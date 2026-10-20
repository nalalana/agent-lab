from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.agent_runtime.models import AgentRun, AgentToolCall, CompanyAgentRuntimePolicy
from apps.agent_runtime.operations import runtime_metrics
from apps.agent_runtime.tests.memory_fixtures import create_memory_fixture
from apps.company.models import TransparentCompanyAgentTestMessage
from apps.company.services import CompanyService


class Phase4CanaryRoutingTests(TestCase):
    def setUp(self):
        self.fixture = create_memory_fixture("phase4-routing")
        self.company = self.fixture["company_a"]
        self.session = self.fixture["session_a1"]
        self.transparent_company = self.session.transparentCompany
        self.input_message = TransparentCompanyAgentTestMessage.objects.create(
            session=self.session,
            role=TransparentCompanyAgentTestMessage.ROLE_USER,
            content="route me",
            created_at="2026-08-14",
            updated_at="2026-08-14",
        )
        self.policy = CompanyAgentRuntimePolicy.objects.create(
            company=self.company,
            mode=CompanyAgentRuntimePolicy.MODE_LEGACY,
            canary_percentage=0,
        )

    def test_legacy_route_is_blocked_without_openclaw_adapter(self):
        with patch("apps.runtime.services.OpenClawRuntimeService.get_company_instance") as get_instance, patch(
            "apps.runtime.services.OpenClawRuntimeService.get_provider_adapter"
        ) as get_adapter, patch(
            "apps.agent_runtime.runtime.UnifiedRuntimeService.stream",
            side_effect=AssertionError("unified must not run for a blocked legacy policy"),
        ):
            ok, message = CompanyService._stream_client_entry_message_via_runtime(
                self.transparent_company,
                {
                    "content": "legacy request",
                    "_runtime_session_id": self.session.pk,
                    "_runtime_input_message_id": self.input_message.pk,
                },
            )

        self.assertFalse(ok)
        self.assertIn("统一运行时已全量切换", message)
        get_instance.assert_not_called()
        get_adapter.assert_not_called()

    def test_unified_route_does_not_query_openclaw_adapter(self):
        self.policy.mode = CompanyAgentRuntimePolicy.MODE_UNIFIED
        self.policy.canary_percentage = 100
        self.policy.save(update_fields=["mode", "canary_percentage"])
        fake_events = iter([{"event": "complete", "content": "unified"}])

        with patch("apps.agent_runtime.runtime.UnifiedRuntimeService.stream", return_value=fake_events) as stream, patch(
            "apps.runtime.services.OpenClawRuntimeService.get_company_instance",
            side_effect=AssertionError("OpenClaw must not be queried for unified route"),
        ):
            ok, events = CompanyService._stream_client_entry_message_via_runtime(
                self.transparent_company,
                {
                    "content": "unified request",
                    "_runtime_session_id": self.session.pk,
                    "_runtime_input_message_id": self.input_message.pk,
                },
            )

        self.assertTrue(ok)
        self.assertIs(events, fake_events)
        self.assertEqual(stream.call_args.kwargs["source"], "public_client")

    def test_canary_100_percent_routes_to_unified_without_openclaw(self):
        self.policy.mode = CompanyAgentRuntimePolicy.MODE_CANARY
        self.policy.canary_percentage = 100
        self.policy.save(update_fields=["mode", "canary_percentage"])

        with patch("apps.agent_runtime.runtime.UnifiedRuntimeService.stream", return_value=iter([])) as stream, patch(
            "apps.runtime.services.OpenClawRuntimeService.get_company_instance",
            side_effect=AssertionError("OpenClaw must not be queried for 100 percent canary"),
        ):
            ok, _events = CompanyService._stream_agent_test_message_via_runtime(
                self.transparent_company,
                self.session,
                "canary all",
            )

        self.assertTrue(ok)
        self.assertTrue(stream.called)

    def test_missing_policy_is_backfilled_to_unified_after_cutover(self):
        self.policy.delete()
        fake_events = iter([{"event": "complete", "content": "unified"}])

        with patch("apps.agent_runtime.runtime.UnifiedRuntimeService.stream", return_value=fake_events) as stream, patch(
            "apps.runtime.services.OpenClawRuntimeService.get_company_instance",
            side_effect=AssertionError("OpenClaw must not be queried after cutover"),
        ):
            ok, events = CompanyService._stream_agent_test_message_via_runtime(
                self.transparent_company,
                self.session,
                "missing policy should self-heal",
            )

        self.assertTrue(ok)
        self.assertIs(events, fake_events)
        self.assertEqual(stream.call_args.kwargs["source"], "agent_test")
        policy = CompanyAgentRuntimePolicy.objects.get(company=self.company)
        self.assertEqual(policy.mode, CompanyAgentRuntimePolicy.MODE_UNIFIED)
        self.assertEqual(policy.canary_percentage, 100)

class Phase4RuntimeMetricsTests(TestCase):
    def test_runtime_metrics_cover_latency_and_error_rates(self):
        fixture = create_memory_fixture("phase4-metrics")
        run_ok = self._run(fixture, "ok", AgentRun.STATUS_COMPLETED, error_code="")
        run_model_error = self._run(
            fixture,
            "model-error",
            AgentRun.STATUS_FAILED,
            error_code="MODEL_TIMEOUT",
        )
        now = timezone.now()
        AgentRun.objects.filter(pk=run_ok.pk).update(
            started_at=now - timedelta(milliseconds=900),
            first_token_at=now - timedelta(milliseconds=700),
            completed_at=now,
        )
        AgentRun.objects.filter(pk=run_model_error.pk).update(
            started_at=now - timedelta(milliseconds=500),
            completed_at=now,
        )
        AgentToolCall.objects.create(
            run=run_ok,
            model_call_id="tool-ok",
            tool_name="runtime.echo",
            executor="backend",
            status=AgentToolCall.STATUS_COMPLETED,
            arguments_redacted={},
            arguments_hash="a" * 64,
        )
        AgentToolCall.objects.create(
            run=run_model_error,
            model_call_id="tool-fail",
            tool_name="python.execute",
            executor="sandbox",
            status=AgentToolCall.STATUS_FAILED,
            arguments_redacted={},
            arguments_hash="b" * 64,
        )

        data = runtime_metrics(fixture["company_a"], {"window_minutes": 60})

        self.assertEqual(data["runCount"], 2)
        self.assertEqual(data["firstTokenMs"]["count"], 1)
        self.assertGreaterEqual(data["totalDurationMs"]["avg"], 0)
        self.assertEqual(data["modelErrorRate"], 0.5)
        self.assertEqual(data["toolErrorRate"], 0.5)

    def _run(self, fixture, request_id, status, *, error_code):
        input_message = TransparentCompanyAgentTestMessage.objects.create(
            session=fixture["session_a1"],
            role=TransparentCompanyAgentTestMessage.ROLE_USER,
            content=request_id,
            created_at="2026-08-14",
            updated_at="2026-08-14",
        )
        return AgentRun.objects.create(
            company=fixture["company_a"],
            request_id=request_id,
            agent=fixture["agent_a1"],
            user=fixture["owner"],
            session=fixture["session_a1"],
            source=AgentRun.SOURCE_AGENT_TEST,
            engine=AgentRun.ENGINE_UNIFIED,
            status=status,
            input_message=input_message,
            model_provider="openai",
            model_name="gpt-test",
            error_code=error_code,
        )
