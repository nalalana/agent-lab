"""Three cross-entry cell-version contracts; no model calls or full HTTP substitution."""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
from uuid import uuid4

from bootstrap import ROOT
import run_realdb_integration as integration
from run_realdb_legacy import attempt, evaluate
from run_postgres_locks import require
from source_identity import source_identity

CASE_IDS = ["raw_cell_version", "clear_cell_version", "delete_cell_version"]


def cell_checks(output, user, gate, factory):
    from django.db import connection
    from apps.opengate.models import GatesTable
    from run_table_boundary import load_writer
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["postgresql_server_version"] = connection.connection.get_parameter_status("server_version")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    rows = []

    def check(facts, checks, kind):
        table = GatesTable.objects.create(tid="t" + uuid4().hex, gate=gate, name=kind,
            num_columns=2, created_at="1", updated_at="1")
        remote = factory(gate, table)
        writer = load_writer(remote)
        seed = writer.upsert_table_cells(user, table, [{"r": 0, "c": 0, "value": 100}], expected_table_version=0)
        require(seed[0] is True, "Cell-version seed was not acknowledged")
        ns = writer.upsert_table_cells.__globals__
        cells = [{"r": 0, "c": 0}]
        old = ns["_cell_versions"](table, cells)[(0, 0)]
        method, arguments = {"raw": (writer.upsert_table_data_full, {"r": 0, "c": 0, "ctt": "n", "cts": "150"}),
            "clear": (writer.clear_table_cells, cells), "delete": (writer.delete_table_row, 0)}[kind]
        facts["mutation"] = attempt(method, user, table, arguments, version=seed[1]["table_version"])
        require(facts["mutation"]["acknowledged"], "Intervening mutation was not acknowledged")
        current = ns["_cell_versions"](table, cells)[(0, 0)]
        facts.update(old_cell_version=old, current_cell_version=current, before_stale_values=remote.values(table.tid))
        calls = remote.calls
        # Omit the table revision intentionally: this is the existing optional
        # HTTP contract in which only a cell expected_version was supplied.
        facts["stale"] = attempt(writer.upsert_table_cells, user, table,
            [{"r": 0, "c": 0, "value": 200, "expected_version": old}])
        facts.update(stale_new_remote_calls=remote.calls - calls, after_stale_values=remote.values(table.tid))
        checks.update(cell_revision_advanced=current > old,
            stale_typed_conflict=facts["stale"]["typed_blocked"] and facts["stale"].get("error_code") == "CELL_WRITE_CONFLICT",
            stale_zero_remote_calls=facts["stale_new_remote_calls"] == 0,
            stale_values_preserved=facts["after_stale_values"] == facts["before_stale_values"])
        calls = remote.calls
        facts["fresh"] = attempt(writer.upsert_table_cells, user, table,
            [{"r": 0, "c": 0, "value": 200, "expected_version": current}])
        checks["fresh_revision_writes_normally"] = (facts["fresh"]["acknowledged"] and remote.calls == calls + 1
            and remote.values(table.tid) == [{"r": 0, "c": 0, "value": "200"}])

    for kind, name in zip(("raw", "clear", "delete"), CASE_IDS):
        rows.append(evaluate(name, lambda facts, checks: check(facts, checks, kind)))
        (output / "cell-version-trials.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return rows


def run(output, config_path):
    manifest = {"kind": "real_storage_cross_entry_cell_version_oracle", "case_ids": CASE_IDS,
        "oracle_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "identity": source_identity(ROOT),
        "lab_sqlite_unknown_guard_enabled": False,
        "substitutions": ["source-extracted production methods", "IAM/moderation allow fixture"],
        "not_tested": ["full HTTP/runtime integration", "unknown reconciliation"],
        "counter_scope": "mutation service entry, not SQL RPC count"}
    with contextlib.redirect_stdout(io.StringIO()):
        code = integration.run(output, config_path, extra_checks=cell_checks, manifest_updates=manifest)
    summary_path, trials_path = output / "summary.json", output / "cell-version-trials.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    rows = json.loads(trials_path.read_text(encoding="utf-8")) if trials_path.exists() else []
    failures = [{"case": row["case"], "check": name} for row in rows for name, passed in row["checks"].items() if not passed]
    summary.update(kind=manifest["kind"], cell_version_cases_expected=3, cell_version_cases_completed=len(rows),
        cell_version_cases_passed=sum(row["passed"] for row in rows), cell_version_failed_checks=failures,
        lab_sqlite_unknown_guard_enabled=False)
    summary["checks_passed"] = int(summary.get("postgresql_lock_behavior_tested", False)) * 2 + summary["cell_version_cases_passed"]
    if summary.get("status") == "passed" and (failures or [row["case"] for row in rows] != CASE_IDS):
        summary["status"], code = "failed", 1
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary))
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.output.resolve(), args.config))
