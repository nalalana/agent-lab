from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier, Lock
from unittest.mock import MagicMock, patch

from django.db import close_old_connections, connection
from django.test import Client, TransactionTestCase

from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import get_or_create_principal, get_or_create_resource, grant_permissions
from apps.opengate.models import Gates, GatesTable, TableAuditEvent
from apps.opengate.services import OpenGateService, TableWriteConflict
from apps.user.models import User
from common.utils.Login_utils import make_api_bearer_token


class TableConcurrencyTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('select_for_update concurrency requires PostgreSQL')
        self.user = User.objects.create(
            uid='concurrent-editor', username='Concurrent editor', created_at='1', updated_at='1',
        )
        self.gate = Gates.objects.create(
            gid='concurrent-project', owner=self.user, gatename='Concurrent project',
            created_at='1', updated_at='1',
        )
        self.table = GatesTable.objects.create(
            tid='concurrent-table', gate=self.gate, name='Concurrent table', num_columns=16,
            created_at='1', updated_at='1',
        )

    def _run_parallel(self, count, make_cells, *, append=False, expected_max_row_index=None, immudb):
        barrier = Barrier(count, timeout=20)

        def moderate(**_kwargs):
            barrier.wait()
            return True, ''

        def write(index):
            close_old_connections()
            try:
                try:
                    result = OpenGateService.upsert_table_cells(
                        self.user, self.table, make_cells(index),
                        append=append, expected_max_row_index=expected_max_row_index,
                    )
                    return 'success' if result[0] else 'failed'
                except TableWriteConflict as exc:
                    return exc.code
            finally:
                connection.close()

        with patch.object(OpenGateService, 'can_write_table', return_value=True), \
                patch('apps.opengate.services._moderate_opengate_texts', side_effect=moderate), \
                patch('apps.opengate.services.ImmudbService', return_value=immudb):
            with ThreadPoolExecutor(max_workers=count) as pool:
                return list(pool.map(write, range(count)))

    def test_parallel_writers_to_same_cell(self):
        from unittest.mock import MagicMock

        immudb = MagicMock()
        immudb.upsert_data.return_value = (True, 'written')
        results = self._run_parallel(
            12, lambda index: [{'r': 1, 'c': 0, 'value': index}], immudb=immudb,
        )
        self.assertEqual(results.count('success'), 1, results)
        self.assertEqual(results.count('TABLE_WRITE_CONFLICT'), 11, results)
        self.assertEqual(immudb.upsert_data.call_count, 1)
        self.assertEqual(TableAuditEvent.objects.filter(result='success').count(), 1)

    def test_parallel_writers_to_different_cells(self):
        from unittest.mock import MagicMock

        immudb = MagicMock()
        immudb.upsert_data.return_value = (True, 'written')
        results = self._run_parallel(
            12, lambda index: [{'r': 1, 'c': index, 'value': index}], immudb=immudb,
        )
        self.assertEqual(results.count('success'), 1, results)
        self.assertEqual(results.count('TABLE_WRITE_CONFLICT'), 11, results)
        self.assertEqual(immudb.upsert_data.call_count, 1)
        self.assertEqual(TableAuditEvent.objects.filter(result='success').count(), 1)

    def test_parallel_writers_appending_to_same_tail(self):
        from unittest.mock import MagicMock

        immudb = MagicMock()
        state = {'max_row': -1}
        state_lock = Lock()

        def read_row(*_args):
            with state_lock:
                return True, {'max_row_index': state['max_row']}

        def insert_row(*_args):
            with state_lock:
                state['max_row'] = 0
            return True, 'written'

        immudb.get_data.side_effect = read_row
        immudb.insert_row.side_effect = insert_row
        results = self._run_parallel(
            12, lambda index: [{'r': 0, 'c': index, 'value': index}],
            append=True, expected_max_row_index=-1, immudb=immudb,
        )
        self.assertEqual(results.count('success'), 1, results)
        self.assertEqual(results.count('ROW_APPEND_CONFLICT'), 11, results)
        self.assertEqual(immudb.insert_row.call_count, 1)
        self.assertEqual(TableAuditEvent.objects.filter(result='success').count(), 1)

    def test_parallel_api_key_requests_return_http_409(self):
        principal = get_or_create_principal(Principal.TYPE_USER, self.user.uid)
        project = get_or_create_resource(Resource.TYPE_PROJECT, self.gate.gid)
        grant_permissions(principal, project, Grant.PERM_ADMIN, granter_user=self.user)
        ok, key = OpenGateService.create_gate_key(self.user, self.gate, 'concurrency test')
        self.assertTrue(ok)
        token = make_api_bearer_token(key['secret_id'], key['secret_key'])
        count = 8
        barrier = Barrier(count, timeout=20)
        immudb = MagicMock()
        immudb.upsert_data.return_value = (True, 'written')

        def moderate(**_kwargs):
            barrier.wait()
            return True, ''

        def request(index):
            close_old_connections()
            try:
                response = Client().post(
                    '/api/opengate/app/tables/upsert',
                    data=json.dumps({
                        'tid': self.table.tid,
                        'values': [{'r': 2, 'c': index, 'value': index}],
                    }),
                    content_type='application/json',
                    HTTP_AUTHORIZATION=f'Bearer {token}',
                )
                return response.status_code, response.json().get('code')
            finally:
                connection.close()

        allowed = MagicMock(allowed=True)
        with patch.object(OpenGateService, 'can_write_table', return_value=True), \
                patch('apps.opengate.services._moderate_opengate_texts', side_effect=moderate), \
                patch('apps.opengate.services.ImmudbService', return_value=immudb), \
                patch('apps.opengate.views.RiskControlService.evaluate', return_value=allowed):
            with ThreadPoolExecutor(max_workers=count) as pool:
                results = list(pool.map(request, range(count)))

        self.assertEqual(sum(status == 200 for status, _ in results), 1, results)
        self.assertEqual(sum(status == 409 and code == 'TABLE_WRITE_CONFLICT' for status, code in results), 7, results)
        self.assertEqual(immudb.upsert_data.call_count, 1)
