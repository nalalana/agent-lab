"""Fixed legacy-entry storage contracts; real DB execution requires explicit local config."""
import argparse
import contextlib
import hashlib
import inspect
import io
import json
from pathlib import Path
from uuid import uuid4

from bootstrap import ROOT
import run_realdb_integration as integration
from run_realdb_safety import managed_read
from source_identity import source_identity

MUTATIONS = ("raw", "cells", "clear", "delete")
CASE_IDS = ["raw_normal_metadata", "delete_normal_version", "pending_write_blocks_raw",
    "pending_write_blocks_delete", "pending_clear_blocks_raw", "pending_clear_blocks_delete",
    "raw_ack_lost_blocks_all_mutations", "delete_ack_lost_blocks_all_mutations", "raw_invalid_no_dispatch",
    "raw_stale_version_no_dispatch", "delete_stale_version_no_dispatch",
    "raw_outer_atomic_no_dispatch", "delete_outer_atomic_no_dispatch"]
METADATA_FIELDS = ("r", "c", "ctfa", "ctt", "cts", "bg", "fs", "bl", "ff", "fc")


def attempt(method, *args, version=None):
    """Call the real signature; absent revision support is evidence, never a TypeError pass."""
    supports_version = "expected_table_version" in inspect.signature(method).parameters
    facts = {"supports_expected_table_version": supports_version, "requested_table_version": version,
             "acknowledged": False, "blocked": False, "typed_blocked": False}
    known = tuple(value for name in ("TableWriteConflict", "TableWriteDurabilityError",
        "TableCellValidationError", "UnconfirmedWriteError")
        if isinstance(value := method.__globals__.get(name), type) and issubclass(value, Exception))
    try:
        result = method(*args, **({"expected_table_version": version} if supports_version else {}))
        facts.update(returned_type=type(result).__name__,
            acknowledged=isinstance(result, tuple) and bool(result) and result[0] is True,
            blocked=isinstance(result, tuple) and bool(result) and result[0] is False)
    except known as exc:
        facts.update(blocked=True, typed_blocked=True, error_type=type(exc).__name__,
            error_code=getattr(exc, "code", None), execution_status=getattr(exc, "execution_status", None),
            business_outcome=getattr(exc, "business_outcome", None))
    except Exception as exc:
        facts["unexpected_error_type"] = type(exc).__name__
    return facts


def evaluate(name, check):
    row = {"case": name, "facts": {}, "checks": {}}
    try:
        check(row["facts"], row["checks"])
    except Exception as exc:
        row["facts"]["scenario_error_type"] = type(exc).__name__
        row["checks"]["scenario_completed"] = False
    row["passed"] = bool(row["checks"]) and all(row["checks"].values())
    return row


