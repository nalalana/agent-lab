"""Cell CAS contracts with isolated SQLite ORM/remote, not real DB/model scores."""
import logging
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from bootstrap import setup
from run_table_boundary import load_writer
from test_legacy_write_fence import LegacyRemote


class CellVersionMutationTests(unittest.TestCase):
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
        self.user = self.User.objects.create(uid=uuid4().hex, username="Fixture", created_at="1", updated_at="1")
        self.gate = self.Gates.objects.create(gid="test_" + uuid4().hex, owner=self.user,
            gatename="Fixture", created_at="1", updated_at="1")
        self.table = self.new_table()
        self.remote = LegacyRemote(Path(self.directory.name) / (self.table.tid + ".sqlite"))
        self.writer = load_writer(self.remote)
        self.namespace = self.writer.upsert_table_cells.__globals__

    def new_table(self):
        return self.Table.objects.create(tid="t" + uuid4().hex, gate=self.gate, name="Fixture",
            num_columns=8, created_at="1", updated_at="1")

    def event(self, action, row, column=None, result="success"):
        return self.Audit.objects.create(table=self.table, actor=self.user, source="web", action=action,
            row_index=row, column_index=column, result=result)

    def versions(self, coordinates):
        return self.namespace["_cell_versions"](self.table, [{"r": r, "c": c} for r, c in coordinates])

    def test_stale_cell_only_write_is_blocked_after_raw_clear_and_row_delete_then_fresh_write_succeeds(self):
        for mutation, action in (("raw", "data.upsert"), ("clear", "cell.clear"), ("delete", "row.delete")):
            with self.subTest(mutation=mutation):
                self.table = self.new_table()
                ok, receipt = self.writer.upsert_table_cells(self.user, self.table,
                    [{"r": 0, "c": 0, "value": 100}], expected_table_version=0)
                self.assertTrue(ok)
                old_cell_version = receipt["cell_versions"]["0:0"]
                if mutation == "raw":
                    self.writer.upsert_table_data_full(self.user, self.table, {"r": 0, "c": 0, "cts": "200"},
                        expected_table_version=receipt["table_version"])
                elif mutation == "clear":
                    self.writer.clear_table_cells(self.user, self.table, [{"r": 0, "c": 0}],
                        expected_table_version=receipt["table_version"])
                else:
                    self.writer.delete_table_row(self.user, self.table, 0,
                        expected_table_version=receipt["table_version"])
                change = self.Audit.objects.get(table=self.table, action=action, result="success")
                before, calls = [tuple(r) for r in self.remote.values(self.table.tid)], self.remote.calls
                # Deliberately omit expected_table_version, as supported by old HTTP callers.
                with self.assertRaises(self.namespace["TableWriteConflict"]) as caught:
                    self.writer.upsert_table_cells(self.user, self.table,
                        [{"r": 0, "c": 0, "value": 999, "expected_version": old_cell_version}])
                self.assertEqual(caught.exception.code, "CELL_WRITE_CONFLICT")
                self.assertEqual(self.remote.calls, calls)
                self.assertEqual([tuple(r) for r in self.remote.values(self.table.tid)], before)
                self.assertEqual(self.versions([(0, 0)]), {(0, 0): change.pk})
                ok, fresh = self.writer.upsert_table_cells(self.user, self.table,
                    [{"r": 0, "c": 0, "value": 300, "expected_version": change.pk}])
                self.assertTrue(ok)
                self.assertGreater(fresh["cell_versions"]["0:0"], change.pk)
                self.assertEqual(self.remote.values(self.table.tid)[0]["value"], "300")
                ok, read = self.writer.get_table_data(self.user, self.table, None, None, 20, 0)
                self.assertTrue(ok)
                self.assertEqual(read["cell_versions"]["0:0"], fresh["cell_versions"]["0:0"])

    def test_coordinate_mutations_do_not_advance_other_columns_or_rows(self):
        other_column = self.event("cell.upsert", 0, 1)
        other_row = self.event("cell.upsert", 1, 0)
        self.event("cell.upsert", 0, 0)
        self.event("data.upsert", 0, 0)
        latest = self.event("cell.clear", 0, 0)
        self.assertEqual(self.versions([(0, 0), (0, 1), (1, 0), (1, 1)]),
            {(0, 0): latest.pk, (0, 1): other_column.pk, (1, 0): other_row.pk, (1, 1): 0})

    def test_row_tombstone_covers_requested_columns_and_recreation_uses_maximum(self):
        self.event("cell.upsert", 0, 0)
        other_row = self.event("data.upsert", 1, 0)
        self.event("row.delete", 0)
        deleted = self.event("row.delete", 0)
        recreated = self.event("data.upsert", 0, 1)
        self.assertEqual(self.versions([(0, 0), (0, 1), (0, 7), (1, 0), (1, 7)]),
            {(0, 0): deleted.pk, (0, 1): recreated.pk, (0, 7): deleted.pk,
             (1, 0): other_row.pk, (1, 7): 0})
        again = self.event("row.delete", 0)
        self.assertEqual(self.versions([(0, 0), (0, 1), (0, 7)]),
            {(0, 0): again.pk, (0, 1): again.pk, (0, 7): again.pk})

    def test_failed_pending_fence_and_unrelated_actions_never_advance_versions(self):
        first = self.event("cell.upsert", 0, 0)
        for action in ("cell.upsert", "data.upsert", "cell.clear", "row.delete"):
            self.event(action, 0, 0 if action != "row.delete" else None, "failed")
            self.event(action, 0, 0 if action != "row.delete" else None, "pending")
        self.event("write.fence", 0, 0)
        self.event("table.update", 0, 0)
        other_table = self.new_table()
        self.Audit.objects.create(table=other_table, actor=self.user, source="web", action="row.delete",
            row_index=0, result="success")
        self.assertEqual(self.versions([(0, 0), (0, 0), (0, 1), (1, 0)]),
            {(0, 0): first.pk, (0, 1): 0, (1, 0): 0})
        self.assertEqual(self.versions([]), {})


if __name__ == "__main__":
    unittest.main()
