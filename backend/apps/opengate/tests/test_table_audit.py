import json
from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import get_or_create_principal, get_or_create_resource, grant_permissions
from apps.opengate.models import Gates, GatesTable, TableAuditEvent
from apps.opengate.services import OpenGateService
from apps.user.models import User
from common.utils.Login_utils import make_api_bearer_token, sign_token
from common.immudbIO.services import ImmudbService


class TableAuditTests(TestCase):
    @patch('common.immudbIO.services.toumingClient')
    def test_data_pagination_uses_max_row_not_cell_count(self, client_cls):
        service = object.__new__(ImmudbService)
        service.immudb_client = MagicMock()
        client_cls.return_value.simpleSelect.side_effect = [[], [[6, 240]], [], [[0, 0]]]

        ok, first_page = service.get_data('project', 'table', 200, 0)
        self.assertTrue(ok)
        self.assertEqual(first_page['total'], 6)
        self.assertEqual(first_page['max_row_index'], 240)
        self.assertTrue(first_page['has_more'])

        ok, empty_page = service.get_data('project', 'table', 200, 1)
        self.assertTrue(ok)
        self.assertEqual(empty_page['max_row_index'], -1)
        self.assertFalse(empty_page['has_more'])

    @patch('common.immudbIO.services.toumingClient')
    def test_delete_row_targets_one_physical_row(self, client_cls):
        service = object.__new__(ImmudbService)
        service.immudb_client = MagicMock()
        client_cls.return_value.simpleSelect.return_value = [['cell-a'], ['cell-b']]
        service.immudb_client.sqlExec.return_value.txs = [object()]

        ok, count = service.delete_row('project', 'table', 7)
        self.assertTrue(ok)
        self.assertEqual(count, 2)
        service.immudb_client.sqlExec.assert_called_once_with(
            'DELETE FROM table WHERE r=@r;', {'r': 7},
        )

    @patch('common.immudbIO.services.toumingClient')
    def test_upsert_does_not_report_retry_exhaustion_as_success(self, client_cls):
        service = object.__new__(ImmudbService)
        service.immudb_client = MagicMock()
        client_cls.return_value.simpleSelect.return_value = []
        client_cls.return_value.upsertToTable.return_value = (False, 'tx read conflict')
        ok, message = service.upsert_data('project', 'table', {'r': 2, 'c': 0, 'cts': 'value'})
        self.assertFalse(ok)
        self.assertEqual(message, 'tx read conflict')

    def test_new_row_cells_are_inserted_in_one_transaction(self):
        service = object.__new__(ImmudbService)
        service.immudb_client = MagicMock()
        service.immudb_client.sqlExec.return_value.txs = [object()]
        service.immudb_client.sqlExec.return_value.ongoingTx = False
        ok, _ = service.insert_row('project', 'table', [
            {'r': 2, 'c': 0, 'cts': 'one'},
            {'r': 2, 'c': 1, 'cts': 'two'},
        ])
        self.assertTrue(ok)
        query, params = service.immudb_client.sqlExec.call_args.args
        self.assertTrue(query.startswith('BEGIN TRANSACTION;'))
        self.assertTrue(query.endswith('COMMIT;'))
        self.assertEqual(query.count('INSERT INTO table'), 2)
        self.assertNotEqual(params['cell_id_0'], params['cell_id_1'])

    @patch('apps.opengate.services.ImmudbService')
    def test_row_delete_requires_write_and_records_operation(self, immudb_cls):
        path = '/api/opengate/tables/data/rows/delete'
        self.assertEqual(self.post(path, {'tid': self.table.tid, 'r': 7}).status_code, 401)
        outsider = {'HTTP_AUTHORIZATION': f"Bearer {sign_token({'user_id': self.outsider.id})}"}
        self.assertEqual(self.post(path, {'tid': self.table.tid, 'r': 7}, outsider).status_code, 403)
        self.assertEqual(self.post(path, {'tid': self.table.tid, 'r': -1}, self.headers).status_code, 400)
        self.assertEqual(TableAuditEvent.objects.count(), 0)

        immudb_cls.return_value.delete_row.return_value = (True, 2)
        response = self.post(path, {'tid': self.table.tid, 'r': 7}, self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['deleted_cells'], 2)
        event = TableAuditEvent.objects.get()
        self.assertEqual((event.action, event.source, event.result), ('row.delete', 'web', 'success'))
        self.assertEqual((event.row_index, event.details['deleted_cells']), (7, 2))

        immudb_cls.return_value.delete_row.return_value = (False, '行不存在或已删除')
        response = self.post(path, {'tid': self.table.tid, 'r': 7}, self.headers)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(TableAuditEvent.objects.filter(action='row.delete', result='failed').count(), 1)

    def setUp(self):
        self.user = User.objects.create(uid='table-audit-user', username='Table Editor', created_at='1', updated_at='1')
        self.outsider = User.objects.create(uid='table-audit-outsider', username='Outsider', created_at='1', updated_at='1')
        self.gate = Gates.objects.create(gid='table-audit-project', owner=self.user, gatename='Project', created_at='1', updated_at='1')
        self.table = GatesTable.objects.create(
            tid='table-audit-table', gate=self.gate, name='Records', num_columns=2,
            columns_config=[{'field_name': 'A', 'title': 'Name'}, {'field_name': 'B', 'title': 'Age'}],
            created_at='1', updated_at='1',
        )
        principal = get_or_create_principal(Principal.TYPE_USER, self.user.uid)
        project = get_or_create_resource(Resource.TYPE_PROJECT, self.gate.gid)
        table_resource = get_or_create_resource(Resource.TYPE_TABLE, self.table.tid, parent=project)
        grant_permissions(principal, project, Grant.PERM_ADMIN, granter_user=self.user)
        grant_permissions(principal, table_resource, Grant.PERM_SUPER_ADMIN, granter_user=self.user)
        self.headers = {'HTTP_AUTHORIZATION': f"Bearer {sign_token({'user_id': self.user.id})}"}

    def post(self, path, body, headers=None):
        return self.client.post(path, json.dumps(body), content_type='application/json', **(headers or {}))

    @patch('apps.opengate.services._moderate_opengate_texts', return_value=(True, ''))
    @patch('apps.opengate.services.ImmudbService')
    def test_web_cell_edits_and_partial_failure_are_audited(self, immudb_cls, _moderation):
        immudb_cls.return_value.upsert_data.side_effect = [(True, 'written'), (False, 'failed')]
        response = self.post('/api/opengate/tables/data/upsert', {
            'tid': self.table.tid,
            'values': [{'r': 7, 'c': 0, 'value': 'Alice'}, {'r': 7, 'c': 1, 'value': 18}],
        }, self.headers)
        self.assertEqual(response.status_code, 403)
        events = list(TableAuditEvent.objects.order_by('id'))
        self.assertEqual([event.result for event in events], ['success', 'failed'])
        self.assertEqual([(event.row_index, event.column_index) for event in events], [(7, 0), (7, 1)])
        self.assertTrue(all(event.source == 'web' and event.actor == self.user for event in events))
        self.assertNotIn('Alice', json.dumps(events[0].details))

    @patch('apps.opengate.services._moderate_opengate_texts', return_value=(True, ''))
    @patch('apps.opengate.services.ImmudbService')
    def test_api_key_edit_records_key_name_and_actor(self, immudb_cls, _moderation):
        immudb_cls.return_value.upsert_data.return_value = (True, 'written')
        ok, key_data = OpenGateService.create_gate_key(self.user, self.gate, 'nightly sync')
        self.assertTrue(ok)
        response = self.post('/api/opengate/app/tables/upsert', {
            'tid': self.table.tid, 'values': [{'r': 2, 'c': 0, 'value': 'Bob'}],
        }, {'HTTP_AUTHORIZATION': f"Bearer {make_api_bearer_token(key_data['secret_id'], key_data['secret_key'])}"})
        self.assertEqual(response.status_code, 200)
        event = TableAuditEvent.objects.get()
        self.assertEqual(event.source, 'api_key')
        self.assertEqual(event.api_key_name, 'nightly sync')
        self.assertEqual(event.actor, self.user)
        self.assertEqual(event.result, 'success')

    @patch('apps.opengate.services._moderate_opengate_texts', return_value=(True, ''))
    @patch('apps.opengate.services.ImmudbService')
    def test_append_row_is_atomic_and_reports_stale_tail(self, immudb_cls, _moderation):
        path = '/api/opengate/tables/data/upsert'
        body = {
            'tid': self.table.tid,
            'append': True,
            'expected_max_row_index': 4,
            'values': [
                {'r': 5, 'c': 0, 'value': 'Alice'},
                {'r': 5, 'c': 1, 'value': 18},
            ],
        }
        immudb_cls.return_value.get_data.return_value = (True, {'max_row_index': 4})
        immudb_cls.return_value.insert_row.return_value = (True, 'written')
        response = self.post(path, body, self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['row_index'], 5)
        self.assertEqual(immudb_cls.return_value.insert_row.call_count, 1)
        self.assertEqual(TableAuditEvent.objects.filter(result='success').count(), 2)

        immudb_cls.return_value.get_data.return_value = (True, {'max_row_index': 5})
        response = self.post(path, body, self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'ROW_APPEND_CONFLICT')
        self.assertEqual(response.json()['data']['next_row_index'], 6)
        self.assertEqual(immudb_cls.return_value.insert_row.call_count, 1)

    @patch('apps.opengate.services._moderate_opengate_texts', return_value=(True, ''))
    @patch('apps.opengate.services.ImmudbService')
    def test_stale_cell_version_returns_conflict_without_writing(self, immudb_cls, _moderation):
        TableAuditEvent.objects.create(
            table=self.table, actor=self.user, source='web', action='cell.upsert',
            result='success', row_index=7, column_index=0,
        )
        response = self.post('/api/opengate/tables/data/upsert', {
            'tid': self.table.tid,
            'values': [{'r': 7, 'c': 0, 'value': 'stale', 'expected_version': 0}],
        }, self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'CELL_WRITE_CONFLICT')
        immudb_cls.return_value.upsert_data.assert_not_called()

    @patch('apps.opengate.services._moderate_opengate_texts', return_value=(True, ''))
    @patch('apps.opengate.services.ImmudbService')
    def test_disjoint_writes_with_same_table_version_conflict(self, immudb_cls, _moderation):
        immudb_cls.return_value.upsert_data.return_value = (True, 'written')
        path = '/api/opengate/tables/data/upsert'
        first = self.post(path, {
            'tid': self.table.tid, 'expected_table_version': 0,
            'values': [{'r': 1, 'c': 0, 'value': 'first'}],
        }, self.headers)
        self.assertEqual(first.status_code, 200)
        self.assertGreater(first.json()['data']['table_version'], 0)

        second = self.post(path, {
            'tid': self.table.tid, 'expected_table_version': 0,
            'values': [{'r': 1, 'c': 1, 'value': 'second'}],
        }, self.headers)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()['code'], 'TABLE_WRITE_CONFLICT')
        self.assertEqual(immudb_cls.return_value.upsert_data.call_count, 1)

    @patch('apps.opengate.services.ImmudbService')
    def test_write_without_explicit_version_detects_intervening_write(self, immudb_cls):
        def concurrent_write(**_kwargs):
            TableAuditEvent.objects.create(
                table=self.table, actor=self.outsider, source='web', action='cell.upsert',
                result='success', row_index=1, column_index=1,
            )
            return True, ''

        with patch('apps.opengate.services._moderate_opengate_texts', side_effect=concurrent_write):
            response = self.post('/api/opengate/tables/data/upsert', {
                'tid': self.table.tid, 'values': [{'r': 1, 'c': 0, 'value': 'first'}],
            }, self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'TABLE_WRITE_CONFLICT')
        immudb_cls.return_value.upsert_data.assert_not_called()

    @patch('apps.opengate.services._table_version', side_effect=[0, 1, 1, 1])
    @patch('apps.opengate.services.ImmudbService')
    def test_read_retries_when_table_changes_during_fetch(self, immudb_cls, _version):
        immudb_cls.return_value.get_data.side_effect = [
            (True, {'data': [], 'max_row_index': -1}),
            (True, {'data': [], 'max_row_index': -1}),
        ]
        ok, data = OpenGateService.get_table_data(self.user, self.table, None, None, 200, 0)
        self.assertTrue(ok)
        self.assertEqual(data['table_version'], 1)
        self.assertEqual(immudb_cls.return_value.get_data.call_count, 2)

    @patch('apps.opengate.services._moderate_opengate_texts', return_value=(True, ''))
    def test_schema_update_is_audited_and_query_requires_internal_read(self, _moderation):
        response = self.post('/api/opengate/tables/update', {
            'tid': self.table.tid, 'tableName': 'Updated records',
            'columnTitles': ['Name', 'Age'], 'numColumns': 2, 'permission': 0,
        }, self.headers)
        self.assertEqual(response.status_code, 200)
        event = TableAuditEvent.objects.get()
        self.assertEqual(event.action, 'schema.update')
        self.assertEqual(event.details['before']['name'], 'Records')
        self.assertEqual(event.details['after']['name'], 'Updated records')

        query = self.post('/api/opengate/tables/audit', {'tid': self.table.tid}, self.headers)
        self.assertEqual(query.status_code, 200)
        self.assertEqual(query.json()['data']['items'][0]['source'], 'web')
        self.assertEqual(self.post('/api/opengate/tables/audit', {'tid': self.table.tid}).status_code, 401)
        outsider = {'HTTP_AUTHORIZATION': f"Bearer {sign_token({'user_id': self.outsider.id})}"}
        self.assertEqual(self.post('/api/opengate/tables/audit', {'tid': self.table.tid}, outsider).status_code, 403)
