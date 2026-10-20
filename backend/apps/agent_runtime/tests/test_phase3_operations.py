"""Phase 3-C operations API, redaction, usage and alert tests."""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from apps.agent_runtime.alerts import dispatch_runtime_alerts, evaluate_runtime_alerts
from apps.agent_runtime.models import (
    AgentRun,
    AgentRunEvent,
    AgentToolCall,
    CompanyAgentRuntimePolicy,
    SandboxExecution,
)
from apps.agent_runtime.tests.memory_fixtures import create_memory_fixture
from apps.agent_runtime.views import run_detail, run_list
from apps.company.models import Company, CompanyMember, TransparentCompanyAgentTestMessage
from apps.notification.models import Notification
from common.utils.Login_utils import sign_token


class Phase3OperationsTests(TestCase):
    def setUp(self):
        self.fixture = create_memory_fixture(prefix="phase3-operations")
        self.factory = RequestFactory()
        self.run = self._create_run(
            company=self.fixture["company_a"],
            agent=self.fixture["agent_a1"],
            session=self.fixture["session_a1"],
            request_id="run-a",
            status=AgentRun.STATUS_FAILED,
        )
        self.other_run = self._create_run(
            company=self.fixture["company_b"],
            agent=self.fixture["agent_b1"],
            session=self.fixture["session_b1"],
            request_id="run-b",
            status=AgentRun.STATUS_COMPLETED,
        )

    def test_run_list_filters_and_never_crosses_company_boundary(self):
        response = self._call(
            run_list,
            self.factory.get(
                "/runs",
                {"cid": self.fixture["company_a"].cid, "status": AgentRun.STATUS_FAILED},
                HTTP_ACCESS_TOKEN=sign_token({"user_id": 7}),
            ),
            company=self.fixture["company_a"],
        )

        payload = json.loads(response.content)["data"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual([item["id"] for item in payload["items"]], [str(self.run.id)])
        self.assertNotIn(str(self.other_run.id), json.dumps(payload))

    def test_run_detail_redacts_events_and_hides_sandbox_external_id(self):
        AgentRunEvent.objects.create(
            run=self.run,
            sequence=1,
            event_type="warning",
            payload={"api_key": "secret-value", "safe": "visible"},
        )
        AgentToolCall.objects.create(
            run=self.run,
            model_call_id="model-call-1",
            tool_name="python.execute",
            executor="sandbox",
            arguments_redacted={"code": "print('secret')"},
            arguments_hash="a" * 64,
            status=AgentToolCall.STATUS_COMPLETED,
            duration_ms=8,
            started_at=timezone.now(),
            completed_at=timezone.now(),
        )
        SandboxExecution.objects.create(
            run=self.run,
            company=self.fixture["company_a"],
            external_id="agsx-do-not-return",
            status=SandboxExecution.STATUS_TERMINATED,
            template="touming-code-v1",
            cpu_cores=2,
            memory_mib=2048,
            network_policy="isolated",
            expires_at=timezone.now() + timedelta(minutes=5),
            ready_at=timezone.now(),
            terminated_at=timezone.now(),
            billed_seconds=3,
        )

        response = self._call(
            run_detail,
            self.factory.get(
                f"/runs/{self.run.id}",
                {"cid": self.fixture["company_a"].cid},
                HTTP_ACCESS_TOKEN=sign_token({"user_id": 7}),
            ),
            self.run.id,
            company=self.fixture["company_a"],
        )

        data = json.loads(response.content)["data"]
        encoded = json.dumps(data)
        self.assertEqual(data["events"][0]["payload"]["api_key"], "[REDACTED]")
        self.assertEqual(data["events"][0]["payload"]["safe"], "visible")
        self.assertNotIn("agsx-do-not-return", encoded)
        self.assertNotIn("arguments", data["toolCalls"][0])
        self.assertEqual(
            [item["status"] for item in data["sandbox"]["lifecycle"]],
            ["creating", "ready", "running", "terminated"],
        )

    def test_run_detail_returns_404_for_another_company(self):
        response = self._call(
            run_detail,
            self.factory.get(
                f"/runs/{self.other_run.id}",
                {"cid": self.fixture["company_a"].cid},
                HTTP_ACCESS_TOKEN=sign_token({"user_id": 7}),
            ),
            self.other_run.id,
            company=self.fixture["company_a"],
        )
        self.assertEqual(response.status_code, 404)

    @override_settings(
        AGENT_RUNTIME_ALERT_ORPHAN_MINUTES=15,
        AGENT_RUNTIME_ALERT_FAILURE_WINDOW_MINUTES=15,
        AGENT_RUNTIME_ALERT_FAILURE_RATE=0.2,
        AGENT_RUNTIME_ALERT_FAILURE_MIN_RUNS=1,
        AGENT_RUNTIME_ALERT_QUOTA_USAGE_RATIO=0.8,
    )
    def test_alerts_cover_orphans_quota_and_failure_rate(self):
        CompanyAgentRuntimePolicy.objects.create(
            company=self.fixture["company_a"],
            daily_token_budget=10,
            daily_sandbox_seconds_budget=10,
        )
        self.run.input_tokens = 8
        self.run.output_tokens = 2
        self.run.sandbox_seconds = 9
        self.run.completed_at = timezone.now()
        self.run.save(
            update_fields=[
                "input_tokens",
                "output_tokens",
                "sandbox_seconds",
                "completed_at",
            ]
        )
        sandbox = SandboxExecution.objects.create(
            run=self.run,
            company=self.fixture["company_a"],
            status=SandboxExecution.STATUS_RUNNING,
            template="touming-code-v1",
            cpu_cores=2,
            memory_mib=2048,
            network_policy="isolated",
            expires_at=timezone.now() - timedelta(minutes=10),
        )
        SandboxExecution.objects.filter(pk=sandbox.pk).update(
            created_at=timezone.now() - timedelta(minutes=20)
        )

        snapshot = evaluate_runtime_alerts(self.fixture["company_a"])

        self.assertEqual(
            {item["code"] for item in snapshot["alerts"]},
            {"orphan_sandbox", "token_quota", "sandbox_quota", "run_failure_rate"},
        )
        self.assertEqual(snapshot["metrics"]["orphanSandboxCount"], 1)

    @override_settings(
        AGENT_RUNTIME_ALERT_ORPHAN_MINUTES=60,
        AGENT_RUNTIME_ALERT_FAILURE_MIN_RUNS=999,
        AGENT_RUNTIME_ALERT_QUOTA_USAGE_RATIO=0.8,
        AGSX_GLOBAL_MAX_CONCURRENT_SANDBOXES=1,
        AGSX_ESTIMATED_COST_MICRO_UNITS_PER_SECOND_2C2G=100,
        AGSX_DEFAULT_COMPANY_DAILY_COST_BUDGET_MICRO_UNITS=1000,
        AGSX_GLOBAL_DAILY_COST_BUDGET_MICRO_UNITS=1000,
        AGSX_COST_CURRENCY="CNY",
        AGSX_ALERT_FAILURE_WINDOW_MINUTES=15,
        AGSX_ALERT_FAILURE_RATE=0.2,
        AGSX_ALERT_FAILURE_MIN_CREATES=2,
    )
    def test_alerts_cover_agsx_concurrency_cost_failure_and_cleanup(self):
        CompanyAgentRuntimePolicy.objects.create(
            company=self.fixture["company_a"],
            max_concurrent_runs=1,
        )
        self.run.sandbox_seconds = 9
        self.run.save(update_fields=["sandbox_seconds"])
        SandboxExecution.objects.create(
            run=self.run,
            company=self.fixture["company_a"],
            external_id="provider-instance-needing-cleanup",
            status=SandboxExecution.STATUS_FAILED,
            template="touming-code-v1",
            cpu_cores=2,
            memory_mib=2048,
            network_policy="isolated",
            expires_at=timezone.now() + timedelta(minutes=5),
            error_code="INTERNAL_ERROR",
        )
        active_run = self._create_run(
            company=self.fixture["company_a"],
            agent=self.fixture["agent_a1"],
            session=self.fixture["session_a1"],
            request_id="run-active-sandbox",
            status=AgentRun.STATUS_RUNNING,
        )
        SandboxExecution.objects.create(
            run=active_run,
            company=self.fixture["company_a"],
            external_id="provider-instance-active",
            status=SandboxExecution.STATUS_RUNNING,
            template="touming-code-v1",
            cpu_cores=2,
            memory_mib=2048,
            network_policy="isolated",
            expires_at=timezone.now() + timedelta(minutes=5),
        )

        snapshot = evaluate_runtime_alerts(self.fixture["company_a"])

        self.assertEqual(
            {item["code"] for item in snapshot["alerts"]},
            {
                "agsx_company_concurrency_quota",
                "agsx_global_concurrency_quota",
                "agsx_company_daily_cost",
                "agsx_global_daily_cost",
                "agsx_create_failure_rate",
                "agsx_cleanup_failure",
            },
        )
        self.assertEqual(snapshot["metrics"]["dailyEstimatedCostMicroUnits"], 900)
        self.assertEqual(snapshot["metrics"]["sandboxCleanupFailureCount"], 1)
        self.assertEqual(snapshot["metrics"]["costCurrency"], "CNY")

    @override_settings(
        AGENT_RUNTIME_ALERTS_ENABLED=True,
        AGENT_RUNTIME_ALERT_DEDUP_SECONDS=900,
        AGENT_RUNTIME_ALERT_FAILURE_MIN_RUNS=999,
    )
    def test_periodic_alert_notification_is_deduplicated(self):
        CompanyAgentRuntimePolicy.objects.create(company=self.fixture["company_a"])
        cache.clear()
        alert = {
            "code": "orphan_sandbox",
            "severity": "critical",
            "current": 1,
            "threshold": 0,
            "message": "发现遗留沙箱",
            "observedAt": timezone.now().isoformat(),
        }
        snapshot = {"alerts": [alert], "schemaVersion": 1, "metrics": {}}
        with patch("apps.agent_runtime.alerts.evaluate_runtime_alerts", return_value=snapshot):
            dispatch_runtime_alerts()
            dispatch_runtime_alerts()

        self.assertEqual(
            Notification.objects.filter(
                user=self.fixture["owner"],
                type=Notification.TYPE_RUNTIME_CONFIG_ALERT,
            ).count(),
            1,
        )

    @override_settings(
        AGENT_RUNTIME_ALERTS_ENABLED=True,
        AGENT_RUNTIME_ALERT_DEDUP_SECONDS=900,
        AGENT_RUNTIME_ALERT_FAILURE_MIN_RUNS=999,
    )
    def test_verified_created_by_is_not_notified_without_active_membership(self):
        company = self.fixture["company_a"]
        company.verification_status = Company.VERIFICATION_STATUS_VERIFIED
        company.save(update_fields=["verification_status"])
        CompanyAgentRuntimePolicy.objects.create(company=company)
        cache.clear()
        snapshot = {
            "alerts": [{
                "code": "verified-member-only",
                "severity": "critical",
                "current": 1,
                "threshold": 0,
                "message": "verified member only",
                "observedAt": timezone.now().isoformat(),
            }],
            "schemaVersion": 1,
            "metrics": {},
        }

        with patch("apps.agent_runtime.alerts.evaluate_runtime_alerts", return_value=snapshot):
            dispatch_runtime_alerts()

        self.assertFalse(
            Notification.objects.filter(
                user=self.fixture["owner"],
                type=Notification.TYPE_RUNTIME_CONFIG_ALERT,
            ).exists()
        )

    @override_settings(
        AGENT_RUNTIME_ALERTS_ENABLED=True,
        AGENT_RUNTIME_ALERT_DEDUP_SECONDS=900,
        AGENT_RUNTIME_ALERT_FAILURE_MIN_RUNS=999,
    )
    def test_verified_created_by_active_owner_receives_one_notification(self):
        company = self.fixture["company_a"]
        company.verification_status = Company.VERIFICATION_STATUS_VERIFIED
        company.save(update_fields=["verification_status"])
        CompanyMember.objects.create(
            company=company,
            user=self.fixture["owner"],
            memberUid=self.fixture["owner"].uid,
            name=self.fixture["owner"].username,
            email="phase3-owner@example.com",
            role="owner",
            status=CompanyMember.STATUS_ACTIVE,
            created_at="2026-08-13",
            updated_at="2026-08-13",
        )
        CompanyAgentRuntimePolicy.objects.create(company=company)
        cache.clear()
        snapshot = {
            "alerts": [{
                "code": "verified-active-owner",
                "severity": "critical",
                "current": 1,
                "threshold": 0,
                "message": "verified active owner",
                "observedAt": timezone.now().isoformat(),
            }],
            "schemaVersion": 1,
            "metrics": {},
        }

        with patch("apps.agent_runtime.alerts.evaluate_runtime_alerts", return_value=snapshot):
            dispatch_runtime_alerts()

        self.assertEqual(
            Notification.objects.filter(
                user=self.fixture["owner"],
                type=Notification.TYPE_RUNTIME_CONFIG_ALERT,
            ).count(),
            1,
        )

    def test_operational_alert_task_is_scheduled_every_minute(self):
        from django.conf import settings

        schedule = settings.CELERY_BEAT_SCHEDULE["agent-runtime-operational-alerts"]
        self.assertEqual(
            schedule["task"],
            "apps.agent_runtime.alerts.dispatch_runtime_alerts",
        )
        self.assertEqual(schedule["schedule"], 60.0)

    def _call(self, view, request, *args, company):
        with patch("apps.agent_runtime.views.get_user_from_request", return_value=self.fixture["owner"]), patch(
            "apps.agent_runtime.views.CompanyService._get_company_for_admin",
            return_value=(True, company),
        ):
            return view(request, *args)

    def _create_run(self, *, company, agent, session, request_id, status):
        message = TransparentCompanyAgentTestMessage.objects.create(
            session=session,
            role=TransparentCompanyAgentTestMessage.ROLE_USER,
            content=request_id,
            created_at="2026-08-13",
            updated_at="2026-08-13",
        )
        return AgentRun.objects.create(
            request_id=request_id,
            company=company,
            agent=agent,
            user=self.fixture["owner"],
            session=session,
            source=AgentRun.SOURCE_AGENT_TEST,
            engine=AgentRun.ENGINE_UNIFIED,
            status=status,
            input_message=message,
            metadata={"trace_id": f"trace-{request_id}"},
        )
