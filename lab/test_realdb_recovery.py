"""Offline extraction/fault/oracle contracts; no credentials, DB connections, or models."""
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
from recovery_fixture import RecoveryClient, load_recovery_writer, logical_transaction
from run_realdb_recovery import CASE_IDS, FAMILIES, attempt, run


def transaction():
    return ("BEGIN TRANSACTION;\nUPSERT INTO t_fixture (cell_id) VALUES (@cell_0_cell_id);\n"
        "INSERT INTO agent_operation_receipts_v1 (opid,receipt) VALUES (@receipt_opid,@receipt_payload);\nCOMMIT;",
        {"receipt_opid": "a" * 32, "receipt_payload": json.dumps({"operation_id": "a" * 32})})


class RecoveryOracleTests(unittest.TestCase):
    def test_only_the_pinned_complete_receipt_transaction_arms_faults(self):
        sql, params = transaction()
        self.assertTrue(logical_transaction(sql, params))
        for query, arguments in (("CREATE TABLE agent_operation_receipts_v1 (opid VARCHAR);", params),
                (sql.removesuffix("COMMIT;"), params), (sql, {**params, "receipt_opid": "b" * 32}),
                (sql.replace("UPSERT INTO t_fixture", "SELECT * FROM t_fixture"), params),
                ("UPSERT INTO t_fixture (a) VALUES (1);", {})):
            self.assertFalse(logical_transaction(query, arguments))

    def test_lost_ack_is_after_one_real_sdk_completed_ack_and_ddl_does_not_consume_fault(self):
        client = Mock()
        client.sqlExec.return_value = SimpleNamespace(ongoingTx=False, txs=[SimpleNamespace(header=SimpleNamespace(id=9))])
        proxy = RecoveryClient(client)
        proxy.fault = "ack_lost"
        proxy.sqlExec("CREATE TABLE fixture (id INTEGER);")
        self.assertEqual(proxy.fault, "ack_lost")
        sql, params = transaction()
        with self.assertRaises(TimeoutError):
            proxy.sqlExec(sql, params)
        self.assertEqual(client.sqlExec.call_count, 2)
        self.assertEqual(proxy.business_mutation_calls, 1)
        self.assertEqual(proxy.confirmed_transactions, [{"operation_id": "a" * 32, "tx_id": 9}])
        proxy.fault = "ack_lost"
        client.sqlExec.return_value = SimpleNamespace(ongoingTx=True, txs=[])
        with self.assertRaises(AssertionError):
            proxy.sqlExec(sql, params)
        self.assertEqual(len(proxy.confirmed_transactions), 1)

    def test_exit_hook_is_only_reached_after_sdk_commit(self):
        sql, params = transaction()
        client = Mock()
        client.sqlExec.return_value = SimpleNamespace(ongoingTx=False, txs=[SimpleNamespace(header=SimpleNamespace(id=10))])
        proxy = RecoveryClient(client)
        proxy.fault = "exit"
        with patch("recovery_fixture.os._exit", side_effect=SystemExit(73)) as exit_process:
            with self.assertRaises(SystemExit):
                proxy.sqlExec(sql, params)
        exit_process.assert_called_once_with(73)
        client.sqlExec.assert_called_once()
        self.assertEqual(proxy.confirmed_transactions[0]["tx_id"], 10)

    def test_pre_dispatch_and_tamper_fixtures_are_explicit_and_separately_counted(self):
        sql, params = transaction()
        client = Mock()
        client.sqlExec.return_value = SimpleNamespace(ongoingTx=False, txs=[SimpleNamespace(header=SimpleNamespace(id=11))])
        proxy = RecoveryClient(client)
        proxy.fault = "before_dispatch"
        with self.assertRaises(TimeoutError):
            proxy.sqlExec(sql, params)
        client.sqlExec.assert_not_called()
        self.assertEqual(proxy.total_rpc, 0)
        proxy.fault = "operation_id_mismatch"
        proxy.sqlExec(sql, params)
        sent = client.sqlExec.call_args.args[1]
        self.assertNotEqual(json.loads(sent["receipt_payload"])["operation_id"], sent["receipt_opid"])
        self.assertEqual(proxy.last_injection, "operation_id_mismatch")
        self.assertEqual(proxy.receipt_reads, 0)

    def test_no_arbitrary_exception_or_boolean_is_a_verified_commit(self):
        for value, unknown in ((UnconfirmedWriteError(), True), (RuntimeError("committed; blocked"), False),
                              (False, False), ({"status": "unknown"}, False)):
            fn = Mock(side_effect=value) if isinstance(value, BaseException) else Mock(return_value=value)
            result = attempt(fn)
            self.assertFalse(result["committed"])
            self.assertEqual(result["unknown"], unknown)
            fn.assert_called_once()

    def test_extractor_exposes_unsupported_baseline_and_counts_constructed_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "service.py"
            source.write_text("class OpenGateService:\n    pass\n")
            with patch("recovery_fixture.SOURCE", source):
                baseline = load_recovery_writer(SimpleNamespace())
            self.assertFalse(hasattr(baseline, "submit_recoverable_write"))
            source.write_text("class OpenGateService:\n"
                "    def submit_recoverable_write(*args, **kwargs): return ImmudbService()\n"
                "    def reconcile_committed_write(*args, **kwargs): return ImmudbService()\n")
            rpc = RecoveryClient(Mock())
            remote = SimpleNamespace(immudb_client=rpc)
            with patch("recovery_fixture.SOURCE", source):
                writer = load_recovery_writer(remote)
            self.assertIs(writer.submit_recoverable_write(), remote)
            self.assertIs(writer.reconcile_committed_write(), remote)
            self.assertEqual((rpc.service_constructions, rpc.total_rpc), (2, 0))

    def test_twenty_four_fixed_leaves_preserve_failures_or_explicit_unsupported(self):
        self.assertEqual((len(FAMILIES), len(CASE_IDS), len(set(CASE_IDS))), (12, 24, 24))
        for unsupported in (False, True):
            with self.subTest(unsupported=unsupported), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "output"
                def integration_run(folder, _config, **kwargs):
                    self.assertEqual(kwargs["manifest_updates"]["case_ids"], CASE_IDS)
                    folder.mkdir()
                    (folder / "summary.json").write_text(json.dumps({"status": "passed", "checks_passed": 26,
                        "postgresql_lock_behavior_tested": True, "schema_removed": True}))
                    rows = [{"case": name, "family": name.split(":")[0],
                        "facts": {"sequence": i}, "checks": {"condition": False if unsupported else i != 2},
                        "passed": False if unsupported else i != 2,
                        "status": "unsupported" if unsupported else ("passed" if i != 2 else "failed")}
                        for i, name in enumerate(CASE_IDS)]
                    (folder / "recovery-trials.json").write_text(json.dumps(rows))
                    return 0
                with patch("run_realdb_recovery.integration.run", side_effect=integration_run), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(run(output, Path("unused-test-config.json")), 2 if unsupported else 1)
                summary = json.loads((output / "summary.json").read_text())
                rows = json.loads((output / "recovery-trials.json").read_text())
                self.assertEqual(summary["recovery_cases_completed"], 24)
                self.assertEqual(summary["recovery_cases_passed"], 0 if unsupported else 23)
                self.assertEqual(summary["recovery_cases_unsupported"], 24 if unsupported else 0)
                self.assertEqual(summary["checks_passed"], 2 if unsupported else 25)
                self.assertEqual(rows[-1]["facts"], {"sequence": 23})
                self.assertEqual(summary["status"], "unsupported" if unsupported else "failed")


if __name__ == "__main__":
    unittest.main()
