"""Shared storage safety oracle, not an agent/model benchmark or a legacy guard."""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
from uuid import uuid4

from bootstrap import ROOT
import run_realdb_integration as integration
from source_identity import source_identity

TOOLS = ("company.table.write", "company.table.clear_cells")
CASE_IDS = ["normal_write_clear", "invalid_second_cell_all_or_nothing", *(
    f"{fault}:{revision}:{retry}" for fault in TOOLS for revision in ("original", "new_read") for retry in TOOLS)]


def attempt(call):
    """Only typed execution failures or explicit False results count as blocked."""
    from apps.agent_runtime.tools.executors import ToolExecutionError
    from common.immudbIO.errors import UnconfirmedWriteError
    try:
        result = call()
        rejected = isinstance(result, tuple) and result and result[0] is False
        return {"acknowledged": not rejected, "blocked": bool(rejected), "returned_type": type(result).__name__}
    except (ToolExecutionError, UnconfirmedWriteError) as exc:
        return {"acknowledged": False, "blocked": True, "error_type": type(exc).__name__,
                "error_code": str(exc.code), "execution_status": getattr(exc, "execution_status", None),
                "business_outcome": getattr(exc, "business_outcome", None)}
    except Exception as exc:
        return {"acknowledged": False, "blocked": False, "unexpected_error_type": type(exc).__name__}


def managed_read(writer, user, table):
    try:
        ok, result = writer.get_table_data(user, table, None, {}, 50, 0)
        version = result.get("table_version") if ok is True and isinstance(result, dict) else None
        return {"success": ok is True, "has_version": type(version) is int and version >= 0,
                "version": version if type(version) is int and version >= 0 else None}
    except Exception as exc:
        return {"success": False, "has_version": False, "version": None, "error_type": type(exc).__name__}


def safety_checks(output, user, gate, factory):
    from django.db import connection
    from apps.opengate.models import GatesTable, TableAuditEvent
    from apps.agent_runtime.contracts import ToolContext, ToolRequest
    from run_table_boundary import load_writer
    from test_table_versions import project_adapter

    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(postgresql_server_version_number=connection.pg_version,
        postgresql_server_version=connection.connection.get_parameter_status("server_version"))
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    rows = []

    def new_table(label):
        table = GatesTable.objects.create(tid="t" + uuid4().hex, gate=gate, name=label,
            num_columns=2, created_at="1", updated_at="1")
        remote = factory(gate, table)
        return table, remote, load_writer(remote)

    def mutate(writer, table, tool, version, value=100, cells=None):
        adapter = project_adapter(writer, table, writer.upsert_table_cells.__globals__["TableWriteConflict"], user)
        selected = cells if cells is not None else [{"r": 0, "c": 0, **({"value": value} if tool.endswith("write") else {})}]
        request = ToolRequest(tool, {"tid": table.tid, "cells": selected}, 5)
        context = ToolContext("test_" + uuid4().hex, uuid4().hex, "fixture", "fixture", user.uid, frozenset(), version)
        return attempt(lambda: adapter(request, context))

    def snapshot(writer, table, remote):
        return {"remote_values": remote.values(table.tid), "remote_mutation_calls": remote.calls,
                "audit_version": writer.upsert_table_cells.__globals__["_table_version"](table),
                "audit_events": list(TableAuditEvent.objects.filter(table=table).order_by("id").values(
                    "id", "action", "result", "row_index", "column_index", "details"))}

    def record(name, check):
        row = {"case": name, "facts": {}, "checks": {}}
        try:
            check(row["facts"], row["checks"])
        except Exception as exc:
            row["facts"]["scenario_error_type"] = type(exc).__name__
            row["checks"]["scenario_completed"] = False
        row["passed"] = bool(row["checks"]) and all(row["checks"].values())
        rows.append(row)
        temporary = output / "safety-trials.tmp"
        temporary.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        temporary.replace(output / "safety-trials.json")

    def normal(facts, checks):
        table, remote, writer = new_table("normal_write_clear")
        facts["before_read"] = managed_read(writer, user, table)
        facts["write"] = mutate(writer, table, "company.table.write", facts["before_read"]["version"])
        facts["after_write"] = snapshot(writer, table, remote)
        facts["read_after_write"] = managed_read(writer, user, table)
        facts["clear"] = mutate(writer, table, "company.table.clear_cells", facts["read_after_write"]["version"])
        facts["after_clear"] = snapshot(writer, table, remote)
        checks.update(write_acknowledged=facts["write"]["acknowledged"], clear_acknowledged=facts["clear"]["acknowledged"],
            write_value=facts["after_write"]["remote_values"] == [{"r": 0, "c": 0, "value": "100"}],
            clear_value=facts["after_clear"]["remote_values"] == [{"r": 0, "c": 0, "value": ""}],
            exactly_two_mutation_calls=remote.calls == 2, managed_read_has_version=facts["read_after_write"]["has_version"])

    def partial(facts, checks):
        table, remote, writer = new_table("invalid_second_cell")
        facts["before"] = snapshot(writer, table, remote)
        version = managed_read(writer, user, table)["version"]
        facts["attempt"] = mutate(writer, table, "company.table.write", version,
            cells=[{"r": 0, "c": 0, "value": 100}, {"r": 0, "c": 1, "value": ""}])
        facts["after"] = snapshot(writer, table, remote)
        checks.update(rejected=facts["attempt"]["blocked"], all_cells_unchanged=facts["after"]["remote_values"] == [],
            zero_remote_mutation_calls=remote.calls == 0)

    def unknown(facts, checks, fault_tool, version_kind, retry_tool):
        table, remote, writer = new_table("unknown_" + uuid4().hex)
        if fault_tool.endswith("clear_cells"):
            seed_version = managed_read(writer, user, table)["version"]
            facts["seed"] = mutate(writer, table, "company.table.write", seed_version)
        facts["original_read"] = managed_read(writer, user, table)
        original_version = facts["original_read"]["version"]
        remote.rpc.armed = "UPDATE " if fault_tool.endswith("clear_cells") else "UPSERT INTO "
        facts["fault_attempt"] = mutate(writer, table, fault_tool, original_version)
        facts["after_fault"] = snapshot(writer, table, remote)
        facts["real_commit_ack_dropped"] = remote.rpc.committed_then_lost
        fresh_writer = load_writer(remote)  # Fresh service globals + a new ToolContext, no in-memory/SQLite journal.
        facts["new_run_read"] = managed_read(fresh_writer, user, table)
        retry_version = original_version if version_kind == "original" else facts["new_run_read"]["version"]
        facts["retry_version_kind"], facts["retry_version"] = version_kind, retry_version
        before_calls = remote.calls
        facts["retry_attempt"] = mutate(fresh_writer, table, retry_tool, retry_version, value=200)
        facts["after_retry"] = snapshot(fresh_writer, table, remote)
        facts["retry_remote_mutation_calls"] = remote.calls - before_calls
        expected = "" if fault_tool.endswith("clear_cells") else "100"
        checks.update(ack_was_really_lost=remote.rpc.committed_then_lost == 1,
            initial_unknown_not_acknowledged=facts["fault_attempt"]["blocked"],
            committed_effect_observed=facts["after_fault"]["remote_values"] == [{"r": 0, "c": 0, "value": expected}],
            pending_read_exposes_no_version=not facts["new_run_read"]["has_version"],
            retry_actually_blocked=facts["retry_attempt"]["blocked"],
            zero_retry_remote_mutation_calls=facts["retry_remote_mutation_calls"] == 0,
            committed_value_unchanged=facts["after_retry"]["remote_values"] == facts["after_fault"]["remote_values"])
        other, other_remote, other_writer = new_table("unrelated_table")
        facts["other_table_read"] = managed_read(other_writer, user, other)
        facts["other_table_write"] = mutate(other_writer, other, "company.table.write", facts["other_table_read"]["version"], value=77)
        facts["other_table_after"] = snapshot(other_writer, other, other_remote)
        checks["unrelated_table_still_writable"] = (facts["other_table_write"]["acknowledged"] and other_remote.calls == 1
            and facts["other_table_after"]["remote_values"] == [{"r": 0, "c": 0, "value": "77"}])

    record("normal_write_clear", normal)
    record("invalid_second_cell_all_or_nothing", partial)
    for fault_tool in TOOLS:
        for version_kind in ("original", "new_read"):
            for retry_tool in TOOLS:
                name = f"{fault_tool}:{version_kind}:{retry_tool}"
                record(name, lambda facts, checks: unknown(facts, checks, fault_tool, version_kind, retry_tool))
    return rows


