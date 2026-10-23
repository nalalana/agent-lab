"""No database/model: ensure failed or missing cell-version cases cannot pass."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from run_realdb_cell_versions import CASE_IDS, cell_checks, run


class CellVersionOracleTests(unittest.TestCase):
    def test_failure_facts_and_missing_cases_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new"
            def completed(folder, _config, **kwargs):
                self.assertIs(kwargs["extra_checks"], cell_checks)
                self.assertEqual(kwargs["manifest_updates"]["case_ids"], CASE_IDS)
                folder.mkdir()
                (folder / "summary.json").write_text(json.dumps({"status": "passed", "postgresql_lock_behavior_tested": True}))
                (folder / "cell-version-trials.json").write_text(json.dumps([
                    {"case": CASE_IDS[0], "facts": {"stale_new_remote_calls": 1},
                     "checks": {"stale_zero_remote_calls": False}, "passed": False}]))
                return 0
            with patch("run_realdb_cell_versions.integration.run", side_effect=completed), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run(output, Path("unused-config.json")), 1)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual((summary["status"], summary["cell_version_cases_completed"], summary["cell_version_cases_passed"]), ("failed", 1, 0))
            self.assertEqual(summary["cell_version_failed_checks"], [{"case": CASE_IDS[0], "check": "stale_zero_remote_calls"}])


if __name__ == "__main__":
    unittest.main()
