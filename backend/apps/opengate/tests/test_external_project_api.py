import json
from unittest.mock import patch

from django.test import Client, TestCase

from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import get_or_create_principal, get_or_create_resource, grant_permissions
from apps.opengate.models import APIKey, Gates, GatesTable
from apps.user.models import User
from common.utils.Login_utils import sign_token


class ExternalProjectApiTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create(
            uid="user-ext-api",
            username="external_api_owner",
            created_at="1710000000",
            updated_at="1710000000",
        )
        self.gate = Gates.objects.create(
            gid="gate-ext-api",
            owner=self.user,
            gatename="External API Project",
            description="",
            created_at="1710000000",
            updated_at="1710000000",
        )
        principal = get_or_create_principal(Principal.TYPE_USER, self.user.uid)
        resource = get_or_create_resource(Resource.TYPE_PROJECT, self.gate.gid)
        grant_permissions(principal, resource, Grant.PERM_ADMIN, granter_user=self.user)
        self.login_header = {"HTTP_AUTHORIZATION": f"Bearer {sign_token({'user_id': self.user.id})}"}

    def _post_json(self, path, payload, headers=None):
        return self.client.post(
            path,
            data=json.dumps(payload),
            content_type="application/json",
            **(headers or {}),
        )

    def _create_api_key(self):
        response = self._post_json(
            "/api/opengate/keys/create",
            {"gid": self.gate.gid, "key_name": "integration-test-key"},
            self.login_header,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["result"], 1)
        return payload["data"]["api_key"]

    def test_authenticated_reader_can_load_project_details(self):
        response = self._post_json(
            "/api/opengate/gates/details",
            {"gid": self.gate.gid},
            self.login_header,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["result"], 1)
        self.assertEqual(payload["data"]["gid"], self.gate.gid)

    @patch("apps.opengate.services.ImmudbService")
    def test_bearer_key_can_create_table_and_write_cells(self, immudb_cls):
        immudb = immudb_cls.return_value
        immudb.create_table.return_value = (True, "created")
        immudb.upsert_data.return_value = (True, "written")

        api_key = self._create_api_key()
        app_headers = {"HTTP_AUTHORIZATION": f"Bearer {api_key}"}

        create_response = self._post_json(
            "/api/opengate/app/tables/create",
            {"tableName": "daily-supply", "columnTitles": ["牛种", "数量"], "permission": 0},
            app_headers,
        )
        self.assertEqual(create_response.status_code, 201)
        table_id = create_response.json()["data"]["tid"]
        self.assertTrue(GatesTable.objects.filter(tid=table_id, gate=self.gate).exists())

        write_response = self._post_json(
            "/api/opengate/app/tables/upsert",
            {"tid": table_id, "values": [{"r": 0, "c": 0, "value": "安格斯"}, {"r": 0, "c": 1, "value": 12}]},
            app_headers,
        )
        self.assertEqual(write_response.status_code, 200)
        self.assertEqual(write_response.json()["data"]["written"], 2)
        self.assertEqual(immudb.upsert_data.call_count, 2)
        key_obj = APIKey.objects.get(gate=self.gate)
        self.assertEqual(key_obj.usage_count, 2)
        self.assertEqual(key_obj.last_used_ip, "127.0.0.1")
        self.assertEqual(key_obj.last_used_path, "/api/opengate/app/tables/upsert")
        self.assertIsNotNone(key_obj.last_used_at)

    def test_missing_or_inactive_key_is_rejected_with_401(self):
        missing_response = self._post_json("/api/opengate/app/tables/create", {"tableName": "x", "columnTitles": ["a"]})
        self.assertEqual(missing_response.status_code, 401)

        api_key = self._create_api_key()
        key_obj = APIKey.objects.get(gate=self.gate)
        key_obj.is_active = APIKey.IS_ACTIVE_INACTIVE
        key_obj.save(update_fields=["is_active"])

        inactive_response = self._post_json(
            "/api/opengate/app/tables/create",
            {"tableName": "x", "columnTitles": ["a"]},
            {"HTTP_AUTHORIZATION": f"Bearer {api_key}"},
        )
        self.assertEqual(inactive_response.status_code, 401)

    @patch("apps.opengate.services.ImmudbService")
    def test_api_key_cannot_access_table_from_another_project(self, immudb_cls):
        immudb_cls.return_value.create_table.return_value = (True, "created")
        api_key = self._create_api_key()
        other_gate = Gates.objects.create(
            gid="gate-other",
            owner=self.user,
            gatename="Other",
            description="",
            created_at="1710000000",
            updated_at="1710000000",
        )
        other_table = GatesTable.objects.create(
            tid="table-other",
            gate=other_gate,
            name="private",
            permission=GatesTable.PERM_PRIVATE,
            num_columns=1,
            columns_config=[{"field_name": "A", "title": "A", "column_index": 0}],
            created_at="1710000000",
            updated_at="1710000000",
        )

        response = self._post_json(
            "/api/opengate/app/tables/upsert",
            {"tid": other_table.tid, "values": [{"r": 0, "c": 0, "value": "blocked"}]},
            {"HTTP_AUTHORIZATION": f"Bearer {api_key}"},
        )
        self.assertEqual(response.status_code, 403)
