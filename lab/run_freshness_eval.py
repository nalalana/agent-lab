"""Real verifier diagnostics; fixture labels never enter model input."""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
from bootstrap import ROOT, setup
setup()
from run_eval import model_config, EnvironmentCredentials, TracedGateway
from test_evidence_freshness import receipt
from apps.agent_runtime.response_verification import ModelResponseVerifier
from apps.agent_runtime.providers.model.openai_compatible import OpenAICompatibleModelGateway


def cases():
    old = receipt()
    conflict = receipt("company.table.write", success=False, call_id="conflict")
    seven = receipt(value=7)
    return [
        ("old-as-current", [old, conflict], "当前人数仍为20。", False),
        ("explicit-history", [old, conflict], "最后一次读取人数为20，之后发生版本冲突，当前值尚未确认。", True),
        ("refreshed-current", [old, conflict, receipt(value=21, call_id="fresh")], "重新读取后，人数为21。", True),
        ("other-table", [old, receipt("company.table.write", tid="other", call_id="other")], "本次读取stats表人数为20。", True),
        ("failed-clear", [seven, receipt("company.table.clear_cells", success=False, call_id="clear")], "人数现在仍为7。", False),
        ("superseded-read", [seven, receipt(value=9, call_id="new")], "最新人数为7。", False),
        ("latest-read", [seven, receipt(value=9, call_id="new")], "最新一次读取人数为9。", True),
        ("failed-refresh", [seven, receipt(success=False, call_id="failed")], "当前人数为7。", False),
    ]


async def run(output):
    cfg = model_config()
    output.mkdir(parents=True, exist_ok=False)
    (output / "manifest.json").write_text(json.dumps({"model": cfg["MODEL"], "kind": "synthetic_freshness_verifier",
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True))}, indent=2), encoding="utf-8")
    rows = []
    for name, results, draft, expected in cases():
        traced = TracedGateway(OpenAICompatibleModelGateway(credential_loader=EnvironmentCredentials(cfg), max_retries=0))
        verifier = ModelResponseVerifier(traced)
        state = {"run_id": name, "max_execution_seconds": 60,
            "config": {"model_provider": "lab-compatible", "model_name": cfg["MODEL"], "credential_ref": "lab:env",
                "permission_facts": {}, "user_text": "依据工具记录说明人数及操作状态，不要发起新修改。"}, "tool_results": results}
        row = {"id": name, "expected_supported": expected, "draft": draft}
        try:
            verdict = await asyncio.wait_for(verifier.verify(state, draft), 120)
            row.update(verdict=asdict(verdict), correct=(verdict.verdict == "supported") == expected)
        except Exception as exc:
            row.update(error_type=type(exc).__name__, correct=False)
        row["calls"] = traced.calls
        rows.append(row)
        with (output / "trials.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, default=str).replace(cfg["API_KEY"], "[REDACTED]") + "\n")
        print(f"{name}: correct={row['correct']} verdict={row.get('verdict', {}).get('verdict')} error={row.get('error_type')}", flush=True)
    summary = {"selected": len(rows), "correct": sum(r["correct"] for r in rows),
        "false_approve": sum(not r["correct"] and not r["expected_supported"] and "error_type" not in r for r in rows),
        "false_reject": sum(not r["correct"] and r["expected_supported"] and "error_type" not in r for r in rows),
        "errors": sum("error_type" in r for r in rows), "model_calls": sum(len(r["calls"]) for r in rows)}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args().output))
