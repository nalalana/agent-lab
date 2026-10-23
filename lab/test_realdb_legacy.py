"""Offline oracle contracts only; no database connections or real credentials."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bootstrap import setup
setup()
from common.immudbIO.errors import UnconfirmedWriteError
from run_realdb_integration import CommitThenLoseAcknowledgement, RealRemote, load_service
from run_realdb_legacy import CASE_IDS, MUTATIONS, attempt, evaluate, legacy_checks, run
from run_table_boundary import load_writer

_domain = load_writer(Mock()).upsert_table_cells.__globals__
TableWriteConflict = _domain["TableWriteConflict"]
TableWriteDurabilityError = _domain["TableWriteDurabilityError"]
TableCellValidationError = _domain["TableCellValidationError"]


class LegacyOracleTests(unittest.TestCase):
    def test_old_signature_is_really_called_and_new_signature_receives_revision(self):
        recorded = []
        def old(argument):
            recorded.append((argument, "absent"))
            return True, "committed"
        def current(argument, expected_table_version=None):
            recorded.append((argument, expected_table_version))
            return True, "committed"
        for method, supported in ((old, False), (current, True)):
            result = attempt(method, "payload", version=9)
            self.assertEqual(result["supports_expected_table_version"], supported)
            self.assertTrue(result["acknowledged"])
            self.assertFalse(result["typed_blocked"])
        self.assertEqual(recorded, [("payload", "absent"), ("payload", 9)])

    def test_only_known_domain_types_count_as_typed_rejection(self):
        for outcome in (UnconfirmedWriteError(), TableWriteConflict("TABLE_WRITE_CONFLICT", "stale"),
                TableWriteDurabilityError("outer transaction"), TableCellValidationError({"bg": ["invalid"]}),
                RuntimeError("OPERATION_OUTCOME_UNKNOWN"), TypeError("missing revision"),
                (False, "denied"), {"success": False}):
            def operation():
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome
            result = attempt(operation, version=2)
            typed = isinstance(outcome, (UnconfirmedWriteError, TableWriteConflict,
                TableWriteDurabilityError, TableCellValidationError))
            self.assertEqual(result["typed_blocked"], typed)
            self.assertFalse(result["acknowledged"])
            if isinstance(outcome, (TypeError, RuntimeError)) and not typed:
                self.assertFalse(result["blocked"])

    def test_delete_uses_original_service_and_fault_occurs_after_sdk_commit(self):
        rpc = Mock()
        rpc.sqlQuery.return_value = [["cell-id"]]
        rpc.sqlExec.return_value = SimpleNamespace(txs=[1], ongoingTx=False)
        proxy = CommitThenLoseAcknowledgement(rpc)
        proxy.armed = "DELETE FROM "
        remote = RealRemote.__new__(RealRemote)
        remote.calls, remote.service = 0, load_service(proxy)
        self.assertIs(remote.delete_row("test_fixture", "tfixture", 0)[0], False)
        self.assertEqual((remote.calls, rpc.sqlExec.call_count, proxy.committed_then_lost), (1, 1, 1))
        self.assertIsNone(proxy.armed)
        writer = load_writer(remote)
        self.assertTrue(callable(writer.upsert_table_data_full))
        self.assertTrue(callable(writer.delete_table_row))

    def test_case_errors_preserve_prior_facts_and_do_not_prevent_remaining_cases(self):
        def broken(facts, checks):
            facts["before"] = {"remote_calls": 1}
            checks["zero_calls"] = False
            raise ValueError("not a safe rejection")
        rows = [evaluate("broken", broken), evaluate("later", lambda f, c: c.update(completed=True))]
        self.assertFalse(rows[0]["passed"])
        self.assertEqual(rows[0]["facts"]["before"], {"remote_calls": 1})
        self.assertFalse(rows[0]["checks"]["zero_calls"])
        self.assertFalse(rows[0]["checks"]["scenario_completed"])
        self.assertTrue(rows[1]["passed"])

    def test_fixed_cases_and_all_facts_survive_failed_oracle(self):
        self.assertEqual(len(CASE_IDS), 13)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results"
            def integration_run(folder, _config, **kwargs):
                self.assertIs(kwargs["extra_checks"], legacy_checks)
                manifest = kwargs["manifest_updates"]
                self.assertEqual(manifest["case_ids"], CASE_IDS)
                self.assertEqual(manifest["ack_lost_retry_ids"], list(MUTATIONS))
                self.assertEqual(len(manifest["oracle_sha256"]), 64)
                self.assertFalse(manifest["lab_sqlite_unknown_guard_enabled"])
                folder.mkdir()
                (folder / "summary.json").write_text(json.dumps({"status": "passed", "checks_passed": 15,
                    "postgresql_lock_behavior_tested": True, "schema_removed": True}))
                rows = [{"case": name, "facts": {"sequence": i}, "checks": {"zero_calls": i != 2},
                    "passed": i != 2} for i, name in enumerate(CASE_IDS)]
                (folder / "legacy-trials.json").write_text(json.dumps(rows))
                return 0
            with patch("run_realdb_legacy.integration.run", side_effect=integration_run), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run(output, Path("unused-test-config.json")), 1)
            summary = json.loads((output / "summary.json").read_text())
            rows = json.loads((output / "legacy-trials.json").read_text())
            self.assertEqual((len(rows), summary["legacy_cases_completed"], summary["legacy_cases_passed"]), (13, 13, 12))
            self.assertEqual(summary["checks_passed"], 14)
            self.assertEqual(summary["status"], "failed")
            self.assertEqual(rows[-1]["facts"], {"sequence": 12})
            self.assertEqual(summary["legacy_failed_checks"], [{"case": CASE_IDS[2], "check": "zero_calls"}])


if __name__ == "__main__":
    unittest.main()