def legacy_checks(output, user, gate, factory):
    from django.db import connection, transaction
    from apps.opengate.models import GatesTable, TableAuditEvent
    from common.immudbIO.client import toumingClient
    from run_table_boundary import load_writer

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

    def mutate(writer, table, kind, version, *, value=100, payload=None):
        methods = {"raw": (writer.upsert_table_data_full, payload if payload is not None else
                   {"r": 0, "c": 0, "ctt": "n", "cts": str(value)}),
            "cells": (writer.upsert_table_cells, [{"r": 0, "c": 0, "value": value}]),
            "clear": (writer.clear_table_cells, [{"r": 0, "c": 0}]),
            "delete": (writer.delete_table_row, 0)}
        method, arguments = methods[kind]
        return attempt(method, user, table, arguments, version=version)

    def snapshot(writer, table, remote):
        return {"remote_values": remote.values(table.tid), "remote_mutation_calls": remote.calls,
            "audit_version": writer.upsert_table_cells.__globals__["_table_version"](table),
            "audit_events": list(TableAuditEvent.objects.filter(table=table).order_by("id").values(
                "id", "action", "result", "row_index", "column_index", "details"))}

    def isolated_table(facts, checks):
        other, remote, writer = new_table("unrelated")
        observed = managed_read(writer, user, other)
        facts["other_table_write"] = mutate(writer, other, "cells", observed["version"], value=77)
        facts["other_table_after"] = snapshot(writer, other, remote)
        checks["unrelated_table_writable"] = (observed["has_version"] and facts["other_table_write"]["acknowledged"]
            and remote.calls == 1 and facts["other_table_after"]["remote_values"] == [{"r": 0, "c": 0, "value": "77"}])

    def normal_raw(facts, checks):
        table, remote, writer = new_table("raw_normal")
        facts["before_read"] = managed_read(writer, user, table)
        payload = dict(zip(METADATA_FIELDS, (0, 0, "0.00", "n", "123.40", "#12AbEF", 17, True, "4", "#AABBCC")))
        facts["payload"] = payload
        facts["attempt"] = mutate(writer, table, "raw", facts["before_read"]["version"], payload=payload)
        remote.rpc.useDatabase(gate.gid.encode("utf-8"))
        facts["sql_metadata"] = [list(row) for row in
            toumingClient(remote.rpc).simpleSelect(table.tid, list(METADATA_FIELDS), {})]
        facts["after_read"] = managed_read(writer, user, table)
        facts["after"] = snapshot(writer, table, remote)
        checks.update(acknowledged=facts["attempt"]["acknowledged"], exactly_one_remote_call=remote.calls == 1,
            raw_metadata_preserved=facts["sql_metadata"] == [[payload[field] for field in METADATA_FIELDS]],
            managed_version_advanced=facts["before_read"]["has_version"] and facts["after_read"]["has_version"]
                and facts["after_read"]["version"] > facts["before_read"]["version"])

    def normal_delete(facts, checks):
        table, remote, writer = new_table("delete_normal")
        facts["seed"] = attempt(writer.upsert_table_cells, user, table,
            [{"r": 0, "c": 0, "value": 100}, {"r": 1, "c": 1, "value": 200}], version=0)
        facts["before_read"] = managed_read(writer, user, table)
        calls = remote.calls
        facts["attempt"] = mutate(writer, table, "delete", facts["before_read"]["version"])
        facts["after_read"] = managed_read(writer, user, table)
        facts["after"] = snapshot(writer, table, remote)
        checks.update(seed_acknowledged=facts["seed"]["acknowledged"], acknowledged=facts["attempt"]["acknowledged"],
            exactly_one_delete_call=remote.calls - calls == 1,
            only_target_row_deleted=facts["after"]["remote_values"] == [{"r": 1, "c": 1, "value": "200"}],
            managed_version_advanced=facts["before_read"]["has_version"] and facts["after_read"]["has_version"]
                and facts["after_read"]["version"] > facts["before_read"]["version"])

    def unknown(facts, checks, fault, retry):
        table, remote, writer = new_table(fault + "_lost_ack_" + retry)
        if fault in {"clear", "delete"}:
            facts["seed"] = mutate(writer, table, "cells", 0)
            checks["seed_acknowledged"] = facts["seed"]["acknowledged"]
        facts["original_read"] = managed_read(writer, user, table)
        original = facts["original_read"]["version"]
        remote.rpc.armed = {"raw": "UPSERT INTO ", "cells": "UPSERT INTO ", "clear": "UPDATE ", "delete": "DELETE FROM "}[fault]
        facts["fault_attempt"] = mutate(writer, table, fault, original)
        facts["after_fault"] = snapshot(writer, table, remote)
        facts["real_commit_ack_dropped"] = remote.rpc.committed_then_lost
        fresh_writer = load_writer(remote)  # New service globals; no SQLite unknown guard.
        facts["new_run_read"] = managed_read(fresh_writer, user, table)
        calls = remote.calls
        facts["retry_kind"] = retry
        facts["retry_attempt"] = mutate(fresh_writer, table, retry, original, value=200)
        facts["after_retry"] = snapshot(fresh_writer, table, remote)
        facts["retry_remote_mutation_calls"] = remote.calls - calls
        expected = [] if fault == "delete" else [{"r": 0, "c": 0, "value": "" if fault == "clear" else "100"}]
        rejection = facts["retry_attempt"]
        checks.update(initial_read_has_version=facts["original_read"]["has_version"],
            real_commit_ack_lost=remote.rpc.committed_then_lost == 1,
            initial_outcome_not_acknowledged=facts["fault_attempt"]["blocked"],
            committed_effect_observed=facts["after_fault"]["remote_values"] == expected,
            pending_managed_read_has_no_version=not facts["new_run_read"]["has_version"],
            typed_unknown_rejection=rejection["typed_blocked"] and rejection.get("error_type") == "UnconfirmedWriteError"
                and rejection.get("error_code") == "OPERATION_OUTCOME_UNKNOWN",
            zero_retry_remote_calls=facts["retry_remote_mutation_calls"] == 0,
            committed_values_unchanged=facts["after_retry"]["remote_values"] == facts["after_fault"]["remote_values"])
        isolated_table(facts, checks)

    def unknown_all(facts, checks, fault):
        # Each retry gets its own real table and fault; a prior retry cannot make another one stale.
        facts["retries"] = [evaluate(kind, lambda f, c: unknown(f, c, fault, kind)) for kind in MUTATIONS]
        checks.update({row["case"] + "_safe": row["passed"] for row in facts["retries"]})

    def reject(facts, checks, kind, reason):
        table, remote, writer = new_table(kind + "_" + reason)
        original = managed_read(writer, user, table)
        if reason != "invalid":
            facts["seed"] = mutate(writer, table, "cells", original["version"])
            checks["seed_acknowledged"] = facts["seed"]["acknowledged"]
        facts["before"] = snapshot(writer, table, remote)
        facts["before_read"] = managed_read(writer, user, table)
        version = original["version"] if reason == "stale" else facts["before_read"]["version"]
        payload = {"r": 0, "c": 0, "cts": "invalid", "bg": "not-a-color"} if reason == "invalid" else None
        if reason == "atomic":
            with transaction.atomic():
                facts["attempt"] = mutate(writer, table, kind, version, value=200, payload=payload)
        else:
            facts["attempt"] = mutate(writer, table, kind, version, value=200, payload=payload)
        facts["after"] = snapshot(writer, table, remote)
        rejection = facts["attempt"]
        checks.update(typed_rejection=rejection["typed_blocked"] and rejection.get("error_type") ==
            {"invalid": "TableCellValidationError", "stale": "TableWriteConflict", "atomic": "TableWriteDurabilityError"}[reason],
            zero_remote_calls=facts["after"]["remote_mutation_calls"] == facts["before"]["remote_mutation_calls"],
            values_unchanged=facts["after"]["remote_values"] == facts["before"]["remote_values"],
            business_version_unchanged=facts["after"]["audit_version"] == facts["before"]["audit_version"])
        if reason == "stale":
            checks.update(explicit_revision_supported=rejection["supports_expected_table_version"],
                stale_revision_was_real=original["has_version"] and facts["before_read"]["has_version"]
                    and original["version"] < facts["before_read"]["version"],
                conflict_code=rejection.get("error_code") == "TABLE_WRITE_CONFLICT")
        elif reason == "atomic":
            checks["durability_code"] = rejection.get("error_code") == "TABLE_WRITE_DURABILITY_REQUIRED"

    def record(name, check):
        (output / "active-case.json").write_text(json.dumps({"case": name}), encoding="utf-8")
        rows.append(evaluate(name, check))
        temporary = output / "legacy-trials.tmp"
        temporary.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        temporary.replace(output / "legacy-trials.json")

    record("raw_normal_metadata", normal_raw)
    record("delete_normal_version", normal_delete)
    for fault in ("cells", "clear"):
        for retry in ("raw", "delete"):
            record(f"pending_{'write' if fault == 'cells' else 'clear'}_blocks_{retry}",
                lambda f, c: unknown(f, c, fault, retry))
    for fault in ("raw", "delete"):
        record(fault + "_ack_lost_blocks_all_mutations", lambda f, c: unknown_all(f, c, fault))
    record("raw_invalid_no_dispatch", lambda f, c: reject(f, c, "raw", "invalid"))
    for reason, label in (("stale", "stale_version"), ("atomic", "outer_atomic")):
        for kind in ("raw", "delete"):
            record(f"{kind}_{label}_no_dispatch", lambda f, c: reject(f, c, kind, reason))
    return rows


