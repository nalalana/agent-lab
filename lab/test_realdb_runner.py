"""Offline runner contracts only; these mocks are not database evidence."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bootstrap import setup
from run_realdb_integration import CommitThenLoseAcknowledgement, config, load_service, run


class RealDatabaseRunnerTests(unittest.TestCase):
    def values(self):
        return {"AGENT_LAB_PG_ENABLED": "1", "AGENT_LAB_PG_HOST": "127.0.0.1",
            "AGENT_LAB_PG_PORT": "55432", "AGENT_LAB_PG_DATABASE": "test_agent_lab",
            "AGENT_LAB_PG_USER": "fixture", "AGENT_LAB_PG_PASSWORD": "secret-pg-fixture",
            "AGENT_LAB_IMMUDB_ENABLED": "1", "AGENT_LAB_IMMUDB_HOST": "127.0.0.1",
            "AGENT_LAB_IMMUDB_PORT": "53322", "AGENT_LAB_IMMUDB_USER": "fixture",
            "AGENT_LAB_IMMUDB_PASSWORD": "secret-immudb-fixture"}

    def test_explicit_loopback_test_config_and_both_opt_ins_required(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / "local.json"
            values = self.values()
            path.write_text(json.dumps(values))
            self.assertEqual(config(path)["address"], "127.0.0.1:53322")
            path.write_text(json.dumps({**values, "AGENT_LAB_PG_DATABASE": "agent_lab_test"}))
            self.assertEqual(config(path)["address"], "127.0.0.1:53322")
            for key, value in (("AGENT_LAB_PG_HOST", "remote.invalid"),
                    ("AGENT_LAB_IMMUDB_HOST", "remote.invalid"),
                    ("AGENT_LAB_PG_DATABASE", "production"),
                    ("AGENT_LAB_IMMUDB_ENABLED", "0"), ("AGENT_LAB_IMMUDB_PORT", "0")):
                path.write_text(json.dumps({**values, key: value}))
                with self.assertRaises(ValueError):
                    config(path)
            with self.assertRaises(ValueError):
                config(None)

    def test_unavailable_immudb_is_sanitized_and_never_starts_pg_or_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local.json"
            path.write_text(json.dumps(self.values()))
            output = Path(directory) / "result"
            captured = io.StringIO()
            with patch("immudb.ImmudbClient") as constructor, patch("run_realdb_integration.run_locks") as locks:
                constructor.return_value.login.side_effect = RuntimeError("secret-immudb-fixture")
                with contextlib.redirect_stdout(captured):
                    self.assertEqual(run(output, path), 2)
                locks.assert_not_called()
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual((summary["status"], summary["checks_passed"]), ("unavailable", 0))
            self.assertFalse(summary["real_immudb"])
            self.assertNotIn("secret-", captured.getvalue())
            self.assertFalse((output / "manifest.json").exists())

    def test_real_service_write_lost_ack_is_unknown_without_sdk_retry(self):
        setup()
        from common.immudbIO.errors import UnconfirmedWriteError
        rpc = Mock()
        committed = SimpleNamespace(txs=[1], ongoingTx=False)
        rpc.sqlExec.return_value = committed
        rpc.sqlQuery.return_value = []
        proxy = CommitThenLoseAcknowledgement(rpc)
        proxy.armed = "UPSERT INTO "
        # A properties module import here would load production configuration.
        import builtins
        original_import = builtins.__import__
        def safe_import(name, *args, **kwargs):
            if name == "properties":
                raise AssertionError("Production properties import")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=safe_import):
            service = load_service(proxy)
            with self.assertRaises(UnconfirmedWriteError) as caught:
                service.upsert_data("test_fixture", "tfixture", {"r": 0, "c": 0, "cts": "100"})
        self.assertIsInstance(caught.exception.__cause__, TimeoutError)
        self.assertEqual((rpc.sqlExec.call_count, proxy.committed_then_lost), (1, 1))
        self.assertIsNone(proxy.armed)

    def test_real_clear_lost_ack_and_blank_second_cell_are_not_acknowledged(self):
        setup()
        rpc = Mock()
        rpc.sqlExec.return_value = SimpleNamespace(txs=[1], ongoingTx=False)
        rpc.sqlQuery.return_value = [["cell-id"]]
        proxy = CommitThenLoseAcknowledgement(rpc)
        service = load_service(proxy)
        proxy.armed = "UPDATE "
        ok, result = service.clear_cells("test_fixture", "tfixture", [{"r": 0, "c": 0}])
        self.assertFalse(ok)
        self.assertEqual(result["cleared_cells"], [])
        self.assertEqual((rpc.sqlExec.call_count, proxy.committed_then_lost), (1, 1))
        rpc.reset_mock()
        rpc.sqlQuery.return_value = []
        self.assertTrue(service.upsert_data("test_fixture", "tfixture", {"r": 0, "c": 0, "cts": "100"})[0])
        self.assertFalse(service.upsert_data("test_fixture", "tfixture", {"r": 0, "c": 1, "cts": ""})[0])
        self.assertEqual(rpc.sqlExec.call_count, 1)


if __name__ == "__main__":
    unittest.main()
