import json
from unittest.mock import patch

from django.test import Client, TestCase

from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import get_or_create_principal, get_or_create_resource, grant_permissions
from apps.opengate.models import Gates, GatesTable
from apps.user.models import User
from common.utils.Login_utils import sign_token


class ProjectPublicAccessTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.owner = User.objects.create(
            uid='public-project-owner',
            username='Owner',
            email='owner@example.com',
            phone='13800000000',
            created_at='1710000000',
            updated_at='1710000000',
        )
        self.outsider = User.objects.create(
            uid='public-project-outsider',
            username='Outsider',
            created_at='1710000000',
            updated_at='1710000000',
        )
        self.gate = Gates.objects.create(
            gid='gate-public-access',
            owner=self.owner,
            gatename='Public access project',
            description='',
            created_at='1710000000',
            updated_at='1710000000',
        )
        self.table = GatesTable.objects.create(
            tid='table-public-access',
            gate=self.gate,
            name='Data',
            permission=GatesTable.PERM_PRIVATE,
            num_columns=1,
            columns_config=[{'field_name': 'A', 'title': 'A', 'column_index': 0}],
            created_at='1710000000',
            updated_at='1710000000',
        )
        owner_principal = get_or_create_principal(Principal.TYPE_USER, self.owner.uid)
        project_resource = get_or_create_resource(Resource.TYPE_PROJECT, self.gate.gid)
        get_or_create_resource(Resource.TYPE_TABLE, self.table.tid, parent=project_resource)
        grant_permissions(
            owner_principal,
            project_resource,
            Grant.PERM_SUPER_ADMIN,
            granter_user=self.owner,
        )
        self.owner_headers = self._headers(self.owner)
        self.outsider_headers = self._headers(self.outsider)

    @staticmethod
    def _headers(user):
        return {'HTTP_AUTHORIZATION': f"Bearer {sign_token({'user_id': user.id})}"}

    def _post(self, path, body, headers=None):
        return self.client.post(
            path,
            data=json.dumps(body),
            content_type='application/json',
            **(headers or {}),
        )

    def test_only_project_managers_can_change_public_access(self):
        denied = self._post(
            '/api/opengate/gates/public-access',
            {'gid': self.gate.gid, 'public_access': Gates.PUBLIC_ACCESS_READ},
            self.outsider_headers,
        )
        self.assertEqual(denied.status_code, 403)

        response = self._post(
            '/api/opengate/gates/public-access',
            {'gid': self.gate.gid, 'public_access': Gates.PUBLIC_ACCESS_WRITE},
            self.owner_headers,
        )
        self.assertEqual(response.status_code, 200)
        self.gate.refresh_from_db()
        self.assertEqual(self.gate.public_access, Gates.PUBLIC_ACCESS_WRITE)
        self.assertEqual(response.json()['data']['public_access'], Gates.PUBLIC_ACCESS_WRITE)

    def test_internal_project_hides_details_from_outsiders(self):
        anonymous = self._post('/api/opengate/gates/details', {'gid': self.gate.gid})
        outsider = self._post(
            '/api/opengate/gates/details',
            {'gid': self.gate.gid},
            self.outsider_headers,
        )
        owner = self._post(
            '/api/opengate/gates/details',
            {'gid': self.gate.gid},
            self.owner_headers,
        )
        self.assertEqual(anonymous.status_code, 404)
        self.assertEqual(outsider.status_code, 404)
        self.assertEqual(owner.status_code, 200)
        self.assertTrue(owner.json()['data']['can_manage_public_access'])

    @patch('apps.opengate.services.ImmudbService')
    def test_public_read_allows_anonymous_reads_but_not_writes(self, immudb_cls):
        self.gate.public_access = Gates.PUBLIC_ACCESS_READ
        self.gate.save(update_fields=['public_access'])
        immudb_cls.return_value.get_data.return_value = (True, {'rows': [], 'total': 0})
        immudb_cls.return_value.get_table_integrity.return_value = (
            True,
            {'verified_rows': 0, 'total_rows': 0, 'integrity_score': 100},
        )

        detail = self._post('/api/opengate/gates/details', {'gid': self.gate.gid})
        tables = self.client.get('/api/opengate/gates/tables', {'gid': self.gate.gid})
        table_detail = self._post('/api/opengate/tables/details', {'tid': self.table.tid})
        table_data = self._post('/api/opengate/tables/data', {'tid': self.table.tid})
        integrity = self.client.get(f'/api/opengate/tables/{self.table.tid}/integrity')
        anonymous_write = self._post(
            '/api/opengate/tables/data/upsert',
            {'tid': self.table.tid, 'values': [{'r': 0, 'c': 0, 'value': 'blocked'}]},
        )
        outsider_write = self._post(
            '/api/opengate/tables/data/upsert',
            {'tid': self.table.tid, 'values': [{'r': 0, 'c': 0, 'value': 'blocked'}]},
            self.outsider_headers,
        )

        self.assertEqual(detail.status_code, 200)
        self.assertFalse(detail.json()['data']['can_manage_public_access'])
        self.assertNotIn('email', detail.json()['data']['owner'])
        self.assertNotIn('phone', detail.json()['data']['owner'])
        self.assertEqual(tables.status_code, 200)
        self.assertEqual(tables.json()['data']['total'], 1)
        self.assertEqual(table_detail.status_code, 200)
        self.assertFalse(table_detail.json()['data']['can_write'])
        self.assertEqual(table_data.status_code, 200)
        self.assertEqual(integrity.status_code, 200)
        self.assertEqual(anonymous_write.status_code, 401)
        self.assertEqual(outsider_write.status_code, 403)

    @patch('apps.opengate.services._moderate_opengate_texts', return_value=(True, 'ok'))
    @patch('apps.opengate.services.ImmudbService')
    def test_public_write_allows_authenticated_outsider_data_edits(
        self,
        immudb_cls,
        _moderate,
    ):
        self.gate.public_access = Gates.PUBLIC_ACCESS_WRITE
        self.gate.save(update_fields=['public_access'])
        immudb_cls.return_value.upsert_data.return_value = (True, 'written')

        anonymous = self._post(
            '/api/opengate/tables/data/upsert',
            {'tid': self.table.tid, 'values': [{'r': 0, 'c': 0, 'value': 'anonymous'}]},
        )
        outsider = self._post(
            '/api/opengate/tables/data/upsert',
            {'tid': self.table.tid, 'values': [{'r': 0, 'c': 0, 'value': 'allowed'}]},
            self.outsider_headers,
        )
        detail = self._post(
            '/api/opengate/tables/details',
            {'tid': self.table.tid},
            self.outsider_headers,
        )

        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(outsider.status_code, 200)
        self.assertTrue(detail.json()['data']['can_write'])
        immudb_cls.return_value.upsert_data.assert_called_once()
