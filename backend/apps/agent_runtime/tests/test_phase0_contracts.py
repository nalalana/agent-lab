from __future__ import annotations

import asyncio
import json
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from django.test import SimpleTestCase

from apps.agent_runtime.contracts import ModelRequest, RunEvent, RunRequest
from apps.agent_runtime.enums import ErrorCode, ModelEventType, RunEventType
from apps.agent_runtime.providers.model.fake import FakeModelGateway
from apps.agent_runtime.sse import run_events_to_legacy_sse, safe_error_message


class Phase0ContractsTests(SimpleTestCase):
    def test_run_request_contract_fields_are_frozen(self):
        self.assertEqual(
            [field.name for field in fields(RunRequest)],
            [
                "request_id",
                "company_id",
                "agent_id",
                "user_id",
                "session_id",
                "source",
                "user_message_id",
                "locale",
                "trace_id",
            ],
        )

    def test_fake_model_gateway_replays_delta_usage_and_finish(self):
        async def collect():
            request = ModelRequest(
                run_id="run-1",
                provider="fake",
                model="fake-chat",
                messages=[{"role": "user", "content": "hello"}],
                tools=[],
                temperature=None,
                max_output_tokens=None,
                timeout_seconds=30,
                credential_ref="fake",
            )
            return [event async for event in FakeModelGateway(chunk_text="hello").stream(request)]

        events = asyncio.run(collect())

        self.assertEqual(
            [str(event.event_type) for event in events],
            [
                str(ModelEventType.TEXT_DELTA),
                str(ModelEventType.TEXT_DELTA),
                str(ModelEventType.USAGE),
                str(ModelEventType.FINISH),
            ],
        )
        self.assertEqual("hello", "".join(event.data.get("delta", "") for event in events))

    def test_run_events_adapt_to_public_client_sse_contract(self):
        run_id = str(uuid4())
        now = datetime.now(timezone.utc)
        events = [
            RunEvent(1, str(uuid4()), run_id, 1, str(RunEventType.RUN_STARTED), now, {
                "conversation_id": "conv-public-1",
                "session_key": "session-public-1",
                "agent": {"agent_id": "support-agent", "entry_key": "__default__"},
                "user_message": {"role": "user", "content": "hello"},
            }),
            RunEvent(1, str(uuid4()), run_id, 2, str(RunEventType.MESSAGE_DELTA), now, {"delta": "Hi"}),
            RunEvent(1, str(uuid4()), run_id, 3, str(RunEventType.MESSAGE_COMPLETED), now, {
                "content": "Hi",
                "token_count": 3,
                "conversation_id": "conv-public-1",
                "session_key": "session-public-1",
                "agent": {"agent_id": "support-agent", "entry_key": "__default__"},
                "tool_calls": [],
                "assistant_message": {"role": "assistant", "content": "Hi"},
                "conversation": {"conversationId": "conv-public-1"},
            }),
        ]

        self.assertEqual(run_events_to_legacy_sse(events), _fixture("sse_public_client_success.json"))

    def test_run_events_adapt_to_agent_test_sse_contract(self):
        run_id = str(uuid4())
        now = datetime.now(timezone.utc)
        events = [
            RunEvent(1, str(uuid4()), run_id, 1, str(RunEventType.RUN_STARTED), now, {
                "conversation": {"conversationId": "conv-agent-test-1"},
                "user_message": {"role": "user", "content": "hello"},
            }),
            RunEvent(1, str(uuid4()), run_id, 2, str(RunEventType.WARNING), now, {
                "message": "Runtime 已连接，正在等待 Agent 输出。",
            }),
            RunEvent(1, str(uuid4()), run_id, 3, str(RunEventType.MESSAGE_DELTA), now, {"delta": "Hi"}),
            RunEvent(1, str(uuid4()), run_id, 4, str(RunEventType.MESSAGE_COMPLETED), now, {
                "assistant_message": {"role": "assistant", "content": "Hi"},
                "conversation": {"conversationId": "conv-agent-test-1"},
                "token_count": 3,
                "tool_calls": [],
            }),
        ]

        self.assertEqual(run_events_to_legacy_sse(events), _fixture("sse_agent_test_success.json"))

    def test_error_codes_map_to_safe_user_messages(self):
        self.assertEqual(safe_error_message(ErrorCode.MODEL_TIMEOUT), "模型响应超时，请稍后重试")
        self.assertEqual(safe_error_message("UNKNOWN", "fallback"), "fallback")


def _fixture(name: str):
    path = Path(__file__).resolve().parents[1] / "fixtures" / name
    return json.loads(path.read_text(encoding="utf-8-sig"))
