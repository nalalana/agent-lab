import base64
import json
import time
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.company.models import Company
from apps.runtime.models import OpenClawConfigVersion, OpenClawInstance, OpenClawRuntimeSnapshot
from apps.runtime.services import OpenClawRuntimeService
from apps.user.models import User


class RuntimeSnapshotCosTests(TestCase):
    def setUp(self):
        now = str(time.time())[:20]
        self.user = User.objects.create(
            uid="snapshot-admin",
            username="snapshot-admin",
            created_at=now,
            updated_at=now,
            role="user",
        )
        self.company = Company.objects.create(
            account=self.user,
            cid="company-runtime-snapshot",
            company_name="Snapshot Inc",
            status=Company.COMPANY_STATUS_TRANSPARENT,
            created_at=now,
            updated_at=now,
        )
        self.instance = OpenClawInstance.objects.create(
            company=self.company,
            provider=OpenClawInstance.PROVIDER_TENCENT_LIGHTHOUSE,
            runtime_namespace="tsds-company-runtime-snapshot",
            instance_id="lhins-test",
            status=OpenClawInstance.STATUS_RUNNING,
            openclaw_status=OpenClawInstance.OPENCLAW_STATUS_HEALTHY,
            config_status=OpenClawInstance.CONFIG_STATUS_IN_SYNC,
            created_at=now,
            updated_at=now,
        )
        self.config_version = OpenClawConfigVersion.objects.create(
            company=self.company,
            instance=self.instance,
            version="snapshot-v1",
            schema_version="v2-preview",
            source=OpenClawConfigVersion.SOURCE_PLATFORM_GUI,
            status=OpenClawConfigVersion.STATUS_PUBLISHED,
            created_by="test",
            notes="",
            config_payload=json.dumps({"agents": [{"agent_id": "main-agent"}]}),
            published_at=now,
            created_at=now,
            updated_at=now,
        )

    @override_settings(COS_BUCKET="runtime-snapshot-bucket", RUNTIME_SNAPSHOT_RETENTION_DAYS=7)
    @patch("apps.runtime.services.RuntimeAuditService.log")
    @patch("apps.runtime.services.put_object_with_key")
    def test_backup_runtime_state_uploads_json_snapshot_to_cos(self, mock_put_object, _mock_audit):
        ok, payload = OpenClawRuntimeService.backup_runtime_state(self.instance)

        self.assertTrue(ok)
        snapshot = OpenClawRuntimeSnapshot.objects.get(id=payload["id"])
        self.assertEqual(OpenClawRuntimeSnapshot.STATUS_UPLOADED, snapshot.status)
        self.assertEqual("runtime-snapshot-bucket", snapshot.cos_bucket)
        self.assertEqual(str(snapshot.id), OpenClawInstance.objects.get(id=self.instance.id).latest_snapshot_id)
        mock_put_object.assert_called_once()
        body, cos_key = mock_put_object.call_args.args
        self.assertEqual(snapshot.cos_key, cos_key)
        self.assertEqual("application/json", mock_put_object.call_args.kwargs["content_type"])
        snapshot_payload = json.loads(body.decode("utf-8"))
        self.assertEqual("runtime-state/v1", snapshot_payload["snapshot_schema"])
        self.assertEqual(7, snapshot_payload["retention_days"])
        self.assertEqual(self.company.cid, snapshot_payload["company"]["cid"])
        self.assertEqual("snapshot-v1", snapshot_payload["config_versions"][0]["version"])
        self.assertEqual("platform_metadata_snapshot", snapshot_payload["provider_payload"]["source"])

    @patch("apps.runtime.services.RuntimeAuditService.log")
    @patch("apps.runtime.services.get_object")
    def test_restore_runtime_state_loads_cos_snapshot_and_recreates_config_draft(self, mock_get_object, _mock_audit):
        snapshot_payload = {
            "snapshot_schema": "runtime-state/v1",
            "provider_payload": {"source": "platform_metadata_snapshot"},
            "config_versions": [
                {
                    "version": "snapshot-v1",
                    "schema_version": "v2-preview",
                    "status": OpenClawConfigVersion.STATUS_PUBLISHED,
                    "config_payload": json.dumps({"agents": [{"agent_id": "restored-agent"}]}),
                }
            ],
            "agent_definitions": [],
            "agent_skill_bindings": [],
        }
        raw = json.dumps(snapshot_payload).encode("utf-8")
        mock_get_object.return_value = base64.b64encode(raw).decode("utf-8")
        now = str(time.time())[:20]
        snapshot = OpenClawRuntimeSnapshot.objects.create(
            company=self.company,
            openclaw_instance=self.instance,
            snapshot_type=OpenClawRuntimeSnapshot.TYPE_FULL,
            snapshot_version="runtime-state/v1",
            cos_bucket="runtime-snapshot-bucket",
            cos_key="runtime-backups/company/company-runtime-snapshot/1.json",
            checksum="sha256-test",
            status=OpenClawRuntimeSnapshot.STATUS_UPLOADED,
            retention_until="",
            source_instance_id=self.instance.instance_id,
            created_at=now,
            updated_at=now,
        )

        ok, payload = OpenClawRuntimeService.restore_runtime_state(self.instance, snapshot_id=snapshot.id)

        self.assertTrue(ok)
        snapshot.refresh_from_db()
        self.assertEqual(OpenClawRuntimeSnapshot.STATUS_RESTORED, snapshot.status)
        self.assertEqual(self.instance.instance_id, snapshot.restored_to_instance_id)
        self.assertTrue(payload["config_restore"]["restored"])
        restored = OpenClawConfigVersion.objects.get(id=payload["config_restore"]["config_version"]["id"])
        self.assertEqual(OpenClawConfigVersion.STATUS_DRAFT, restored.status)
        self.assertIn("restored-agent", restored.config_payload)
        mock_get_object.assert_called_once_with(snapshot.cos_key)