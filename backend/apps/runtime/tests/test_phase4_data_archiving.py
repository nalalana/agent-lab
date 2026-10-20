"""Acceptance tests for the Phase 4-B legacy runtime freeze and archive."""

import json
import os
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import RequestFactory, TestCase, override_settings
from django.http import JsonResponse

from apps.company.models import Company
from apps.company.services import CompanyService
from apps.runtime.legacy_archive import LegacyArchiveError, LegacyRuntimeArchive
from apps.runtime.legacy_control import (
    LegacyRuntimeReadOnlyError,
    LegacyRuntimeReadOnlyMiddleware,
)
from apps.runtime.models import OpenClawInstance
from apps.runtime.services import OpenClawRuntimeService
from apps.runtime.tasks import provision_openclaw_instance_task
from apps.user.models import User


READ_ONLY = override_settings(OPENCLAW_LEGACY_WRITE_MODE="read_only")


class Phase4DataArchivingFixture:
    """Create the minimum records needed by the archive acceptance tests."""

    def setUp(self):
        super().setUp()
        self.owner = User.objects.create(
            uid="phase4-owner",
            username="phase4-owner",
            created_at="2026-08-14",
            updated_at="2026-08-14",
        )
        self.company = Company.objects.create(
            account=self.owner,
            cid="phase4-company",
            company_name="Phase 4 Company",
            created_at="2026-08-14",
            updated_at="2026-08-14",
        )
        self.instance = self._create_instance("legacy-1")

    def _create_instance(self, instance_id):
        return OpenClawInstance.objects.create(
            company=self.company,
            instance_id=instance_id,
            created_at="2026-08-14",
            updated_at="2026-08-14",
        )


class LegacyRuntimeWriteRouterTests(Phase4DataArchivingFixture, TestCase):
    def test_read_only_mode_allows_reads_and_rejects_all_orm_writes(self):
        with READ_ONLY:
            self.assertEqual(OpenClawInstance.objects.count(), 1)
            with self.assertRaises(LegacyRuntimeReadOnlyError):
                self._create_instance("legacy-2")
            with self.assertRaises(LegacyRuntimeReadOnlyError):
                OpenClawInstance.objects.filter(pk=self.instance.pk).update(
                    status=OpenClawInstance.STATUS_RUNNING,
                )
            with self.assertRaises(LegacyRuntimeReadOnlyError):
                OpenClawInstance.objects.filter(pk=self.instance.pk).delete()

        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, OpenClawInstance.STATUS_PENDING)

    def test_default_mode_preserves_existing_write_behavior(self):
        self._create_instance("legacy-2")
        self.assertEqual(OpenClawInstance.objects.count(), 2)


class LegacyRuntimeMiddlewareTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.called = False
        self.middleware = LegacyRuntimeReadOnlyMiddleware(self._get_response)

    def _get_response(self, request):
        self.called = True
        return JsonResponse({"result": 1})

    def test_legacy_mutation_is_blocked_before_view_execution(self):
        with READ_ONLY:
            response = self.middleware(
                self.factory.post("/api/runtime/instance/provision")
            )

        self.assertEqual(response.status_code, 410)
        self.assertFalse(self.called)
        body = json.loads(response.content)
        self.assertEqual(body["result"], 0)
        self.assertEqual(body["schemaVersion"], 1)
        self.assertTrue(body["data"]["retired"])

    def test_read_api_and_unified_workspace_api_remain_available(self):
        with READ_ONLY:
            read_response = self.middleware(
                self.factory.get("/api/runtime/instance/detail")
            )
            workspace_response = self.middleware(
                self.factory.post("/api/runtime/workspace/upload-ticket")
            )

        self.assertEqual(read_response.status_code, 200)
        self.assertEqual(workspace_response.status_code, 200)


class LegacyBackgroundTaskTests(TestCase):
    @patch.object(OpenClawRuntimeService, "run_instance_provision")
    def test_queued_legacy_task_does_not_reach_provider_after_cutover(self, run_mock):
        with READ_ONLY:
            result = provision_openclaw_instance_task(task_id=999999)

        self.assertTrue(result["skipped"])
        run_mock.assert_not_called()


class LegacyRuntimeArchiveTests(Phase4DataArchivingFixture, TestCase):
    def test_archive_is_restricted_verifiable_and_non_destructive(self):
        with TemporaryDirectory() as output_dir, READ_ONLY:
            manifest = LegacyRuntimeArchive().create(output_dir)
            verification = LegacyRuntimeArchive().verify(
                Path(output_dir) / "manifest.json"
            )
            archive_file = Path(output_dir) / manifest["models"][
                "runtime.openclawinstance"
            ]["file"]

            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(archive_file.stat().st_mode), 0o600)
            else:
                self.assertTrue(archive_file.exists())
            self.assertTrue(verification["valid"])
            self.assertTrue(verification["databaseUnchanged"])

        self.assertEqual(OpenClawInstance.objects.count(), 1)

    def test_archive_requires_the_read_only_cutover_gate(self):
        with TemporaryDirectory() as output_dir:
            with self.assertRaises(LegacyArchiveError):
                LegacyRuntimeArchive().create(output_dir)


class CompanyLegacyRuntimeSkipTests(Phase4DataArchivingFixture, TestCase):
    def test_company_sync_paths_do_not_recreate_legacy_records(self):
        with READ_ONLY:
            publish_ok, publish_payload = CompanyService._publish_transparent_company_runtime(
                self.company
            )
            sync_ok, sync_payload = CompanyService._sync_transparent_company_runtime_config(
                self.company,
                operator_id=self.owner.uid,
                notes="phase4 acceptance",
            )

        self.assertTrue(publish_ok)
        self.assertTrue(sync_ok)
        self.assertTrue(publish_payload["skipped"])
        self.assertTrue(sync_payload["skipped"])
        self.assertEqual(OpenClawInstance.objects.count(), 1)