def run(output, config_path):
    storage_sources = ("backend/apps/opengate/services.py", "backend/apps/opengate/models.py",
        "backend/common/immudbIO/services.py", "backend/common/immudbIO/client.py",
        "backend/common/immudbIO/models.py", "backend/common/immudbIO/serializers.py")
    manifest = {"kind": "shared_real_storage_legacy_oracle", "oracle_version": 1, "case_ids": CASE_IDS,
        "ack_lost_retry_ids": list(MUTATIONS), "oracle_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "identity": {**source_identity(ROOT), "storage_source_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in storage_sources}},
        "counter_scope": "mutation service entry (including local SDK-wrapper validation), not SQL RPC count",
        "fresh_run_scope": "fresh source-extracted service class/globals in the same process; no SQLite journal",
        "lab_sqlite_unknown_guard_enabled": False,
        "substitutions": ["source-extracted production methods (constructor replaced)", "IAM allow fixture",
            "moderation allow fixture", "SDK acknowledgement fault after actual commit"],
        "not_tested": ["full HTTP/IAM/moderation/runtime integration", "authoritative unknown reconciliation",
            "arbitrary process/server crashes", "concurrent legacy-entry requests"]}
    with contextlib.redirect_stdout(io.StringIO()):
        code = integration.run(output, config_path, extra_checks=legacy_checks, manifest_updates=manifest)
    summary_path, trials_path = output / "summary.json", output / "legacy-trials.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    rows = json.loads(trials_path.read_text(encoding="utf-8")) if trials_path.exists() else []
    failures = [{"case": row["case"], "check": name} for row in rows for name, passed in row["checks"].items() if not passed]
    summary.update(kind=manifest["kind"], oracle_version=1, legacy_cases_completed=len(rows), legacy_cases_expected=len(CASE_IDS),
        legacy_cases_passed=sum(row["passed"] for row in rows), legacy_failed_checks=failures,
        lab_sqlite_unknown_guard_enabled=False, model_calls=0)
    summary["diagnostic_rows_completed"] = summary.get("checks_passed", 0)
    summary["checks_passed"] = int(summary.get("postgresql_lock_behavior_tested", False)) * 2 + summary["legacy_cases_passed"]
    if summary.get("status") == "passed" and (failures or [row["case"] for row in rows] != CASE_IDS):
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
