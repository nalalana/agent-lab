import os
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.agent_runtime.management.commands.agent_runtime_phase4c_preflight import (
    build_phase4c_preflight,
)
from apps.agent_runtime.models import CompanyAgentRuntimePolicy
from apps.agent_runtime.tests.memory_fixtures import create_memory_fixture


SAFE_AGSX_SETTINGS = {
    "AGSX_ENABLED": True,
    "E2B_DOMAIN": "agsx.example.internal",
    "E2B_API_KEY": "test-only-key",
    "AGSX_TEMPLATE": "touming-code-v1",
    "AGSX_GLOBAL_MAX_CONCURRENT_SANDBOXES": 25,
    "AGSX_FAIL_CLOSED_ON_QUOTA_BACKEND_ERROR": True,
    "AGSX_ESTIMATED_COST_MICRO_UNITS_PER_SECOND_2C2G": 1,
    "AGSX_DEFAULT_COMPANY_DAILY_COST_BUDGET_MICRO_UNITS": 1000000,
    "AGSX_GLOBAL_DAILY_COST_BUDGET_MICRO_UNITS": 10000000,
    "OPENCLAW_LEGACY_WRITE_MODE": "read_only",
    "RUNTIME_DOCKER_SHARED_HOST_API_BASE": "",
    "RUNTIME_DOCKER_SHARED_HOST_API_TOKEN": "",
}


@override_settings(**SAFE_AGSX_SETTINGS)
class Phase4CPreflightTests(TestCase):
    def setUp(self):
        self.fixture = create_memory_fixture("phase4c-preflight")

    def test_report_is_ready_when_all_release_gates_pass(self):
        for company in (self.fixture["company_a"], self.fixture["company_b"]):
            CompanyAgentRuntimePolicy.objects.create(
                company=company,
                mode=CompanyAgentRuntimePolicy.MODE_UNIFIED,
            )

        report = build_phase4c_preflight()

        self.assertTrue(report["summary"]["ready"])
        self.assertEqual(report["summary"]["blockers"], 0)
        self.assertNotIn("test-only-key", str(report))

    @override_settings(
        RUNTIME_DOCKER_SHARED_HOST_API_BASE="http://legacy-bridge.invalid:19080",
        RUNTIME_DOCKER_SHARED_HOST_API_TOKEN="secret-token",
    )
    def test_report_blocks_non_unified_or_missing_policy_and_redacts_bridge_values(self):
        CompanyAgentRuntimePolicy.objects.create(
            company=self.fixture["company_a"],
            mode=CompanyAgentRuntimePolicy.MODE_LEGACY,
        )

        report = build_phase4c_preflight(require_bridge_disabled=True)
        checks = {item["id"]: item for item in report["checks"]}

        self.assertEqual(checks["unified_runtime_policies"]["status"], "block")
        self.assertEqual(checks["legacy_bridge_configuration"]["status"], "block")
        self.assertGreaterEqual(report["summary"]["blockers"], 2)
        self.assertNotIn("secret-token", str(report))
        self.assertNotIn("legacy-bridge.invalid", str(report))

    @override_settings(OPENCLAW_LEGACY_WRITE_MODE="read_write")
    def test_report_blocks_when_phase4b_write_freeze_is_not_enabled(self):
        for company in (self.fixture["company_a"], self.fixture["company_b"]):
            CompanyAgentRuntimePolicy.objects.create(
                company=company,
                mode=CompanyAgentRuntimePolicy.MODE_UNIFIED,
            )

        report = build_phase4c_preflight()
        checks = {item["id"]: item for item in report["checks"]}

        self.assertEqual(checks["legacy_runtime_read_only"]["status"], "block")

    @patch.dict(os.environ, {"TENCENT_LIGHTHOUSE_PUBLIC_IP": "192.0.2.10"})
    def test_final_report_detects_retired_environment_without_exposing_values(self):
        report = build_phase4c_preflight(require_bridge_disabled=True)
        checks = {item["id"]: item for item in report["checks"]}

        self.assertEqual(checks["legacy_bridge_configuration"]["status"], "block")
        self.assertIn(
            "TENCENT_LIGHTHOUSE_PUBLIC_IP",
            checks["legacy_bridge_configuration"]["observed"]["configuredNames"],
        )
        self.assertNotIn("192.0.2.10", str(report))

    def test_required_phase4b_archive_is_a_blocker_when_manifest_is_missing(self):
        report = build_phase4c_preflight(require_legacy_archive=True)
        checks = {item["id"]: item for item in report["checks"]}

        self.assertEqual(checks["legacy_archive_integrity"]["status"], "block")

    @patch(
        "apps.agent_runtime.management.commands.agent_runtime_phase4c_preflight."
        "LegacyRuntimeArchive.verify"
    )
    def test_verified_phase4b_archive_passes_the_retirement_gate(self, verify_mock):
        verify_mock.return_value = {
            "valid": True,
            "archiveFilesValid": True,
            "databaseUnchanged": True,
            "phase3Integrity": {"summary": {"valid": True}},
            "openVikingBackfill": {"countMatches": True, "unchanged": True},
        }

        report = build_phase4c_preflight(
            require_legacy_archive=True,
            legacy_archive_manifest="/secure/archive/manifest.json",
        )
        checks = {item["id"]: item for item in report["checks"]}

        self.assertEqual(checks["legacy_archive_integrity"]["status"], "pass")
        self.assertNotIn("/secure/archive/manifest.json", str(report))
