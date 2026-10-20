import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import Client, SimpleTestCase

from apps.opengate.models import GatesTable
from apps.opengate.services import OpenGateService


class TableIntegrityApiTests(SimpleTestCase):
    def setUp(self):
        self.client = Client()

    @patch("apps.opengate.views.OpenGateService.get_table_integrity")
    def test_public_table_integrity_allows_anonymous_fetch(self, mock_integrity):
        mock_integrity.return_value = (True, {
            "table_id": "table-public",
            "total_rows": 2,
            "verified_rows": 2,
            "last_write_time": "2024-03-15T09:30:00Z",
            "last_verification_time": "2024-03-15T10:30:00Z",
            "merkle_root": "0xabc123",
            "integrity_score": 100,
        })

        response = self.client.get("/api/backend/tables/table-public/integrity")

        self.assertEqual(response.status_code, 200)
        mock_integrity.assert_called_once()
        self.assertIsNone(mock_integrity.call_args.kwargs["user"])
        payload = response.json()
        self.assertEqual(payload["result"], 1)
        self.assertEqual(payload["message"], "success")
        self.assertEqual(set(payload["data"].keys()), {
            "table_id",
            "total_rows",
            "verified_rows",
            "last_write_time",
            "last_verification_time",
            "merkle_root",
            "integrity_score",
        })
        self.assertEqual(payload["data"]["table_id"], "table-public")
        self.assertEqual(payload["data"]["merkle_root"], "0xabc123")

    @patch("apps.opengate.views.OpenGateService.get_table_integrity")
    def test_private_table_integrity_rejects_anonymous_fetch(self, mock_integrity):
        mock_integrity.return_value = (False, "authentication required")

        response = self.client.get("/api/backend/tables/table-private/integrity")

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["result"], 0)
        self.assertEqual(payload["message"], "authentication required")
        self.assertIsNone(mock_integrity.call_args.kwargs["user"])

    @patch("apps.opengate.views.OpenGateService.get_row_integrity")
    def test_public_row_integrity_shape_matches_frontend_contract(self, mock_row_integrity):
        mock_row_integrity.return_value = (True, {
            "row_id": "row-001",
            "hash": "0xrowhash",
            "block_height": 1000000,
            "timestamp": "2024-03-15T10:30:00Z",
            "merkle_proof": ["0xleft", "0xright"],
            "verified": True,
            "verification_time": "2024-03-15T10:31:00Z",
        })

        response = self.client.get("/api/backend/tables/table-public/rows/row-001/integrity")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["result"], 1)
        self.assertEqual(payload["message"], "success")
        self.assertEqual(set(payload["data"].keys()), {
            "row_id",
            "hash",
            "block_height",
            "timestamp",
            "merkle_proof",
            "verified",
            "verification_time",
        })
        self.assertEqual(payload["data"]["row_id"], "row-001")
        self.assertTrue(payload["data"]["verified"])

    @patch("apps.opengate.views.OpenGateService.verify_table_rows")
    def test_public_batch_verify_returns_counts_and_results(self, mock_verify):
        mock_verify.return_value = (True, {
            "table_id": "table-public",
            "verified_count": 1,
            "failed_count": 1,
            "results": [
                {"row_id": "row-001", "verified": True, "hash": "0x1"},
                {"row_id": "row-404", "verified": False, "message": "row data not found"},
            ],
        })

        response = self.client.post(
            "/api/backend/tables/table-public/verify",
            data=json.dumps({"row_ids": ["row-001", "row-404"]}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["result"], 1)
        self.assertEqual(payload["message"], "success")
        self.assertEqual(payload["data"]["verified_count"], 1)
        self.assertEqual(payload["data"]["failed_count"], 1)
        self.assertEqual(len(payload["data"]["results"]), 2)
        self.assertIsNone(mock_verify.call_args.kwargs["user"])

    @patch("apps.opengate.views.OpenGateService.verify_table_rows")
    def test_verify_without_row_ids_verifies_whole_table(self, mock_verify):
        mock_verify.return_value = (True, {
            "table_id": "table-public",
            "verified_count": 2,
            "failed_count": 0,
            "results": [],
            "merkle_root": "0xroot",
            "integrity_score": 100,
        })

        response = self.client.post(
            "/api/backend/tables/table-public/verify",
            data=json.dumps({}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["result"], 1)
        self.assertEqual(payload["data"]["verified_count"], 2)
        self.assertEqual(mock_verify.call_args.kwargs["row_ids"], [])

    @patch("apps.opengate.services.ImmudbService.get_table_integrity")
    @patch("apps.opengate.services.GatesTable.objects.get")
    def test_service_private_anonymous_rejects_before_immudb_lookup(self, mock_get_table, mock_immudb):
        mock_get_table.return_value = SimpleNamespace(
            tid="table-private",
            permission=GatesTable.PERM_PRIVATE,
            gate=SimpleNamespace(allows_public_read=lambda: False),
        )

        ok, message = OpenGateService.get_table_integrity(user=None, tid="table-private")

        self.assertFalse(ok)
        self.assertEqual(message, "authentication required")
        mock_immudb.assert_not_called()
