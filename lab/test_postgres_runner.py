import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import psycopg2
from run_postgres_locks import run, config


class PostgresRunnerTests(unittest.TestCase):
    def test_explicit_remote_test_config_requires_tls(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / "local.json"
            values = {"AGENT_LAB_PG_ENABLED": "1", "AGENT_LAB_PG_PASSWORD": "secret-test-password",
                "AGENT_LAB_PG_HOST": "test.invalid", "AGENT_LAB_PG_PORT": "27785",
                "AGENT_LAB_PG_DATABASE": "test_agent", "AGENT_LAB_PG_USER": "test_user"}
            path.write_text(json.dumps(values))
            cfg = config(path)
            self.assertEqual((cfg["host"], cfg["port"], cfg["dbname"], cfg["user"]),
                ("test.invalid", 27785, "test_agent", "test_user"))
            self.assertEqual(cfg["sslmode"], "require")
            values["AGENT_LAB_PG_DATABASE"] = "production"
            path.write_text(json.dumps(values))
            with self.assertRaises(ValueError):
                config(path)

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
