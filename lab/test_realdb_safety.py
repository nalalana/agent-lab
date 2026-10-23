"""Offline oracle plumbing only; no credentials, database connections, or models."""
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
from apps.agent_runtime.tools.executors import ToolExecutionError
from common.immudbIO.errors import UnconfirmedWriteError
from run_realdb_safety import CASE_IDS, attempt, managed_read, run, safety_checks


class RealStorageSafetyTests(unittest.TestCase):
    def test_only_actual_typed_failure_or_false_counts_as_blocked(self):
        for outcome in (ToolExecutionError(code="TOOL_DENIED", message="denied"), UnconfirmedWriteError(),
                        (False, "rejected"), RuntimeError("blocked according to this text"),
                        {"success": False, "message": "unknown; blocked"}):
            with self.subTest(kind=type(outcome).__name__):
                operation = Mock(side_effect=outcome) if isinstance(outcome, BaseException) else Mock(return_value=outcome)
                result = attempt(operation)
                self.assertEqual(result["blocked"], isinstance(outcome, (ToolExecutionError, UnconfirmedWriteError, tuple)))
                operation.assert_called_once_with()

    def test_new_version_is_only_from_successful_managed_read_and_never_boolean(self):
        for returned, expected in (((True, {"table_version": 4}), 4), ((False, {"table_version": 4}), None),
                                   ((True, {"table_version": True}), None), ((False, "pending"), None)):
            writer = SimpleNamespace(get_table_data=Mock(return_value=returned))
            self.assertEqual(managed_read(writer, "user", "table")["version"], expected)
            writer.get_table_data.assert_called_once_with("user", "table", None, {}, 50, 0)

    def test_all_fixed_case_facts_survive_safety_failure_and_overall_is_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results"
            def completed_run(folder, _config, **kwargs):
                self.assertIs(kwargs["extra_checks"], safety_checks)
                self.assertEqual(kwargs["manifest_updates"]["case_ids"], CASE_IDS)
                self.assertFalse(kwargs["manifest_updates"]["lab_sqlite_unknown_guard_enabled"])
                folder.mkdir()
                (folder / "summary.json").write_text(json.dumps({"status": "passed", "checks_passed": 12,
                    "postgresql_lock_behavior_tested": True, "schema_removed": True}))
                rows = [{"case": name, "facts": {"sequence": i}, "checks": {"zero_remote_calls": i != 1},
                         "passed": i != 1} for i, name in enumerate(CASE_IDS)]
                (folder / "safety-trials.json").write_text(json.dumps(rows))
                return 0
            with patch("run_realdb_safety.integration.run", side_effect=completed_run), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run(output, Path("unused-local-config.json")), 1)
            summary = json.loads((output / "summary.json").read_text())
            saved = json.loads((output / "safety-trials.json").read_text())
            self.assertEqual((len(saved), summary["safety_cases_completed"], summary["safety_cases_passed"]), (10, 10, 9))
            self.assertEqual(summary["status"], "failed")
            self.assertEqual(saved[-1]["facts"], {"sequence": 9})
            self.assertEqual(summary["safety_failed_checks"], [{"case": CASE_IDS[1], "check": "zero_remote_calls"}])


if __name__ == "__main__":
    unittest.main()
