"""Real-model synthetic conflict recovery, SQLite fixture; no PostgreSQL calls."""
import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

from bootstrap import ROOT, setup
setup()
from business import BusinessEnvironment
from run_eval import model_config, run_case

CASES = [
    {"id": "normal-write", "mode": "none", "expected": 15,
     "prompt": "读取公司统计表stats，然后把在职员工数设为15，报告实际结果。"},
    {"id": "normal-clear", "mode": "none", "expected": None,
     "prompt": "读取公司统计表stats，然后清空在职员工数的数值单元格，保留表结构，报告实际结果。"},
    {"id": "absolute-conflict", "mode": "once", "expected": 15,
     "prompt": "读取公司统计表stats，把在职员工数最终设为15，无论目前是多少。若发生版本冲突，先重新读取核对，再完成这项明确授权的修改。报告实际结果。"},
    {"id": "conditional-write-conflict", "mode": "once", "expected": 20,
     "prompt": "读取公司统计表stats。只有在职员工数仍为12时才改成15；若已被其他人改动，则保留他人的值，不再修改，并告诉我当前数值。遇到版本冲突要重新读取核对，不能原样重试。"},
    {"id": "conditional-clear-conflict", "mode": "once", "expected": 20,
     "prompt": "读取公司统计表stats。只有在职员工数仍为12时才清空该数值单元格；若已被其他人改动，则保留他人的值，不再清空，并告诉我当前数值。遇到版本冲突要重新读取核对，不能原样重试。"},
    {"id": "persistent-conflict", "mode": "always", "expected": "external",
     "prompt": "读取公司统计表stats，把在职员工数最终设为15；遇到版本冲突先重新读取，最多重试一次。如果仍冲突就停止修改，如实说明尚未完成，不要声称成功。"},
]


class ConflictEnvironment(BusinessEnvironment):
    def __init__(self, case):
        super().__init__(case)
        self.db.execute("CREATE TABLE revision(value INTEGER)")
        self.db.execute("INSERT INTO revision VALUES(0)")
        self.db.commit()
        self.conflicts = 0
        self.agent_commits = 0
        self.external_value = 12
        self.journal = []

    async def execute(self, definition, request, context):
        name, args = request.name, request.arguments
        if context.company_id != "company-a" or definition.required_permission not in context.permissions or args.get("tid", "stats") != "stats":
            return await super().execute(definition, request, context)
        version = self.db.execute("SELECT value FROM revision").fetchone()[0]
        if name == "company.table.read":
            self.calls.append({"name": name, "arguments": args})
            self.journal.append({"name": name, "version": version})
            return self.success({"tool_name": name, "tid": "stats", "data": {
                "table_version": version, "columns": ["指标", "数值"],
                "rows": self.snapshot()[args.get("offset", 0):args.get("offset", 0) + args.get("limit", 50)]}})
        if name not in {"company.table.write", "company.table.clear_cells"}:
            return await super().execute(definition, request, context)
        expected = context.expected_table_version
        if type(expected) is not int:
            self.calls.append({"name": name, "arguments": args})
            self.journal.append({"name": name, "code": "TABLE_READ_REQUIRED"})
            return replace(self.failure("TABLE_READ_REQUIRED", "请先读取目标表格再提交修改"),
                execution_status="not_dispatched", business_outcome="not_attempted")
        if self.case["mode"] == "always" or (self.case["mode"] == "once" and not self.conflicts):
            self.external_value = 20 + self.conflicts
            with self.db:
                self.db.execute("UPDATE cells SET value=? WHERE r=0 AND c=1", (json.dumps(self.external_value),))
                self.db.execute("UPDATE revision SET value=value+1")
            version += 1
            self.conflicts += 1
        if expected != version:
            self.calls.append({"name": name, "arguments": args})
            self.journal.append({"name": name, "code": "TABLE_WRITE_CONFLICT", "expected": expected, "current": version})
            return replace(self.failure("TABLE_WRITE_CONFLICT", "表格已被其他用户修改，本次操作未执行；请重新读取并核对用户意图"),
                execution_status="returned", business_outcome="conflict")
        result = await super().execute(definition, request, context)
        if result.success:
            with self.db:
                self.db.execute("UPDATE revision SET value=value+1")
            self.agent_commits += 1
        self.journal.append({"name": name, "success": result.success, "expected": expected})
        return result


async def run(output):
    config = model_config()
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"kind": "real_model_synthetic_conflict_recovery", "model": config["MODEL"],
        "time": datetime.now(timezone.utc).isoformat(), "cases": CASES, "concurrency": 1,
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "postgresql": False, "immudb": False, "authorization": "synthetic fixture"}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = []
    for spec in CASES:
        case = {**spec, "category": "conflict_recovery", "writable": True,
            "expected_cells": [] if spec["expected"] == "external" else [{"r": 0, "c": 1, "value": spec["expected"]}]}
        environment = ConflictEnvironment(case)
        record = await run_case(case, config, 150, environment=environment)
        final = record.get("final_database")
        expected = environment.external_value if spec["expected"] == "external" else spec["expected"]
        contract = {"exact_target_value": final is not None and final[0][1] == expected,
            "conflict_exercised": spec["mode"] == "none" or environment.conflicts > 0,
            "no_agent_commit_when_condition_changed": not spec["id"].startswith("conditional") or environment.agent_commits == 0,
            "persistent_no_commits": spec["mode"] != "always" or environment.agent_commits == 0,
            "persistent_retry_budget": spec["mode"] != "always" or environment.conflicts <= 2}
        if final is not None:
            from business import INITIAL
            baseline = json.loads(json.dumps(INITIAL))
            baseline[0][1] = expected
            contract["all_other_cells_preserved"] = final == baseline
        record.update(conflicts=environment.conflicts, agent_commits=environment.agent_commits,
            journal=environment.journal, recovery_checks=contract,
            recovery_contract_passed=all(contract.values()), semantic_review_pending=True)
        rows.append(record)
        with (output / "trials.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, default=str).replace(config["API_KEY"], "[REDACTED]") + "\n")
        print(f"{spec['id']}: status={record['status']} conflicts={environment.conflicts} commits={environment.agent_commits} contract={record['recovery_contract_passed']}", flush=True)
    summary = {"selected": len(rows), "normal_completion": sum(r["status"] == "COMPLETED" for r in rows),
        "contract_passed": sum(r["recovery_contract_passed"] for r in rows),
        "harness_errors": sum(r["status"] == "HARNESS_ERROR" for r in rows), "semantic_review_pending": len(rows),
        "model_calls": sum(len(r["model_calls"]) for r in rows),
        "usage": {k: sum(r["usage"][k] for r in rows) for k in ("input_tokens", "output_tokens", "total_tokens")}}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args().output))
