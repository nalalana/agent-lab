"""Phase 4-C contracts for permanently retired infrastructure entrypoints."""

import json
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase
from django.urls import resolve

from apps.runtime.services import OpenClawRuntimeService
from apps.runtime.tasks import (
    bootstrap_openclaw_instance_task,
    check_openclaw_health_task,
    deploy_lighthouse_bridge_task,
    provision_openclaw_instance_task,
)
from apps.runtime.views import runtime_legacy_retired


RETIRED_PATHS = (
    "/api/runtime/lighthouse/catalog",
    "/api/runtime/lighthouse/purchase-dry-run",
    "/api/runtime/instance/provision",
    "/api/runtime/instance/retry-provision",
    "/api/runtime/instance/bootstrap",
    "/api/runtime/instance/deploy-bridge",
    "/api/runtime/instance/health-check",
)


class Phase4CRetiredRuntimeRouteTests(SimpleTestCase):
    def test_legacy_infrastructure_routes_resolve_only_to_the_tombstone(self):
        for path in RETIRED_PATHS:
            with self.subTest(path=path):
                self.assertIs(resolve(path).func, runtime_legacy_retired)

    def test_tombstone_is_stable_secret_free_and_points_to_unified_runtime(self):
        response = runtime_legacy_retired(RequestFactory().post(RETIRED_PATHS[0]))
        body = json.loads(response.content)

        self.assertEqual(response.status_code, 410)
        self.assertEqual(body["schemaVersion"], 1)
        self.assertTrue(body["data"]["retired"])
        self.assertEqual(body["data"]["replacement"], "/api/agent-runtime")


class Phase4CRetiredRuntimeTaskTests(SimpleTestCase):
    def test_stale_infrastructure_tasks_are_acknowledged_without_provider_calls(self):
        with (
            patch.object(
                OpenClawRuntimeService,
                "run_instance_provision",
            ) as provision_mock,
            patch.object(OpenClawRuntimeService, "run_bootstrap") as bootstrap_mock,
            patch.object(
                OpenClawRuntimeService,
                "run_lighthouse_bridge_deploy",
            ) as deploy_mock,
            patch.object(
                OpenClawRuntimeService,
                "record_health_check",
            ) as health_mock,
        ):
            results = (
                provision_openclaw_instance_task(task_id=1),
                bootstrap_openclaw_instance_task(task_id=2),
                deploy_lighthouse_bridge_task(task_id=3),
                check_openclaw_health_task(instance_id=4),
            )

        for result in results:
            self.assertTrue(result["retired"])
            self.assertTrue(result["skipped"])
            self.assertEqual(result["schemaVersion"], 1)
        provision_mock.assert_not_called()
        bootstrap_mock.assert_not_called()
        deploy_mock.assert_not_called()
        health_mock.assert_not_called()