def run(output, config_path):
    identity = {**source_identity(ROOT), "storage_source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in ("backend/apps/opengate/services.py", "backend/common/immudbIO/services.py", "backend/common/immudbIO/client.py")}}
    manifest = {"kind": "shared_real_storage_safety_oracle", "oracle_version": 1,
        "case_ids": CASE_IDS,
        "oracle_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "identity": identity,
        "substitutions": ["source-extracted production methods (constructor replaced)", "IAM allow fixture",
            "moderation allow fixture", "SDK acknowledgement fault after actual commit"],
        "lab_sqlite_unknown_guard_enabled": False,
        "not_tested": ["full platform runtime/SSE/recorder", "production IAM/moderation", "authoritative unknown reconciliation"]}
    with contextlib.redirect_stdout(io.StringIO()):
        code = integration.run(output, config_path, extra_checks=safety_checks, manifest_updates=manifest)
    summary_path, trials_path = output / "summary.json", output / "safety-trials.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    rows = json.loads(trials_path.read_text(encoding="utf-8")) if trials_path.exists() else []
    failures = [{"case": row["case"], "check": name} for row in rows for name, passed in row["checks"].items() if not passed]
    summary.update(kind=manifest["kind"], oracle_version=1, safety_cases_completed=len(rows), safety_cases_expected=10,
        safety_cases_passed=sum(row["passed"] for row in rows), safety_failed_checks=failures,
        lab_sqlite_unknown_guard_enabled=False, model_calls=0)
    summary["diagnostic_rows_completed"] = summary.get("checks_passed", 0)
    summary["checks_passed"] = int(summary.get("postgresql_lock_behavior_tested", False)) * 2 + summary["safety_cases_passed"]
    if summary.get("status") == "passed" and (failures or len(rows) != 10):
        summary["status"], code = "failed", 1
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary))
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    arguments = parser.parse_args()
    raise SystemExit(run(arguments.output.resolve(), arguments.config))
