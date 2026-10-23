"""Two real process-exit storage checks, separate from the shared ten-case oracle."""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

from bootstrap import ROOT, setup
import run_realdb_integration as integration
from run_postgres_locks import config as pg_config, require
from run_realdb_safety import TOOLS, attempt, managed_read
from source_identity import source_identity

CASE_IDS = ["write_process_exit", "clear_process_exit"]
EXIT_CODES = {"company.table.write": 71, "company.table.clear_cells": 72}


class CommitThenExit:
    def __init__(self, client, tool):
        self.client, self.tool = client, tool

    def __getattr__(self, name):
        return getattr(self.client, name)

    def sqlExec(self, query, *args, **kwargs):
        result = self.client.sqlExec(query, *args, **kwargs)
        prefix = "UPDATE " if self.tool.endswith("clear_cells") else "UPSERT INTO "
        if query.lstrip().upper().startswith(prefix):
            require(bool(getattr(result, "txs", None)) and not getattr(result, "ongoingTx", False),
                    "Process exit requires an acknowledged real transaction")
            os._exit(EXIT_CODES[self.tool])
        return result


def child_run(config_path, schema, table_pk, tool):
    if not re.fullmatch(r"agent_lab_[0-9a-f]{32}", schema) or table_pk < 1 or tool not in TOOLS:
        raise ValueError("Invalid child test identity")
    cfg, pg = integration.config(config_path), pg_config(config_path)
    setup({"ENGINE": "django.db.backends.postgresql", "NAME": pg["dbname"], "USER": pg["user"],
        "PASSWORD": pg["password"], "HOST": pg["host"], "PORT": pg["port"],
        "OPTIONS": {"connect_timeout": 5, "sslmode": pg["sslmode"],
            "options": f"-c search_path={schema} -c lock_timeout=5000 -c statement_timeout=10000"}})
    from immudb import ImmudbClient
    from apps.opengate.models import GatesTable
    from apps.agent_runtime.contracts import ToolContext, ToolRequest
    from run_table_boundary import load_writer
    from test_table_versions import project_adapter
    table = GatesTable.objects.select_related("gate__owner").get(pk=table_pk)
    require(table.gate.gid.startswith("test_"), "Child requires an existing test project")
    client = ImmudbClient(cfg["address"], timeout=5)
    client.login(cfg["user"], cfg["password"])
    # Do not use RealRemote: its constructor creates a table. This child only
    # opens the exact parent-created table in the exact parent-created schema.
    writer = load_writer(integration.load_service(CommitThenExit(client, tool)))
    user = table.gate.owner
    observed = managed_read(writer, user, table)
    require(observed["has_version"], "Child cannot obtain its initial read revision")
    adapter = project_adapter(writer, table, writer.upsert_table_cells.__globals__["TableWriteConflict"], user)
    cells = [{"r": 0, "c": 0, **({"value": 100} if tool.endswith("write") else {})}]
    adapter(ToolRequest(tool, {"tid": table.tid, "cells": cells}, 5),
        ToolContext(uuid4().hex, uuid4().hex, "fixture", "fixture", user.uid, frozenset(), observed["version"]))
    raise RuntimeError("Child returned without the required committed-process-exit fault")


