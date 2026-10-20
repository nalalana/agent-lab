from __future__ import annotations

import asyncio
import time
from uuid import uuid4

from django.test import SimpleTestCase

from apps.agent_runtime.contracts import ModelEvent, RunRequest
from apps.agent_runtime.enums import ErrorCode, ModelEventType, RunEventType
from apps.agent_runtime.orchestration.graph import Phase1AgentRuntimeFacade, build_phase1_graph
from apps.agent_runtime.orchestration.nodes import Phase1Nodes
from apps.agent_runtime.orchestration.state import AgentState
from apps.agent_runtime.providers.model.fake import FakeModelGateway


class Phase1RuntimeGraphTests(SimpleTestCase):
    def test_phase1_facade_emits_started_delta_message_completed_and_run_completed(self):
        async def collect():
            request = _run_request(user_message_id="hello")
            facade = Phase1AgentRuntimeFacade(FakeModelGateway(chunk_text="hello"))
            return [event async for event in facade.stream(request)]

        events = asyncio.run(collect())

        self.assertEqual(
            [event.event_type for event in events],
            [
                str(RunEventType.RUN_STARTED),
                str(RunEventType.MESSAGE_DELTA),
                str(RunEventType.MESSAGE_DELTA),
                str(RunEventType.MESSAGE_COMPLETED),
                str(RunEventType.RUN_COMPLETED),
            ],
        )
        self.assertEqual([event.sequence for event in events], [1, 2, 3, 4, 5])
        self.assertEqual(events[-2].data["content"], "hello")
        self.assertEqual(events[-2].data["token_count"], 14)

    def test_graph_reaches_completed_terminal_status(self):
        async def invoke():
            graph = build_phase1_graph(FakeModelGateway(chunk_text="done"))
            return await graph.ainvoke(_initial_state())

        final_state = asyncio.run(invoke())

        self.assertEqual(final_state["status"], "COMPLETED")
        self.assertEqual(final_state["output_content"], "done")
        self.assertEqual(final_state["usage"]["total_tokens"], 14)

    def test_cancel_requested_stops_before_model_call(self):
        class FailingGateway:
            async def stream(self, request):
                raise AssertionError("model gateway should not be called after cancellation")

        async def invoke():
            graph = build_phase1_graph(FailingGateway())
            state = _initial_state()
            state["cancel_requested"] = True
            return await graph.ainvoke(state)

        final_state = asyncio.run(invoke())

        self.assertEqual(final_state["status"], "CANCELLED")
        self.assertEqual([event.event_type for event in final_state["events"]], [str(RunEventType.RUN_CANCELLED)])

    def test_deadline_exceeded_fails_safely(self):
        async def invoke():
            graph = build_phase1_graph(FakeModelGateway(chunk_text="late"))
            state = _initial_state()
            state["started_monotonic"] = time.monotonic() - 10
            state["max_execution_seconds"] = 0.001
            return await graph.ainvoke(state)

        final_state = asyncio.run(invoke())

        self.assertEqual(final_state["status"], "FAILED")
        self.assertEqual(final_state["error"].code, str(ErrorCode.MODEL_TIMEOUT))
        self.assertEqual(final_state["events"][-1].event_type, str(RunEventType.RUN_FAILED))

    def test_model_error_event_fails_run(self):
        gateway = FakeModelGateway(events=[
            ModelEvent(str(ModelEventType.ERROR), {
                "code": str(ErrorCode.MODEL_RATE_LIMITED),
                "message": "rate limited",
                "retryable": True,
            })
        ])

        async def invoke():
            graph = build_phase1_graph(gateway)
            return await graph.ainvoke(_initial_state())

        final_state = asyncio.run(invoke())

        self.assertEqual(final_state["status"], "FAILED")
        self.assertEqual(final_state["error"].code, str(ErrorCode.MODEL_RATE_LIMITED))
        self.assertEqual(final_state["events"][-1].data["message"], "rate limited")

    def test_facade_streams_model_delta_before_model_node_completes(self):
        async def collect_until_first_delta():
            gateway = BlockingGateway()
            facade = Phase1AgentRuntimeFacade(gateway, max_execution_seconds=1)
            stream = facade.stream(_run_request(user_message_id="hello"))
            first = await asyncio.wait_for(anext(stream), timeout=0.2)
            second = await asyncio.wait_for(anext(stream), timeout=0.2)
            gateway.release.set()
            rest = [event async for event in stream]
            return [first, second, *rest]

        events = asyncio.run(collect_until_first_delta())

        self.assertEqual(events[0].event_type, str(RunEventType.RUN_STARTED))
        self.assertEqual(events[1].event_type, str(RunEventType.MESSAGE_DELTA))
        self.assertEqual(events[1].data["content"], "A")
        self.assertEqual(events[-2].data["content"], "AB")

    def test_model_stream_is_protected_by_hard_timeout(self):
        async def collect():
            facade = Phase1AgentRuntimeFacade(HangingGateway(), max_execution_seconds=0.02)
            return [event async for event in facade.stream(_run_request(user_message_id="hello"))]

        events = asyncio.run(collect())

        self.assertEqual(events[-1].event_type, str(RunEventType.RUN_FAILED))
        self.assertEqual(events[-1].data["code"], str(ErrorCode.MODEL_TIMEOUT))


