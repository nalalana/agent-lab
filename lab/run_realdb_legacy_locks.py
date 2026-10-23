"""Two supplemental legacy writer lock checks, separate from the thirteen-case oracle."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import io
import json
from pathlib import Path
import threading
import time
from uuid import uuid4

from bootstrap import ROOT
import run_realdb_integration as integration
from run_realdb_legacy import attempt, evaluate
from run_postgres_locks import require
from source_identity import source_identity

CASE_IDS = ["raw_then_delete_lock_conflict", "delete_then_raw_lock_conflict"]


class PausingRemote:
    def __init__(self, target, entered, release):
        self.target, self.entered, self.release, self.armed = target, entered, release, False

    def __getattr__(self, name):
        return getattr(self.target, name)

    def pause(self):
        if self.armed:
            self.armed = False
            self.entered.set()
            require(self.release.wait(5), "Holding legacy writer was not released")

    def upsert_data(self, *args):
        self.pause()
        return self.target.upsert_data(*args)

    def delete_row(self, *args):
        self.pause()
        return self.target.delete_row(*args)


def lock_checks(output, user, gate, factory):
    from django.db import connection, connections
    from apps.opengate.models import GatesTable
    from run_table_boundary import load_writer
    rows = []
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(postgresql_server_version=connection.connection.get_parameter_status("server_version"))
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def check(facts, checks, first):
        table = GatesTable.objects.create(tid="t" + uuid4().hex, gate=gate, name=first,
            num_columns=2, created_at="1", updated_at="1")
        entered, release = threading.Event(), threading.Event()
        remote = PausingRemote(factory(gate, table), entered, release)
        writer = load_writer(remote)
        seed = writer.upsert_table_cells(user, table, [{"r": 0, "c": 0, "value": 100}], expected_table_version=0)
        require(seed[0] is True, "Supplemental lock seed failed")
        version = seed[1]["table_version"]
        remote.armed = True
        waiter_name = "legacy_wait_" + uuid4().hex

        def invoke(kind, name):
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SET application_name = %s", [name])
                method, arguments = ((writer.upsert_table_data_full, {"r": 0, "c": 0, "ctt": "n", "cts": "200"})
                    if kind == "raw" else (writer.delete_table_row, 0))
                return attempt(method, user, table, arguments, version=version)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            winner = pool.submit(invoke, first, "legacy_winner_" + uuid4().hex)
            try:
                require(entered.wait(3), "First legacy writer never reached its remote boundary")
                loser = pool.submit(invoke, "delete" if first == "raw" else "raw", waiter_name)
                waiting = False
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE application_name=%s AND wait_event_type='Lock')", [waiter_name])
                        waiting = cursor.fetchone()[0]
                    if waiting or loser.done():
                        break
                    time.sleep(0.02)
                facts["postgres_lock_wait_observed"] = waiting and not loser.done()
            finally:
                release.set()
            facts["winner"], facts["loser"] = winner.result(timeout=6), loser.result(timeout=6)
        facts["remote_mutation_calls"], facts["remote_values"] = remote.calls, remote.values(table.tid)
        expected = [{"r": 0, "c": 0, "value": "200"}] if first == "raw" else []
        checks.update(postgres_lock_wait_observed=facts["postgres_lock_wait_observed"],
            winner_acknowledged=facts["winner"]["acknowledged"],
            stale_loser_rejected=facts["loser"]["typed_blocked"] and facts["loser"].get("error_code") == "TABLE_WRITE_CONFLICT",
            loser_zero_remote_mutations=remote.calls == 2, winner_value_preserved=facts["remote_values"] == expected)

    for first, name in zip(("raw", "delete"), CASE_IDS):
        rows.append(evaluate(name, lambda facts, checks: check(facts, checks, first)))
        (output / "legacy-lock-trials.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return rows


def run(output, config_path):
    manifest = {"kind": "real_storage_legacy_lock_oracle", "oracle_version": 1,
        "case_ids": CASE_IDS, "oracle_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "identity": source_identity(ROOT), "lab_sqlite_unknown_guard_enabled": False,
        "separate_from_legacy_thirteen_cases": True,
        "substitutions": ["source-extracted production methods", "IAM/moderation allow fixture",
            "RPC boundary pause before actual SDK dispatch"],
        "not_tested": ["full platform runtime", "production IAM/moderation", "unknown reconciliation"]}
    with contextlib.redirect_stdout(io.StringIO()):
        code = integration.run(output, config_path, extra_checks=lock_checks, manifest_updates=manifest)
    summary_path, trials_path = output / "summary.json", output / "legacy-lock-trials.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    rows = json.loads(trials_path.read_text(encoding="utf-8")) if trials_path.exists() else []
    failures = [{"case": row["case"], "check": name} for row in rows for name, passed in row["checks"].items() if not passed]
    summary.update(kind=manifest["kind"], legacy_lock_cases_expected=2, legacy_lock_cases_completed=len(rows),
        legacy_lock_cases_passed=sum(row["passed"] for row in rows), legacy_lock_failed_checks=failures,
        lab_sqlite_unknown_guard_enabled=False, separate_from_legacy_thirteen_cases=True)
    summary["checks_passed"] = int(summary.get("postgresql_lock_behavior_tested", False)) * 2 + summary["legacy_lock_cases_passed"]
    if summary.get("status") == "passed" and (failures or len(rows) != 2):
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
