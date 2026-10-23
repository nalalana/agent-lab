"""Offline crash-oracle contracts; no process exit, database, or model calls."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bootstrap import setup
setup()
from run_realdb_crash import CASE_IDS, EXIT_CODES, CommitThenExit, child_run, launch_child, run


class ExitBoundary(BaseException):
    pass


class RealDatabaseCrashTests(unittest.TestCase):
    def test_exit_requires_actual_committed_sdk_result_and_sends_sql_once(self):
        for tool, query in (("company.table.write", "UPSERT INTO cells VALUES(1)"),
                             ("company.table.clear_cells", "UPDATE cells SET cts=''")):
            with self.subTest(tool=tool):
                rpc = Mock()
                rpc.sqlExec.return_value = SimpleNamespace(txs=[1], ongoingTx=False)
                proxy = CommitThenExit(rpc, tool)
                with patch("run_realdb_crash.os._exit", side_effect=ExitBoundary) as exit_process:
                    with self.assertRaises(ExitBoundary):
                        proxy.sqlExec(query, {"value": 1})
                rpc.sqlExec.assert_called_once_with(query, {"value": 1})
                exit_process.assert_called_once_with(EXIT_CODES[tool])
                for invalid in (None, SimpleNamespace(txs=[], ongoingTx=False), SimpleNamespace(txs=[1], ongoingTx=True)):
                    rpc.sqlExec.return_value = invalid
                    with patch("run_realdb_crash.os._exit") as exit_process:
                        with self.assertRaises(AssertionError):
                            proxy.sqlExec(query)
                        exit_process.assert_not_called()

    def test_child_command_has_only_paths_and_test_identity_and_keeps_logs_local(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            result = SimpleNamespace(returncode=71, stdout=b"local stdout", stderr=b"local stderr")
            with patch("run_realdb_crash.subprocess.run", return_value=result) as spawn:
                observed = launch_child(output, output / "config.json", "agent_lab_" + "a" * 32, 4, "company.table.write")
            command = spawn.call_args.args[0]
            self.assertIn(str((output / "config.json").resolve()), command)
            self.assertNotIn("--password", command)
            self.assertEqual(spawn.call_args.kwargs["timeout"], 30)
            self.assertEqual(spawn.call_args.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(observed, {"exit_code": 71, "expected_exit_code": 71, "timed_out": False})
            self.assertEqual((output / "write-child-stderr.log").read_bytes(), b"local stderr")
            with patch("run_realdb_crash.subprocess.run", side_effect=subprocess.TimeoutExpired(command, 30, output=b"partial")):
                observed = launch_child(output, output / "config.json", "agent_lab_" + "a" * 32, 4, "company.table.clear_cells")
            self.assertTrue(observed["timed_out"])
            self.assertIsNone(observed["exit_code"])
            self.assertEqual((output / "clear-child-stdout.log").read_bytes(), b"partial")

    def test_child_only_loads_existing_table_and_cannot_recreate_parent_resources(self):
        table = SimpleNamespace(tid="tfixture", gate=SimpleNamespace(gid="test_fixture", owner=SimpleNamespace(uid="u")))
        service, adapter = Mock(), Mock()
        writer = SimpleNamespace(upsert_table_cells=SimpleNamespace(__globals__={
            "TableWriteConflict": type("Conflict", (Exception,), {})}))
        pg = {"dbname": "test_fixture", "user": "fixture", "password": "fixture-password",
              "host": "127.0.0.1", "port": 55432, "sslmode": "require"}
        with patch("run_realdb_crash.integration.config", return_value={"address": "127.0.0.1:53322",
                "user": "fixture", "password": "fixture-password"}), \
             patch("run_realdb_crash.pg_config", return_value=pg), patch("run_realdb_crash.setup"), \
             patch("immudb.ImmudbClient"), patch("apps.opengate.models.GatesTable.objects") as tables, \
             patch("run_realdb_crash.integration.load_service", return_value=service), \
             patch("run_table_boundary.load_writer", return_value=writer), \
             patch("run_realdb_crash.managed_read", return_value={"has_version": True, "version": 0}), \
             patch("test_table_versions.project_adapter", return_value=adapter):
            tables.select_related.return_value.get.return_value = table
            with self.assertRaisesRegex(RuntimeError, "without the required"):
                child_run(Path("unused-local-config.json"), "agent_lab_" + "a" * 32, 7, "company.table.write")
            tables.select_related.return_value.get.assert_called_once_with(pk=7)
            tables.create.assert_not_called()
            service.create_db.assert_not_called()
            service.create_table.assert_not_called()
            adapter.assert_called_once()
        with patch("run_realdb_crash.integration.config") as config:
            with self.assertRaises(ValueError):
                child_run(Path("unused"), "public", 1, "company.table.write")
            config.assert_not_called()

    def test_failed_crash_safety_stays_failed_and_is_separate_from_ten_case_results(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result"
            def completed(folder, config, **kwargs):
                self.assertEqual(kwargs["manifest_updates"]["case_ids"], CASE_IDS)
                self.assertFalse(kwargs["manifest_updates"]["lab_sqlite_unknown_guard_enabled"])
                folder.mkdir()
                (folder / "summary.json").write_text(json.dumps({"status": "passed", "checks_passed": 4,
                    "postgresql_lock_behavior_tested": True, "schema_removed": True}))
                (folder / "crash-trials.json").write_text(json.dumps([
                    {"case": name, "facts": {"exit_code": 71 + i}, "checks": {"blocked": i == 0}, "passed": i == 0}
                    for i, name in enumerate(CASE_IDS)]))
                return 0
            with patch("run_realdb_crash.integration.run", side_effect=completed), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run(output, Path("unused-local-config.json")), 1)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual((summary["status"], summary["crash_cases_completed"], summary["crash_cases_passed"]),
                             ("failed", 2, 1))
            self.assertTrue(summary["separate_from_shared_ten_cases"])
            self.assertEqual(summary["checks_passed"], 3)
            self.assertFalse((output / "safety-trials.json").exists())


if __name__ == "__main__":
    unittest.main()