def launch_child(output, config_path, schema, table_pk, tool):
    label = "clear" if tool.endswith("clear_cells") else "write"
    command = [sys.executable, str(Path(__file__).resolve()), "--child", "--config", str(config_path.resolve()),
               "--schema", schema, "--table-pk", str(table_pk), "--tool", tool]
    try:
        result = subprocess.run(command, cwd=ROOT / "lab", capture_output=True, timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        stdout, stderr, code, timed_out = result.stdout, result.stderr, result.returncode, False
    except subprocess.TimeoutExpired as exc:
        stdout, stderr, code, timed_out = exc.stdout or b"", exc.stderr or b"", None, True
    # SDK diagnostics can contain connection material. Keep them in ignored
    # local artifacts; only the exit code and timeout flag enter public output.
    (output / f"{label}-child-stdout.log").write_bytes(stdout)
    (output / f"{label}-child-stderr.log").write_bytes(stderr)
    return {"exit_code": code, "expected_exit_code": EXIT_CODES[tool], "timed_out": timed_out}


def crash_checks(output, user, gate, factory, config_path):
    from apps.opengate.models import GatesTable, TableAuditEvent
    from apps.agent_runtime.contracts import ToolContext, ToolRequest
    from run_table_boundary import load_writer
    from test_table_versions import project_adapter
    schema = json.loads((output / "manifest.json").read_text(encoding="utf-8"))["schema"]
    rows = []
    for tool, name in zip(TOOLS, CASE_IDS):
        row = {"case": name, "facts": {}, "checks": {}}
        facts, checks = row["facts"], row["checks"]
        try:
            table = GatesTable.objects.create(tid="t" + uuid4().hex, gate=gate, name=name,
                num_columns=2, created_at="1", updated_at="1")
            remote = factory(gate, table)
            writer = load_writer(remote)
            def mutate(current, operation, version, value=200):
                adapter = project_adapter(current, table, current.upsert_table_cells.__globals__["TableWriteConflict"], user)
                cells = [{"r": 0, "c": 0, **({"value": value} if operation.endswith("write") else {})}]
                context = ToolContext(uuid4().hex, uuid4().hex, "fixture", "fixture", user.uid, frozenset(), version)
                return attempt(lambda: adapter(ToolRequest(operation, {"tid": table.tid, "cells": cells}, 5), context))
            if tool.endswith("clear_cells"):
                seed = mutate(writer, "company.table.write", managed_read(writer, user, table)["version"], 100)
                require(seed["acknowledged"], "Parent seed write was not acknowledged")
            original = managed_read(writer, user, table)
            require(original["has_version"], "Parent initial read has no trusted version")
            business_audits = lambda: list(TableAuditEvent.objects.filter(table=table,
                action__in=["cell.upsert", "cell.clear"]).order_by("id").values("id", "action", "result"))
            before_audits = business_audits()
            facts["child"] = launch_child(output, config_path, schema, table.pk, tool)
            facts["after_crash_values"] = remote.values(table.tid)
            facts["business_audit_before"], facts["business_audit_after"] = before_audits, business_audits()
            expected = [{"r": 0, "c": 0, "value": "" if tool.endswith("clear_cells") else "100"}]
            checks.update(expected_committed_exit=facts["child"]["exit_code"] == EXIT_CODES[tool],
                remote_commit_observed=facts["after_crash_values"] == expected,
                business_transaction_rolled_back=facts["business_audit_after"] == before_audits)
            require(checks["expected_committed_exit"] and checks["remote_commit_observed"],
                    "Child did not reach the confirmed commit-exit boundary")
            fresh = load_writer(remote)
            facts["new_run_read"] = managed_read(fresh, user, table)
            checks["managed_read_exposes_no_version"] = not facts["new_run_read"]["has_version"]
            facts["retries"] = []
            for operation in TOOLS:
                calls = remote.calls
                result = mutate(fresh, operation, original["version"])
                observed = remote.values(table.tid)
                facts["retries"].append({"tool": operation, "result": result,
                    "new_remote_mutation_calls": remote.calls - calls, "values": observed})
                checks[operation + ":blocked"] = result["blocked"]
                checks[operation + ":zero_remote_mutations"] = remote.calls == calls
                checks[operation + ":value_unchanged"] = observed == expected
        except Exception as exc:
            facts["scenario_error_type"] = type(exc).__name__
            checks["scenario_completed"] = False
        row["passed"] = bool(checks) and all(checks.values())
        rows.append(row)
        (output / "crash-trials.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return rows


def run(output, config_path):
    manifest = {"kind": "real_storage_process_crash_oracle", "oracle_version": 1, "case_ids": CASE_IDS,
        "oracle_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "identity": source_identity(ROOT),
        "storage_source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
            "backend/apps/opengate/services.py", "backend/common/immudbIO/services.py", "backend/common/immudbIO/client.py")},
        "lab_sqlite_unknown_guard_enabled": False, "separate_from_shared_ten_cases": True,
        "substitutions": ["source-extracted production methods", "IAM/moderation allow fixture",
            "child os._exit after acknowledged real immudb commit"],
        "not_tested": ["full platform runtime", "production IAM/moderation", "unknown reconciliation"]}
    with contextlib.redirect_stdout(io.StringIO()):
        code = integration.run(output, config_path, extra_checks=lambda folder, user, gate, factory:
            crash_checks(folder, user, gate, factory, config_path), manifest_updates=manifest)
    summary_path, trials_path = output / "summary.json", output / "crash-trials.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    rows = json.loads(trials_path.read_text(encoding="utf-8")) if trials_path.exists() else []
    failures = [{"case": row["case"], "check": key} for row in rows for key, passed in row["checks"].items() if not passed]
    summary.update(kind=manifest["kind"], crash_cases_expected=2, crash_cases_completed=len(rows),
        crash_cases_passed=sum(row["passed"] for row in rows), crash_failed_checks=failures,
        lab_sqlite_unknown_guard_enabled=False, separate_from_shared_ten_cases=True, model_calls=0)
    summary["diagnostic_rows_completed"] = summary.get("checks_passed", 0)
    summary["checks_passed"] = int(summary.get("postgresql_lock_behavior_tested", False)) * 2 + summary["crash_cases_passed"]
    if summary.get("status") == "passed" and (failures or len(rows) != 2):
        summary["status"], code = "failed", 1
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary))
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--schema", default="", help=argparse.SUPPRESS)
    parser.add_argument("--table-pk", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--tool", choices=TOOLS, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        try:
            child_run(args.config, args.schema, args.table_pk, args.tool)
        except Exception as exc:
            print(json.dumps({"child_error_type": type(exc).__name__}))
            raise SystemExit(2)
    if args.output is None:
        parser.error("--output is required for parent diagnostics")
    raise SystemExit(run(args.output.resolve(), args.config))
