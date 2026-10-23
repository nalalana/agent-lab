"""Extracted services + isolated SQLite ORM/remote and mocked SDK, no real DB/model."""
import ast
import hashlib
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from uuid import uuid4

from bootstrap import ROOT, setup
from durable_counter import transaction as remote_transaction
from run_table_boundary import SOURCE, RemoteFixture, load_writer


def legacy_writer(remote):
    """Extend the existing extractor locally; shared oracle files stay frozen."""
    writer = load_writer(remote)
    namespace = writer.upsert_table_cells.__globals__
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "OpenGateService")
    names = {"upsert_table_data_full", "delete_table_row"}
    cls.name, cls.body = "LegacyMethods", [n for n in cls.body if getattr(n, "name", None) in names]
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls]
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(SOURCE), "exec"), namespace)
    for name in names:
        setattr(writer, name, staticmethod(getattr(namespace["LegacyMethods"], name)))
    return writer


def sdk_delete_service(rpc):
    from common.immudbIO.client import toumingClient
    path = ROOT / "backend/common/immudbIO/services.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ImmudbService")
    cls.body = [n for n in cls.body if getattr(n, "name", None) == "delete_row"]
    namespace = {"toumingClient": toumingClient}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), str(path), "exec"), namespace)
    service = namespace["ImmudbService"]()
    service.immudb_client = rpc
    return service


class LegacyRemote(RemoteFixture):
    def __init__(self, path):
        super().__init__(path)
        self.payloads, self.reads = [], 0

    def upsert_data(self, *args):
        self.payloads.append(dict(args[2]))
        fault = self.fault
        if fault in {"false_after_write", "value_error_after_write"}:
            self.fault = None
        result = super().upsert_data(*args)
        if fault == "false_after_write":
            return False, "unconfirmed"
        if fault == "value_error_after_write":
            raise ValueError("response handling failed after the remote commit")
        return result

    def delete_row(self, gid, tid, row):
        self.calls += 1
        with remote_transaction(self.path) as db:
            count = db.execute("DELETE FROM cells WHERE tid=? AND r=?", (tid, row)).rowcount
        fault, self.fault = self.fault, None
        if fault == "delete_lost_ack":
            raise TimeoutError("delete committed before acknowledgement loss")
        return (True, count) if count else (False, "row absent")

    def get_data(self, gid, tid, limit, offset):
        self.reads += 1
        return True, {"data": [("id", tid, r["r"], r["c"], r["value"]) for r in self.values(tid)]}


class LegacyWriteFenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        setup({"ENGINE": "django.db.backends.sqlite3", "NAME": str(Path(cls.directory.name) / "test_orm.sqlite")})
        from django.db import connection
        from apps.opengate.models import Gates, GatesTable, APIKey, TableAuditEvent
        from apps.user.models import User
        cls.Gates, cls.Table, cls.Audit, cls.User = Gates, GatesTable, TableAuditEvent, User
        with connection.schema_editor() as editor:
            for model in (User, Gates, GatesTable, APIKey, TableAuditEvent):
                editor.create_model(model)
        cls.logger = logging.getLogger("isolated-table-boundary")
        cls.was_disabled, cls.logger.disabled = cls.logger.disabled, True

    @classmethod
    def tearDownClass(cls):
        from django.db import connections
        connections.close_all()
        cls.logger.disabled = cls.was_disabled
        cls.directory.cleanup()

    def setUp(self):
        from common.immudbIO.errors import UnconfirmedWriteError
        self.Unknown = UnconfirmedWriteError
        self.user = self.User.objects.create(uid=uuid4().hex, username="Fixture", created_at="1", updated_at="1")
        self.gate = self.Gates.objects.create(gid="test_" + uuid4().hex, owner=self.user,
            gatename="Fixture", created_at="1", updated_at="1")
        self.table = self.new_table()
        self.remote = LegacyRemote(Path(self.directory.name) / (self.table.tid + ".sqlite"))
        self.writer = legacy_writer(self.remote)
        self.ns = self.writer.upsert_table_cells.__globals__
        self.raw = {"r": 0, "c": 0, "cts": "100", "ctfa": "0.00", "ctt": "n", "bg": "#123456"}

    def new_table(self):
        return self.Table.objects.create(tid="t" + uuid4().hex, gate=self.gate, name="Fixture",
            num_columns=2, created_at="1", updated_at="1")

    def version(self):
        return self.ns["_table_version"](self.table)

    def mutate(self, kind, expected=None, writer=None):
        writer = writer or self.writer
        args = {"expected_table_version": expected}
        if kind == "raw":
            return writer.upsert_table_data_full(self.user, self.table, self.raw, **args)
        if kind == "delete":
            return writer.delete_table_row(self.user, self.table, 0, **args)
        if kind == "cells":
            return writer.upsert_table_cells(self.user, self.table, [{"r": 0, "c": 0, "value": 200}], **args)
        return writer.clear_table_cells(self.user, self.table, [{"r": 0, "c": 0}], **args)

    def assert_all_blocked(self):
        before, calls = [tuple(r) for r in self.remote.values(self.table.tid)], self.remote.calls
        fresh = legacy_writer(self.remote)
        for kind in ("raw", "delete", "cells", "clear"):
            for version in (0, self.version()):
                with self.assertRaises(self.Unknown):
                    self.mutate(kind, version, fresh)
        ok, result = fresh.get_table_data(self.user, self.table, None, None, 20, 0)
        self.assertFalse(ok)
        self.assertFalse(isinstance(result, dict) and "table_version" in result)
        self.assertEqual((self.remote.calls, [tuple(r) for r in self.remote.values(self.table.tid)]), (calls, before))

    def test_raw_preserves_full_payload_and_original_sdk_coordinate_id_semantics(self):
        from run_realdb_integration import load_service
        rpc = Mock()
        rpc.sqlQuery.return_value = []
        rpc.sqlExec.return_value = SimpleNamespace(txs=[1], ongoingTx=False)
        service = load_service(rpc)
        original = service.upsert_data
        service.upsert_data = Mock(wraps=original)
        writer = legacy_writer(service)
        payload = {**self.raw, "r": "0", "c": "1", "cell_id": "client-chosen-id", "ctt": "f",
            "cts": "=SUM(1,2)", "ff": "3", "fc": "#654321", "fs": 18, "bl": True,
            "it": True, "cl": False, "un": True, "vt": 1, "ht": 2, "tb": 2, "tr": 90}
        self.assertTrue(writer.upsert_table_data_full(self.user, self.table, payload, expected_table_version=0)[0])
        self.assertEqual(service.upsert_data.call_args.args[2], payload)
        params = rpc.sqlExec.call_args.args[1]
        for field in ("ctfa", "ctt", "cts", "bg", "ff", "fc", "fs", "bl", "it", "cl", "un", "vt", "ht", "tb", "tr"):
            self.assertEqual(params[field], payload[field])
        self.assertEqual(params["cell_id"], hashlib.sha256(f"{self.table.tid}:0:1".encode()).hexdigest())
        event = self.Audit.objects.get(table=self.table, action="data.upsert")
        self.assertEqual((event.row_index, event.column_index, event.result), (0, 1, "success"))
        self.assertEqual(writer.upsert_table_cells.__globals__["_table_version"](self.table), event.pk)
        rpc.sqlQuery.return_value = [["original-coordinate-id"]]
        self.assertTrue(writer.upsert_table_data_full(self.user, self.table, payload)[0])
        self.assertEqual(rpc.sqlExec.call_args.args[1]["cell_id"], "original-coordinate-id")

    def test_normal_raw_and_delete_advance_version_and_invalidate_other_entry_points(self):
        self.assertEqual(self.mutate("raw", 0), (True, "written"))
        first = self.version()
        self.assertGreater(first, 0)
        calls = self.remote.calls
        for kind in ("raw", "delete", "cells", "clear"):
            with self.assertRaises(self.ns["TableWriteConflict"]):
                self.mutate(kind, 0)
        self.assertEqual(self.remote.calls, calls)
        self.assertEqual(self.mutate("delete", first), (True, 1))
        self.assertGreater(self.version(), first)
        self.assertEqual(len(self.remote.values(self.table.tid)), 0)
        for kind in ("raw", "delete", "cells", "clear"):
            with self.assertRaises(self.ns["TableWriteConflict"]):
                self.mutate(kind, first)
        self.assertFalse(self.Audit.objects.filter(table=self.table, action="write.fence").exclude(result="success").exists())

    def test_raw_fallback_snapshot_is_taken_before_moderation_intervening_write(self):
        entered = []
        def moderate(**_):
            if not entered:
                entered.append(True)
                self.assertTrue(self.mutate("cells", 0)[0])
            return True, ""
        with patch.dict(self.ns, {"_moderate_opengate_texts": moderate}):
            with self.assertRaises(self.ns["TableWriteConflict"]):
                self.mutate("raw")
        self.assertEqual(self.remote.calls, 1)
        self.assertEqual(self.remote.values(self.table.tid)[0]["value"], "200")

    def test_legacy_unknowns_and_post_dispatch_value_error_block_all_four_paths(self):
        for fault in ("typed_lost_ack", "false_after_write", "value_error_after_write", "delete_lost_ack"):
            with self.subTest(fault=fault):
                self.table = self.new_table()
                self.remote.fault = None
                if fault == "delete_lost_ack":
                    self.mutate("raw", 0)
                self.remote.fault = fault
                with self.assertRaises(self.Unknown):
                    self.mutate("delete" if fault == "delete_lost_ack" else "raw", self.version())
                self.assertEqual(self.Audit.objects.filter(table=self.table, action="write.fence", result="pending").count(), 1)
                self.assert_all_blocked()

    def test_existing_pending_from_managed_cells_blocks_both_legacy_methods(self):
        self.remote.fault = "typed_lost_ack"
        with self.assertRaises(self.Unknown):
            self.mutate("cells", 0)
        self.assert_all_blocked()
        other = self.new_table()
        self.assertTrue(self.writer.upsert_table_data_full(self.user, other, self.raw, expected_table_version=0)[0])

    def test_preflight_permission_and_outer_transaction_refusals_have_zero_rpc_and_fence(self):
        invalid = [{**self.raw, "cts": ""}, {**self.raw, "ctt": "invalid"}, {**self.raw, "r": -1},
                   {**self.raw, "c": -1}, {**self.raw, "c": 2}, {**self.raw, "fs": 999}]
        for values in invalid:
            with self.assertRaises(self.ns["TableCellValidationError"]):
                self.writer.upsert_table_data_full(self.user, self.table, values)
        for row in (-1, True, "0"):
            with self.assertRaises(self.ns["TableCellValidationError"]):
                self.writer.delete_table_row(self.user, self.table, row)
        for kind in ("raw", "delete"):
            for version in (-1, True, "0"):
                with self.assertRaises(self.ns["TableCellValidationError"]):
                    self.mutate(kind, version)
        from django.db import transaction
        with transaction.atomic():
            for kind in ("raw", "delete"):
                with self.assertRaises(self.ns["TableWriteDurabilityError"]):
                    self.mutate(kind, 0)
        self.writer.can_write_table = staticmethod(lambda *_: False)
        self.assertFalse(self.mutate("raw")[0])
        self.assertFalse(self.mutate("delete")[0])
        self.assertEqual(self.remote.calls, 0)
        self.assertFalse(self.Audit.objects.filter(table=self.table).exists())

    def test_commit_failures_before_or_after_rpc_never_lose_durable_unknown(self):
        from django.db import DatabaseError, transaction
        for kind in ("raw", "delete"):
            for fail_commit in (1, 2):
                with self.subTest(kind=kind, fail_commit=fail_commit):
                    self.table = self.new_table()
                    if kind == "delete":
                        self.mutate("raw", 0)
                    calls = self.remote.calls
                    connection = transaction.get_connection()
                    original, attempts = connection.commit, []
                    def fail():
                        attempts.append(True)
                        if len(attempts) == fail_commit:
                            raise DatabaseError("injected commit failure")
                        return original()
                    with patch.object(connection, "commit", side_effect=fail):
                        with self.assertRaises(self.Unknown):
                            self.mutate(kind, self.version())
                    self.assertEqual(self.remote.calls - calls, int(fail_commit == 2))
                    pending = self.Audit.objects.filter(table=self.table, action="write.fence", result="pending").count()
                    self.assertEqual(pending, int(fail_commit == 2))
                    if pending:
                        self.assert_all_blocked()

    def test_original_sdk_delete_absence_and_lost_ack_are_conservative_unknown(self):
        for rows in ([], [["existing-id"]]):
            self.table = self.new_table()
            effects = []
            rpc = Mock()
            rpc.sqlQuery.return_value = rows
            def commit_then_lose(*_):
                effects.append("committed")
                raise TimeoutError("acknowledgement lost after the remote commit")
            rpc.sqlExec.side_effect = commit_then_lose
            writer = legacy_writer(sdk_delete_service(rpc))
            with self.assertRaises(self.Unknown):
                writer.delete_table_row(self.user, self.table, 0, expected_table_version=0)
            self.assertEqual(rpc.sqlExec.call_count, int(bool(rows)))
            self.assertEqual(effects, ["committed"] if rows else [])
            self.assertEqual(self.Audit.objects.filter(table=self.table, action="write.fence", result="pending").count(), 1)
            self.assertEqual(self.version(), 0)


if __name__ == "__main__":
    unittest.main()
