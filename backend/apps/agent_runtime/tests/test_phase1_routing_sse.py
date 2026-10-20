"""Phase 1-C tests for stable routing and legacy stream compatibility."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.agent_runtime.contracts import RunEvent
from apps.agent_runtime.enums import ErrorCode, RunEventType
from apps.agent_runtime.models import CompanyAgentRuntimePolicy
from apps.agent_runtime.routing import RuntimeEngineRouter
from apps.agent_runtime.sse import (
    iter_legacy_runtime_events,
    run_event_to_legacy_sse,
)


class RuntimeEngineRouterTests(SimpleTestCase):
    def test_missing_policy_is_legacy(self):
        route = RuntimeEngineRouter().select_policy(
            None,
            company_id=10,
            session_id=20,
        )

        self.assertEqual(route.engine, "openclaw")
        self.assertEqual(route.policy_mode, "legacy")
        self.assertEqual(route.reason, "missing_policy")

    def test_canary_choice_is_stable_for_company_and_session(self):
        policy = SimpleNamespace(
            mode=CompanyAgentRuntimePolicy.MODE_CANARY,
            canary_percentage=37,
            config_version=9,
        )
        router = RuntimeEngineRouter()

        first = router.select_policy(policy, company_id="company-a", session_id="session-a")
        second = router.select_policy(policy, company_id="company-a", session_id="session-a")

        self.assertEqual(first, second)
        self.assertGreaterEqual(first.canary_bucket, 0)
        self.assertLess(first.canary_bucket, 100)

    def test_canary_zero_and_one_hundred_cover_boundary(self):
        router = RuntimeEngineRouter()
        zero = SimpleNamespace(mode="canary", canary_percentage=0, config_version=1)
        full = SimpleNamespace(mode="canary", canary_percentage=100, config_version=1)

        self.assertEqual(
            router.select_policy(zero, company_id=1, session_id=1).engine,
            "openclaw",
        )
        self.assertEqual(
            router.select_policy(full, company_id=1, session_id=1).engine,
            "unified",
        )


class LegacyStreamAdapterTests(SimpleTestCase):
    def test_async_run_events_are_bridged_to_flat_legacy_events(self):
        events = list(iter_legacy_runtime_events(_successful_events(), max_queue_size=2))

        self.assertEqual([item["event"] for item in events], ["start", "delta", "complete"])
        self.assertEqual(events[1]["delta"], "hello")
        self.assertEqual(events[2]["content"], "hello")
        self.assertEqual(events[2]["run_id"], "run-1")

    def test_failure_adapter_uses_safe_message_not_provider_details(self):
        event = _event(
            RunEventType.RUN_FAILED,
            1,
            {
                "code": str(ErrorCode.MODEL_AUTH_FAILED),
                "message": "upstream said sk-secret-value",
            },
        )

        flat = list(iter_legacy_runtime_events(_single_event(event)))[0]
        nested = run_event_to_legacy_sse(event)

        self.assertEqual(flat["message"], "模型配置不可用")
        self.assertEqual(nested["data"]["message"], "模型配置不可用")
        self.assertNotIn("sk-secret-value", str(flat))


async def _successful_events():
    yield _event(RunEventType.RUN_STARTED, 1, {})
    yield _event(RunEventType.MESSAGE_DELTA, 2, {"delta": "hello", "content": "hello"})
    yield _event(
        RunEventType.MESSAGE_COMPLETED,
        3,
        {"content": "hello", "token_count": 4, "tool_calls": []},
    )
    yield _event(RunEventType.RUN_COMPLETED, 4, {"input_tokens": 2, "output_tokens": 2})


async def _single_event(event):
    yield event


def _event(event_type, sequence, data):
    return RunEvent(
        schema_version=1,
        event_id=f"event-{sequence}",
        run_id="run-1",
        sequence=sequence,
        event_type=str(event_type),
        occurred_at=datetime.now(timezone.utc),
        data=data,
    )
