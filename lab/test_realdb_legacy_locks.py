"""Supplemental oracle plumbing only; no database or model calls."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from run_realdb_legacy_locks import CASE_IDS, PausingRemote, lock_checks, run


class LegacyLockOracleTests(unittest.TestCase):
    def test_pause_precedes_each_real_mutation_and_is_used_once(self):
        for name in ("upsert_data", "delete_row"):
            target, entered, release = Mock(), threading.Event(), threading.Event()
            remote = PausingRemote(target, entered, release)
            def dispatch(*_):
                self.assertTrue(entered.is_set())
                return True, "ack"
            getattr(target, name).side_effect = dispatch
            remote.armed = True
            release.set()
            self.assertEqual(getattr(remote, name)("gid", "tid", {}), (True, "ack"))
            self.assertFalse(remote.armed)
            getattr(target, name).assert_called_once()

    def test_scenario_failures_are_retained_and_cannot_report_overall_passed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new"
            def completed(folder, _config, **kwargs):
                self.assertIs(kwargs["extra_checks"], lock_checks)
                self.assertEqual(kwargs["manifest_updates"]["case_ids"], CASE_IDS)
                folder.mkdir()
                (folder / "summary.json").write_text(json.dumps({"status": "passed", "postgresql_lock_behavior_tested": True}))
                rows = [{"case": name, "facts": {}, "checks": {"actual_lock": i == 0}, "passed": i == 0}
                        for i, name in enumerate(CASE_IDS)]
                (folder / "legacy-lock-trials.json").write_text(json.dumps(rows))
                return 0
            with patch("run_realdb_legacy_locks.integration.run", side_effect=completed), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run(output, Path("unused-config.json")), 1)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual((summary["status"], summary["legacy_lock_cases_passed"], summary["legacy_lock_cases_completed"]), ("failed", 1, 2))
            self.assertEqual(summary["legacy_lock_failed_checks"], [{"case": CASE_IDS[1], "check": "actual_lock"}])


if __name__ == "__main__":
    unittest.main()