class Phase1NodeTests(SimpleTestCase):
    def test_guard_starts_run_and_emits_started_event(self):
        state = _initial_state()
        nodes = Phase1Nodes(FakeModelGateway())

        next_state = nodes.guard(state)

        self.assertEqual(next_state["status"], "RUNNING")
        self.assertEqual(next_state["sequence"], 1)
        self.assertEqual(next_state["events"][0].event_type, str(RunEventType.RUN_STARTED))
        self.assertTrue(next_state.get("started_monotonic"))

    def test_resolve_config_builds_fake_model_request_context(self):
        state = _initial_state()
        state["status"] = "RUNNING"
        nodes = Phase1Nodes(FakeModelGateway())

        next_state = nodes.resolve_config(state)

        self.assertEqual(next_state["config"]["model_provider"], "fake")
        self.assertEqual(next_state["config"]["model_name"], "fake-chat")
        self.assertEqual(next_state["model_messages"], [{"role": "user", "content": "hello"}])

    def test_recall_stub_records_empty_memory_items(self):
        state = _initial_state()
        state["status"] = "RUNNING"
        nodes = Phase1Nodes(FakeModelGateway())

        next_state = nodes.recall_stub(state)

        self.assertEqual(next_state["memory_items"], [])

    def test_call_model_streams_text_and_usage_into_state(self):
        async def invoke():
            state = _initial_state()
            state["status"] = "RUNNING"
            state["started_monotonic"] = time.monotonic()
            state["config"] = {"model_provider": "fake", "model_name": "fake-chat", "credential_ref": "fake"}
            state["model_messages"] = [{"role": "user", "content": "hello"}]
            nodes = Phase1Nodes(FakeModelGateway(chunk_text="node"))
            return await nodes.call_model(state)

        next_state = asyncio.run(invoke())

        self.assertEqual(next_state["output_content"], "node")
        self.assertEqual(next_state["usage"]["total_tokens"], 14)
        self.assertEqual(
            [event.event_type for event in next_state["events"]],
            [str(RunEventType.MESSAGE_DELTA), str(RunEventType.MESSAGE_DELTA)],
        )

    def test_finalize_completes_run_and_appends_terminal_events(self):
        state = _initial_state()
        state["status"] = "RUNNING"
        state["started_monotonic"] = time.monotonic()
        state["output_content"] = "answer"
        state["usage"] = {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}
        nodes = Phase1Nodes(FakeModelGateway())

        next_state = nodes.finalize(state)

        self.assertEqual(next_state["status"], "COMPLETED")
        self.assertEqual(
            [event.event_type for event in next_state["events"]],
            [str(RunEventType.MESSAGE_COMPLETED), str(RunEventType.RUN_COMPLETED)],
        )
        self.assertEqual(next_state["events"][0].data["token_count"], 5)


class BlockingGateway:
    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def stream(self, request):
        yield ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "A"})
        await self.release.wait()
        yield ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "B"})
        yield ModelEvent(str(ModelEventType.USAGE), {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3})
        yield ModelEvent(str(ModelEventType.FINISH), {"reason": "stop"})


class HangingGateway:
    async def stream(self, request):
        await asyncio.sleep(1)
        yield ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "late"})


def _run_request(**overrides) -> RunRequest:
    values = {
        "request_id": str(uuid4()),
        "company_id": "company-a",
        "agent_id": "agent-a1",
        "user_id": "user-a",
        "session_id": "session-a",
        "source": "agent_test",
        "user_message_id": "hello",
        "locale": "zh-CN",
        "trace_id": str(uuid4()),
    }
    values.update(overrides)
    return RunRequest(**values)


def _initial_state() -> AgentState:
    return {
        "request": _run_request(user_message_id="hello"),
        "run_id": str(uuid4()),
        "status": "PENDING",
        "sequence": 0,
        "events": [],
        "output_content": "",
        "usage": {},
        "cancel_requested": False,
        "max_execution_seconds": 30,
    }
