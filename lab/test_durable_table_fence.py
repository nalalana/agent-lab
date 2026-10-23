"""Offline ORM/independent SQLite contracts, not PostgreSQL/immudb evidence."""
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from bootstrap import ROOT, setup
from run_table_boundary import RemoteFixture, load_writer


class ReadableRemote(RemoteFixture):
    reads = 0

    def get_data(self, gid, tid, limit, offset):
        self.reads += 1
        rows = [("cell", tid, r["r"], r["c"], r["value"]) for r in self.values(tid)]
        return True, {"data": rows}


class DurableTableFenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.orm_path = Path(cls.directory.name) / "test_orm.sqlite"
        setup({"ENGINE": "django.db.backends.sqlite3", "NAME": str(cls.orm_path)})
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
        self.table = self.table_for("primary")
        self.remote = ReadableRemote(Path(self.directory.name) / (self.table.tid + ".sqlite"))
        self.writer = load_writer(self.remote)
        self.ns = self.writer.upsert_table_cells.__globals__
        self.cells = [{"r": 0, "c": 0, "value": 100}]

    def table_for(self, name):
        return self.Table.objects.create(tid="t" + uuid4().hex, gate=self.gate, name=name,
            num_columns=2, created_at="1", updated_at="1")

    def fences(self, table=None):
        return self.Audit.objects.filter(table=table or self.table, action="write.fence").order_by("id")

    def version(self):
        return self.ns["_table_version"](self.table)

    def write(self, version=0):
        return self.writer.upsert_table_cells(self.user, self.table, self.cells, expected_table_version=version)

    def assert_blocked(self):
        calls, reads = self.remote.calls, self.remote.reads
        fresh = load_writer(self.remote)
        for version in (0, self.version()):
            with self.assertRaises(self.Unknown):
                fresh.upsert_table_cells(self.user, self.table, self.cells, expected_table_version=version)
            with self.assertRaises(self.Unknown):
                fresh.clear_table_cells(self.user, self.table, [{"r": 0, "c": 0}], expected_table_version=version)
        ok, result = fresh.get_table_data(self.user, self.table, None, None, 20, 0)
        self.assertFalse(ok)
        self.assertFalse(isinstance(result, dict) and "table_version" in result)
        self.assertEqual((self.remote.calls, self.remote.reads), (calls, reads))

    def test_success_versions_exclude_fence_and_clear_completes_its_fence(self):
        ok, receipt = self.write()
        self.assertTrue(ok)
        self.assertEqual(list(self.fences().values_list("result", flat=True)), ["success"])
        self.assertEqual(self.version(), receipt["table_version"])
        # A later completed fence is metadata, never a cell/table revision.
        self.Audit.objects.create(table=self.table, actor=self.user, source="web",
            action="write.fence", result="success")
        self.assertEqual(self.version(), receipt["table_version"])
        ok, data = self.writer.get_table_data(self.user, self.table, None, None, 20, 0)
        self.assertTrue(ok)
        self.assertEqual(data["table_version"], receipt["table_version"])
        ok, count = self.writer.clear_table_cells(self.user, self.table, [{"r": 0, "c": 0}],
            expected_table_version=receipt["table_version"])
        self.assertEqual((ok, count), (True, 1))
        self.assertGreater(self.version(), receipt["table_version"])
        self.assertFalse(self.fences().exclude(result="success").exists())
        self.assertEqual(self.remote.values(self.table.tid)[0]["value"], "")

    def test_write_unknown_survives_rollback_blocks_new_writer_and_other_table_is_usable(self):
        self.remote.fault = "typed_lost_ack"
        with self.assertRaises(self.Unknown):
            self.write()
        self.assertEqual(self.remote.values(self.table.tid)[0]["value"], "100")
        self.assertEqual(self.version(), 0)
        self.assertEqual(list(self.fences().values_list("result", flat=True)), ["pending"])
        self.assertEqual(self.Audit.objects.filter(table=self.table).count(), 1)
        self.assert_blocked()
        other = self.table_for("unrelated")
        self.assertTrue(self.writer.upsert_table_cells(self.user, other, self.cells, expected_table_version=0)[0])

    def test_clear_false_keeps_pending_even_with_failed_cell_audit(self):
        _, receipt = self.write()
        self.remote.fault = "lost_ack"
        ok, _ = self.writer.clear_table_cells(self.user, self.table, [{"r": 0, "c": 0}],
            expected_table_version=receipt["table_version"])
        self.assertFalse(ok)
        self.assertEqual(self.remote.values(self.table.tid)[0]["value"], "")
        self.assertEqual(self.version(), receipt["table_version"])
        self.assertEqual(list(self.fences().values_list("result", flat=True)), ["success", "pending"])
        self.assertEqual(list(self.Audit.objects.filter(table=self.table, action="cell.clear").values_list("result", flat=True)), ["failed"])
        self.assert_blocked()

    def test_partial_false_keeps_fence_even_when_successful_cell_advances_version(self):
        self.remote.fault = "second_rejected"
        ok, _ = self.writer.upsert_table_cells(self.user, self.table,
            self.cells + [{"r": 0, "c": 1, "value": 200}], expected_table_version=0)
        self.assertFalse(ok)
        self.assertEqual(len(self.remote.values(self.table.tid)), 1)
        self.assertGreater(self.version(), 0)
        self.assertEqual(list(self.fences().values_list("result", flat=True)), ["pending"])
        self.assert_blocked()

    def test_business_commit_failure_after_remote_success_preserves_precommitted_fence(self):
        from django.db import DatabaseError, transaction
        connection = transaction.get_connection()
        commit, commits = connection.commit, []
        def fail_second_commit():
            commits.append(1)
            if len(commits) == 2:
                raise DatabaseError("injected business commit failure")
            return commit()
        with patch.object(connection, "commit", side_effect=fail_second_commit):
            self.assertFalse(self.write()[0])
        self.assertEqual(len(commits), 2)
        self.assertEqual(self.remote.values(self.table.tid)[0]["value"], "100")
        self.assertEqual(self.version(), 0)
        self.assertEqual(list(self.fences().values_list("result", flat=True)), ["pending"])
        self.assert_blocked()

    def test_reservation_commit_failure_never_dispatches_remote(self):
        from django.db import DatabaseError, transaction
        with patch.object(transaction.get_connection(), "commit", side_effect=DatabaseError("injected reservation commit failure")):
            self.assertFalse(self.write()[0])
        self.assertEqual(self.remote.calls, 0)
        self.assertFalse(self.fences().exists())

    def test_outer_atomic_and_disabled_autocommit_are_rejected_before_fence_or_dispatch(self):
        from django.db import transaction
        durability_error = self.ns["TableWriteDurabilityError"]
        with transaction.atomic():
            with self.assertRaises(durability_error):
                self.write()
            with self.assertRaises(durability_error):
                self.writer.clear_table_cells(self.user, self.table, [{"r": 0, "c": 0}], expected_table_version=0)
            self.assertFalse(self.writer.get_table_data(self.user, self.table, None, None, 20, 0)[0])
        transaction.set_autocommit(False)
        try:
            with self.assertRaises(durability_error):
                self.write()
        finally:
            transaction.rollback()
            transaction.set_autocommit(True)
        self.assertEqual((self.remote.calls, self.remote.reads), (0, 0))
        self.assertFalse(self.fences().exists())

    def test_stale_version_and_denied_permissions_never_reserve_fence(self):
        self.write()
        count, calls = self.fences().count(), self.remote.calls
        with self.assertRaises(self.ns["TableWriteConflict"]):
            self.write(0)
        with self.assertRaises(self.ns["TableWriteConflict"]):
            self.writer.clear_table_cells(self.user, self.table, [{"r": 0, "c": 0}], expected_table_version=0)
        self.writer.can_write_table = staticmethod(lambda *_: False)
        self.assertFalse(self.write()[0])
        self.assertFalse(self.writer.clear_table_cells(self.user, self.table, [{"r": 0, "c": 0}])[0])
        self.assertEqual((self.fences().count(), self.remote.calls), (count, calls))

    def test_process_exit_after_remote_commit_leaves_durable_fence_for_fresh_process(self):
        script = """import logging,os,sys
from bootstrap import setup
setup({'ENGINE':'django.db.backends.sqlite3','NAME':sys.argv[1]})
from apps.opengate.models import GatesTable
from common.immudbIO.errors import UnconfirmedWriteError
from run_table_boundary import RemoteFixture,load_writer
logging.getLogger('isolated-table-boundary').disabled=True
class Remote(RemoteFixture):
 def upsert_data(self,*args):
  result=super().upsert_data(*args)
  if sys.argv[4]=='exit': os._exit(23)
  return result
remote=Remote(sys.argv[2]); writer=load_writer(remote)
table=GatesTable.objects.select_related('gate__owner').get(pk=int(sys.argv[3]))
user=table.gate.owner
if sys.argv[4]=='exit':
 writer.upsert_table_cells(user,table,[{'r':0,'c':0,'value':100}],expected_table_version=0)
 raise AssertionError('process did not exit')
for operation in ('write','clear'):
 try:
  if operation=='write': writer.upsert_table_cells(user,table,[{'r':0,'c':0,'value':200}],expected_table_version=0)
  else: writer.clear_table_cells(user,table,[{'r':0,'c':0}],expected_table_version=0)
 except UnconfirmedWriteError: pass
 else: raise AssertionError('pending write reached executor')
assert writer.get_table_data(user,table,None,None,20,0)[0] is False
assert remote.calls==0
print('blocked')
"""
        arguments = [str(self.orm_path), str(self.remote.path), str(self.table.pk)]
        result = subprocess.run([sys.executable, "-c", script, *arguments, "exit"], cwd=ROOT / "lab",
            capture_output=True, text=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(self.remote.values(self.table.tid)[0]["value"], "100")
        self.assertEqual(self.version(), 0)
        self.assertEqual(list(self.fences().values_list("result", flat=True)), ["pending"])
        result = subprocess.run([sys.executable, "-c", script, *arguments, "check"], cwd=ROOT / "lab",
            capture_output=True, text=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "blocked")
        self.assert_blocked()


if __name__ == "__main__":
    unittest.main()
