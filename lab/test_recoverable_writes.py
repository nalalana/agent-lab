"""Actual extracted services with isolated SQLite ORM and explicit SDK/IAM substitutes."""
import ast
import copy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from bootstrap import setup
from run_table_boundary import SOURCE, load_writer


def recoverable_writer(remote):
    writer = load_writer(remote)
    namespace = writer.upsert_table_cells.__globals__
    from apps.iam.models import Grant, Resource
    namespace.update(Resource=Resource, Grant=Grant)
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    helpers = {"RecoverableWriteUnknown", "_recoverable_scope", "_check_recoverable_receipt", "_finish_recoverable_write"}
    methods = {"submit_recoverable_write", "reconcile_committed_write"}
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    body.extend(n for n in tree.body if getattr(n, "name", None) in helpers)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "OpenGateService")
    cls.name, cls.body = "RecoverableMethods", [n for n in cls.body if getattr(n, "name", None) in methods]
    body.append(cls)
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(SOURCE), "exec"), namespace)
    for name in methods:
        setattr(writer, name, staticmethod(getattr(namespace["RecoverableMethods"], name)))
    writer._has_iam_permission = staticmethod(lambda *_: True)  # Explicit IAM substitute.
    return writer


class ReceiptFixture:
    """No cryptographic proof claim: tests PG intent/repair, not SDK verification."""
    def __init__(self):
        self.immudb_client = self
        self.values, self.receipts, self.calls, self.reads = {}, {}, 0, 0
        self.fault, self.read_fault = None, None

    def useDatabase(self, db):
        self.db = db.decode()

    def sqlQuery(self, query, params):
        target = query.split(' FROM ', 1)[1].split(' ', 1)[0]
        return [[v['cell_id']] for (db, tid, r, c), v in self.values.items()
                if db == self.db and tid == target and r == params['r'] and c == params['c']]

    def get_data(self, gid, tid, *_):
        rows = [r for db, table, r, _ in self.values if db == gid and table == tid]
        return True, {'max_row_index': max(rows, default=-1)}

    @staticmethod
    def canonical_hash(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                         ensure_ascii=False, allow_nan=False).encode()).hexdigest()

    def build_receipt(self, *, db_name, table_name, operation_id, scope_hash, request_hash, effect_plan):
        return {'version': 1, 'operation_id': operation_id, 'scope_hash': scope_hash,
                'request_hash': request_hash, 'db_name': db_name, 'table_name': table_name,
                'effect_hash': self.canonical_hash(effect_plan), 'operation': effect_plan['operation']}

    def execute_operation(self, client, **args):
        assert client is self
        self.calls += 1
        if self.fault == 'not_committed':
            raise TimeoutError('No receipt; may still be in flight')
        receipt = self.build_receipt(**args)
        key = (args['db_name'], args['operation_id'])
        if key in self.receipts:
            raise RuntimeError('unique receipt key violation')
        plan = args['effect_plan']
        values = copy.deepcopy(self.values)
        gid, tid = args['db_name'], args['table_name']
        for cell in plan.get('cells', []):
            coordinate = gid, tid, cell['r'], cell['c']
            if plan['operation'] == 'append_row' and coordinate in values:
                raise RuntimeError('append PK conflict')
            values[coordinate] = dict(cell)
        for cell in plan.get('coordinates', []):
            coordinate = gid, tid, cell['row_index'], cell['column_index']
            if coordinate in values:
                values[coordinate].update(ctt='g', cts='')
        if plan['operation'] == 'delete_row':
            values = {key: value for key, value in values.items()
                      if key[:2] != (gid, tid) or key[2] != plan['row_index']}
        self.values = values
        confirmation = {'tx_id': self.calls, 'receipt': receipt}
        self.receipts[key] = copy.deepcopy(confirmation)
        if self.fault == 'ack_lost':
            raise TimeoutError('Committed business and receipt; acknowledgement lost')
        return confirmation

    def read_operation_receipt(self, client, *, db_name, expected_receipt):
        assert client is self
        self.reads += 1
        if self.read_fault == 'exception':
            raise ValueError('Invalid proof')
        result = copy.deepcopy(self.receipts.get((db_name, expected_receipt['operation_id'])))
        if self.read_fault == 'mismatch' and result:
            result['receipt']['effect_hash'] = '0' * 64
        return result


class RecoverableWriteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        setup({'ENGINE': 'django.db.backends.sqlite3', 'NAME': str(Path(cls.directory.name) / 'orm.sqlite')})
        from django.db import connection
        from apps.opengate.models import Gates, GatesTable, APIKey, TableAuditEvent
        from apps.user.models import User
        from apps.iam.models import Tenant, Resource
        cls.Gate, cls.Table, cls.Audit, cls.User, cls.Tenant, cls.Resource = Gates, GatesTable, TableAuditEvent, User, Tenant, Resource
        with connection.schema_editor() as editor:
            for model in (User, Tenant, Resource, Gates, GatesTable, APIKey, TableAuditEvent):
                editor.create_model(model)

    @classmethod
    def tearDownClass(cls):
        from django.db import connections
        connections.close_all()
        cls.directory.cleanup()

    def setUp(self):
        self.user = self.User.objects.create(uid=uuid4().hex, username='Fixture', created_at='1', updated_at='1')
        self.tenant = self.Tenant.objects.create(type='personal', external_id=uuid4().hex, owner_user=self.user)
        self.gate = self.Gate.objects.create(gid='test_' + uuid4().hex, owner=self.user,
                                            gatename='Fixture', created_at='1', updated_at='1')
        self.project_resource = self.Resource.objects.create(type='project', external_id=self.gate.gid, tenant=self.tenant)
        self.table = self.new_table()
        self.remote = ReceiptFixture()
        self.writer = recoverable_writer(self.remote)
        self.ns = self.writer.submit_recoverable_write.__globals__
        self.Unknown = self.ns['RecoverableWriteUnknown']
        self.sdk_patch = patch.dict(sys.modules, {'common.immudbIO.recoverable': self.remote})
        self.sdk_patch.start()
        self.addCleanup(self.sdk_patch.stop)

    def new_table(self):
        table = self.Table.objects.create(tid='table' + uuid4().hex, gate=self.gate, name='Fixture',
                                          num_columns=2, created_at='1', updated_at='1')
        self.Resource.objects.create(type='table', external_id=table.tid, parent=self.project_resource, tenant=self.tenant)
        return table

    def version(self):
        return self.ns['_table_version'](self.table)

    def submit(self, operation='cells_upsert', **kwargs):
        if not kwargs and operation == 'cells_upsert':
            kwargs = {'cells': [{'r': 0, 'c': 0, 'value': 100}]}
        return self.writer.submit_recoverable_write(self.user, self.table, operation,
                                                   expected_table_version=self.version(), **kwargs)

    def reconcile(self, operation_id):
        return self.writer.reconcile_committed_write(self.user, self.table, operation_id)

    def pending(self):
        return self.Audit.objects.filter(table=self.table, action='write.fence', result='pending')

    def lose_ack(self, operation='cells_upsert', **kwargs):
        self.remote.fault = 'ack_lost'
        with self.assertRaises(self.Unknown) as error:
            self.submit(operation, **kwargs)
        self.remote.fault = None
        self.assertEqual(error.exception.code, 'OPERATION_OUTCOME_UNKNOWN')
        fence = self.pending().get()
        self.assertEqual(error.exception.operation_id, fence.details['operation_id'])
        self.assertEqual(self.version(), 0)
        return error.exception.operation_id

    def test_all_operation_shapes_commit_versions_and_preserve_raw_format(self):
        raw = {'r': '0', 'c': '0', 'ctfa': '0.00', 'ctt': 'f', 'cts': '=SUM(1,2)',
               'cell_id': 'client-id-is-not-used', 'bg': '#123456', 'tr': 90}
        first = self.submit('raw_upsert', values=raw)
        stored = self.remote.values[(self.gate.gid, self.table.tid, 0, 0)]
        self.assertEqual((stored['ctfa'], stored['ctt'], stored['cts'], stored['bg'], stored['tr']),
                         ('0.00', 'f', '=SUM(1,2)', '#123456', 90))
        self.assertEqual(stored['cell_id'], hashlib.sha256(f'{self.table.tid}:0:0'.encode()).hexdigest())
        self.assertEqual((first['status'], first['reconciled']), ('committed', False))
        for operation, args in [('cells_upsert', {'cells': [{'r': 0, 'c': 1, 'value': 200}]}),
                                ('clear', {'cells': [{'r': 0, 'c': 0}]}),
                                ('delete_row', {'row_index': 0}),
                                ('append_row', {'cells': [{'r': 0, 'c': 0, 'value': 1}, {'r': 0, 'c': 1, 'value': 2}],
                                                'expected_max_row_index': -1})]:
            previous = self.version()
            result = self.submit(operation, **args)
            self.assertGreater(result['table_version'], previous)
            self.assertEqual(result['table_version'], self.version())
        self.assertEqual(self.pending().count(), 0)
        self.assertEqual(self.remote.calls, 5)
        self.assertEqual(self.Audit.objects.filter(table=self.table, result='success').count(), 11)
        # Saved intent has hashes/audit coordinates, never business cell plaintext.
        intents = list(self.Audit.objects.filter(table=self.table, action='write.fence').values_list('details', flat=True))
        self.assertNotIn('SUM(1,2)', json.dumps(intents))

    def test_ack_loss_reconcile_is_positive_only_idempotent_and_never_dispatches(self):
        operation_id = self.lose_ack()
        calls = self.remote.calls
        with self.assertRaises(Exception):
            self.writer.upsert_table_cells(self.user, self.table, [{'r': 0, 'c': 0, 'value': 999}])
        result = self.reconcile(operation_id)
        self.assertEqual((result['status'], result['reconciled']), ('committed', True))
        self.assertGreater(result['table_version'], 0)
        events, reads = self.Audit.objects.filter(table=self.table).count(), self.remote.reads
        self.assertEqual(self.reconcile(operation_id), result)
        self.assertEqual((self.Audit.objects.filter(table=self.table).count(), self.remote.calls, self.remote.reads),
                         (events, calls, reads))
        self.assertEqual(self.pending().count(), 0)
        with self.assertRaises(self.ns['TableWriteConflict']):
            self.writer.submit_recoverable_write(self.user, self.table, 'delete_row', expected_table_version=0, row_index=0)
        self.assertEqual(self.remote.calls, calls)
        self.assertGreater(self.submit()['table_version'], result['table_version'])

    def test_intent_is_visible_to_independent_connection_before_dispatch_and_survives_termination(self):
        from django.db import connection
        def terminate(_client, **_):
            with sqlite3.connect(connection.settings_dict['NAME']) as independent:
                rows = independent.execute('SELECT result, details FROM opengate_table_audit_event WHERE table_id=?',
                                           (self.table.pk,)).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0][0], json.loads(rows[0][1])['recoverable_version']), ('pending', 1))
            raise SystemExit(71)
        with patch.object(self.remote, 'execute_operation', side_effect=terminate), self.assertRaises(SystemExit):
            self.submit()
        fence = self.pending().get()
        with self.assertRaises(self.Unknown):
            self.reconcile(fence.details['operation_id'])
        self.assertEqual((self.remote.calls, self.version(), self.pending().count()), (0, 0, 1))
        # All legacy entries share this fence, including a fresh writer/run.
        fresh = recoverable_writer(self.remote)
        for mutation in (
            lambda: fresh.upsert_table_data_full(self.user, self.table, {'r': 0, 'c': 0, 'cts': '9'}),
            lambda: fresh.upsert_table_cells(self.user, self.table, [{'r': 0, 'c': 0, 'value': 9}]),
            lambda: fresh.clear_table_cells(self.user, self.table, [{'r': 0, 'c': 0}]),
            lambda: fresh.delete_table_row(self.user, self.table, 0),
        ):
            with self.assertRaises(self.ns['UnconfirmedWriteError']):
                mutation()
        ok, result = fresh.get_table_data(self.user, self.table, None, None, 20, 0)
        self.assertFalse(ok)
        self.assertFalse(isinstance(result, dict) and 'table_version' in result)
        self.assertEqual(self.remote.calls, 0)

    def test_all_mutation_receipts_can_repair_correct_audit_actions(self):
        cases = [('raw_upsert', {'values': {'r': 0, 'c': 0, 'cts': '100'}}, 'data.upsert'),
                 ('clear', {'cells': [{'r': 0, 'c': 0}]}, 'cell.clear'),
                 ('delete_row', {'row_index': 0}, 'row.delete'),
                 ('append_row', {'cells': [{'r': 0, 'c': 0, 'value': 1}, {'r': 0, 'c': 1, 'value': 2}],
                                 'expected_max_row_index': -1}, 'cell.upsert')]
        for operation, args, action in cases:
            with self.subTest(operation=operation):
                self.table = self.new_table()
                opid = self.lose_ack(operation, **args)
                calls = self.remote.calls
                result = self.reconcile(opid)
                self.assertEqual(self.remote.calls, calls)
                event = self.Audit.objects.filter(table=self.table, action=action, result='success').first()
                self.assertIsNotNone(event)
                self.assertTrue(event.details['request_committed'])
                self.assertNotIn('cleared_cells', event.details)
                self.assertEqual(result['table_version'], self.version())

    def test_absent_receipt_inflight_invalid_proof_and_legacy_pending_stay_unknown(self):
        self.remote.fault = 'not_committed'
        with self.assertRaises(self.Unknown) as caught:
            self.submit()
        operation_id = caught.exception.operation_id
        for fault in (None, 'exception'):
            self.remote.read_fault = fault
            with self.assertRaises(self.Unknown):
                self.reconcile(operation_id)
        self.assertEqual((self.pending().count(), self.version(), self.remote.calls), (1, 0, 1))
        self.table = self.new_table()
        self.Audit.objects.create(table=self.table, actor=self.user, source='web', action='write.fence',
                                  details={'operation': 'cell.upsert'})
        reads = self.remote.reads
        with self.assertRaises(self.Unknown):
            self.reconcile(uuid4().hex)
        self.assertEqual((self.pending().count(), self.remote.reads), (1, reads))

    def test_receipt_scope_request_or_effect_mismatch_never_releases(self):
        operation_id = self.lose_ack()
        self.remote.read_fault = 'mismatch'
        with self.assertRaises(self.Unknown):
            self.reconcile(operation_id)
        self.remote.read_fault = None
        fence = self.pending().get()
        original = copy.deepcopy(fence.details)
        for key in ('scope_hash', 'request_hash', 'effect_hash', 'db_name', 'table_name', 'operation'):
            details = copy.deepcopy(original)
            details['expected_receipt'][key] = 'mismatch'
            fence.details = details
            fence.save(update_fields=['details'])
            with self.subTest(key=key), self.assertRaises(self.Unknown):
                self.reconcile(operation_id)
            self.assertEqual((self.pending().count(), self.version()), (1, 0))
        fence.details = original
        fence.save(update_fields=['details'])
        self.assertEqual(self.reconcile(operation_id)['status'], 'committed')

    def test_pg_failure_after_remote_commit_or_during_repair_keeps_durable_intent(self):
        finish = self.ns['_finish_table_audit']
        with patch.dict(self.ns, {'_finish_table_audit': lambda *_: (_ for _ in ()).throw(RuntimeError('PG save failed'))}):
            with self.assertRaises(self.Unknown) as caught:
                self.submit()
        operation_id = caught.exception.operation_id
        self.assertEqual((self.pending().count(), self.version(), self.remote.calls), (1, 0, 1))
        self.assertEqual(len(self.remote.receipts), 1)
        def fail_fence(event, success):
            if event.action == 'write.fence':
                raise RuntimeError('PG fence completion failed')
            finish(event, success)
        with patch.dict(self.ns, {'_finish_table_audit': fail_fence}), self.assertRaises(self.Unknown):
            self.reconcile(operation_id)
        self.assertEqual((self.pending().count(), self.version()), (1, 0))
        self.assertEqual(self.Audit.objects.filter(table=self.table).count(), 1)
        self.assertEqual(self.reconcile(operation_id)['status'], 'committed')
        self.assertEqual((self.remote.calls, self.Audit.objects.filter(table=self.table).count()), (1, 2))

    def test_lost_pg_prepare_commit_confirmation_exposes_server_id_without_dispatch(self):
        from django.db import transaction
        atomic = transaction.atomic
        @contextmanager
        def lose_confirmation(*args, **kwargs):
            with atomic(*args, **kwargs):
                yield
            if kwargs.get('durable'):
                raise RuntimeError('PG intent committed; local confirmation lost')
        with patch.object(transaction, 'atomic', lose_confirmation), self.assertRaises(self.Unknown) as caught:
            self.submit()
        self.assertEqual(self.pending().get().details['operation_id'], caught.exception.operation_id)
        self.assertEqual(self.remote.calls, 0)
        with self.assertRaises(self.Unknown):
            self.reconcile(caught.exception.operation_id)
        self.assertEqual((self.pending().count(), self.version(), self.remote.calls), (1, 0, 0))

    def test_authorization_and_cross_table_actor_tenant_binding(self):
        operation_id = self.lose_ack()
        reads = self.remote.reads
        with patch.object(self.writer, '_has_iam_permission', return_value=False), self.assertRaises(PermissionError):
            self.reconcile(operation_id)
        other = self.new_table()
        with self.assertRaises(self.Unknown):
            self.writer.reconcile_committed_write(self.user, other, operation_id)
        uid = self.user.uid
        self.user.uid = 'changed-actor'
        self.user.save(update_fields=['uid'])
        with self.assertRaises(self.Unknown):
            self.reconcile(operation_id)
        self.user.uid = uid
        self.user.save(update_fields=['uid'])
        resource = self.Resource.objects.get(type='table', external_id=self.table.tid)
        resource.tenant = self.Tenant.objects.create(type='personal', external_id=uuid4().hex)
        resource.save(update_fields=['tenant'])
        with self.assertRaises(PermissionError):
            self.reconcile(operation_id)
        self.assertEqual((self.pending().count(), self.version(), self.remote.reads), (1, 0, reads))

    def test_preflight_permissions_atomic_and_stale_version_have_zero_mutation_or_intent(self):
        error = self.ns['TableCellValidationError']
        invalid = [('raw_upsert', {'values': {'r': 0, 'c': 0, 'cts': ''}}),
                   ('cells_upsert', {'cells': [{'r': 0, 'c': 0, 'value': 1}, {'r': 0, 'c': 1, 'value': ''}]}),
                   ('clear', {'cells': [{'r': -1, 'c': 0}]}),
                   ('delete_row', {'row_index': True}),
                   ('append_row', {'cells': [{'r': 0, 'c': 0, 'value': 1}], 'expected_max_row_index': -1})]
        for operation, args in invalid:
            with self.subTest(operation=operation), self.assertRaises(error):
                self.submit(operation, **args)
        with self.assertRaises(error):
            self.writer.submit_recoverable_write(self.user, self.table, 'delete_row',
                                                 expected_table_version=None, row_index=0)
        with patch.object(self.writer, 'can_write_table', return_value=False), self.assertRaises(PermissionError):
            self.submit()
        from django.db import transaction
        with transaction.atomic(), self.assertRaises(self.ns['TableWriteDurabilityError']):
            self.submit()
        with self.assertRaises(self.ns['TableWriteConflict']):
            self.writer.submit_recoverable_write(self.user, self.table, 'delete_row', expected_table_version=1, row_index=0)
        factory = Mock(side_effect=AssertionError('Client/login RPC before local scope validation'))
        self.Resource.objects.filter(type='table', external_id=self.table.tid).update(external_id='missing_' + uuid4().hex)
        with patch.dict(self.ns, {'ImmudbService': factory}), self.assertRaises(PermissionError):
            self.submit()
        factory.assert_not_called()
        self.assertEqual((self.remote.calls, self.Audit.objects.filter(table=self.table).count()), (0, 0))


if __name__ == '__main__':
    unittest.main(verbosity=2)
