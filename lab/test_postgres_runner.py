import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import psycopg2
from run_postgres_locks import run


class PostgresRunnerTests(unittest.TestCase):
    def check_unavailable(self, env, reason, connection_error=False):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, env, clear=True):
            output = Path(directory) / "result"
            captured = io.StringIO()
            with patch("psycopg2.connect", side_effect=psycopg2.OperationalError("secret-test-password")) as connect:
                with contextlib.redirect_stdout(captured):
                    self.assertEqual(run(output), 2)
                self.assertEqual(connect.call_count, int(connection_error))
            result = json.loads((output / "summary.json").read_text())
            self.assertEqual(result["status"], "unavailable")
            self.assertFalse(result["postgresql_lock_behavior_tested"])
            self.assertEqual(result["checks_passed"], 0)
            self.assertIn(reason, result["reason"])
            self.assertNotIn("secret-test-password", captured.getvalue())
            self.assertFalse((output / "manifest.json").exists())

    def test_no_opt_in_does_not_connect(self):
        self.check_unavailable({}, "AGENT_LAB_PG_ENABLED")

    def test_missing_password_does_not_connect(self):
        self.check_unavailable({"AGENT_LAB_PG_ENABLED": "1"}, "AGENT_LAB_PG_PASSWORD")

    def test_unavailable_postgres_never_falls_back_or_exposes_credentials(self):
        self.check_unavailable({"AGENT_LAB_PG_ENABLED": "1", "AGENT_LAB_PG_PASSWORD": "secret-test-password"},
            "no fallback", connection_error=True)


if __name__ == "__main__":
    unittest.main()
