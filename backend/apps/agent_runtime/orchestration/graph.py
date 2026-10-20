"""LangGraph assembly and runtime facades."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from contextlib import suppress
from uuid import uuid4

from asgiref.sync import sync_to_async
from langgraph.graph import END, StateGraph

from apps.agent_runtime.contracts import RunEvent, RunRequest
from apps.agent_runtime.memory.nodes import MemoryRecallNode
from apps.agent_runtime.orchestration.events import cancel_run
from apps.agent_runtime.orchestration.nodes import Phase1Nodes
from apps.agent_runtime.orchestration.state import AgentState
from apps.agent_runtime.orchestration.tool_nodes import (
    Phase2Nodes,
    should_continue_after_model,
    should_continue_after_classification,
    should_continue_after_tool,
)
from apps.agent_runtime.providers.model.fake import FakeModelGateway
from apps.agent_runtime.tools.executors import BackendToolExecutor, InMemoryToolCallRecorder, SandboxToolExecutor
from apps.agent_runtime.tools.registry import ToolRouter, default_tool_registry


def build_phase1_graph(model_gateway=None, context_resolver=None):
    nodes = Phase1Nodes(
        model_gateway or FakeModelGateway(),
        context_resolver=context_resolver,
    )
    graph = StateGraph(AgentState)
    graph.add_node("guard", nodes.guard)
    graph.add_node("resolve_config", nodes.resolve_config)
    graph.add_node("recall_stub", nodes.recall_stub)
    graph.add_node("call_model", nodes.call_model)
    graph.add_node("finalize", nodes.finalize)
    graph.set_entry_point("guard")
    graph.add_edge("guard", "resolve_config")
    graph.add_edge("resolve_config", "recall_stub")
    graph.add_edge("recall_stub", "call_model")
    graph.add_edge("call_model", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()


def build_phase2_graph(
    model_gateway=None,
    *,
    context_resolver=None,
    tool_router: ToolRouter | None = None,
    backend_executor: BackendToolExecutor | None = None,
    sandbox_executor: SandboxToolExecutor | None = None,
    tool_recorder: InMemoryToolCallRecorder | None = None,
    memory_gateway=None,
    memory_recall_node=None,
    response_verifier=None,
    request_classifier=None,
):
    if memory_recall_node is None and memory_gateway is not None:
        memory_recall_node = MemoryRecallNode(memory_gateway)

    nodes = Phase2Nodes(
        model_gateway or FakeModelGateway(),
        context_resolver=context_resolver,
        tool_router=tool_router,
        backend_executor=backend_executor,
        sandbox_executor=sandbox_executor,
        tool_recorder=tool_recorder,
        memory_recall_node=memory_recall_node,
        memory_gateway=memory_gateway,
        response_verifier=response_verifier,
        request_classifier=request_classifier,
    )
    graph = StateGraph(AgentState)
    graph.add_node("guard", nodes.guard)
    graph.add_node("resolve_config", nodes.resolve_config)
    graph.add_node("classify_request", nodes.classify_request)
    graph.add_node("recall_memory", nodes.recall_memory)
    graph.add_node("call_model", nodes.call_model)
    graph.add_node("route_tool", nodes.route_tool)
    graph.add_node("execute_tool", nodes.execute_tool)
    graph.add_node("finalize", nodes.finalize)
    # Node names share LangGraph's namespace with AgentState channels. Keep
    # this internal node name distinct from AgentState.enqueue_memory_commit.
    graph.add_node("enqueue_memory_commit_node", nodes.enqueue_memory_commit)
    graph.set_entry_point("guard")
    graph.add_edge("guard", "resolve_config")
    graph.add_edge("resolve_config", "classify_request")
    graph.add_conditional_edges(
        "classify_request",
        should_continue_after_classification,
        {"recall_memory": "recall_memory", "finalize": "finalize"},
    )
    graph.add_edge("recall_memory", "call_model")
    graph.add_conditional_edges(
        "call_model",
        should_continue_after_model,
        {"route_tool": "route_tool", "call_model": "call_model", "finalize": "finalize"},
    )
    graph.add_edge("route_tool", "execute_tool")
    graph.add_conditional_edges(
        "execute_tool",
        should_continue_after_tool,
        {"route_tool": "route_tool", "call_model": "call_model", "finalize": "finalize"},
    )
    graph.add_edge("finalize", "enqueue_memory_commit_node")
    graph.add_edge("enqueue_memory_commit_node", END)
    return graph.compile()


class Phase1AgentRuntimeFacade:
    def __init__(
        self,
        model_gateway=None,
        *,
        context_resolver=None,
        run_id_factory=None,
        max_execution_seconds: float = 30,
        cancel_checker=None,
    ) -> None:
        self.graph = build_phase1_graph(
            model_gateway=model_gateway,
            context_resolver=context_resolver,
        )
        self.run_id_factory = run_id_factory or (lambda _request: str(uuid4()))
        self.max_execution_seconds = max_execution_seconds
        self.cancel_checker = cancel_checker

    async def stream(self, request: RunRequest) -> AsyncIterator[RunEvent]:
        state = self._initial_state(request)
        async for event in _stream_graph_events(self.graph, state):
            yield event

    def _initial_state(self, request: RunRequest) -> AgentState:
        return {
            "request": request,
            "run_id": str(self.run_id_factory(request)),
            "status": "PENDING",
            "sequence": 0,
            "events": [],
            "output_content": "",
            "usage": {},
            "cancel_requested": False,
            "max_execution_seconds": self.max_execution_seconds,
            "cancel_checker": self.cancel_checker,
        }


class Phase2AgentRuntimeFacade(Phase1AgentRuntimeFacade):
    def __init__(
        self,
        model_gateway=None,
        *,
        context_resolver=None,
        run_id_factory=None,
        max_execution_seconds: float = 60,
        max_tool_rounds: int = 8,
        permissions: frozenset[str] | None = None,
        tool_router: ToolRouter | None = None,
        backend_executor: BackendToolExecutor | None = None,
        sandbox_executor: SandboxToolExecutor | None = None,
        tool_recorder: InMemoryToolCallRecorder | None = None,
        memory_gateway=None,
        memory_recall_node=None,
        cancel_checker=None,
        response_verifier=None,
        request_classifier=None,
    ) -> None:
        self.sandbox_executor = sandbox_executor
        self.graph = build_phase2_graph(
            model_gateway=model_gateway,
            context_resolver=context_resolver,
            tool_router=tool_router or ToolRouter(default_tool_registry()),
            backend_executor=backend_executor,
            sandbox_executor=sandbox_executor,
            tool_recorder=tool_recorder,
            memory_gateway=memory_gateway,
            memory_recall_node=memory_recall_node,
            response_verifier=response_verifier,
            request_classifier=request_classifier,
        )
        self.run_id_factory = run_id_factory or (lambda _request: str(uuid4()))
        self.max_execution_seconds = max_execution_seconds
        self.max_tool_rounds = max_tool_rounds
        self.permissions = permissions or frozenset()
        self.cancel_checker = cancel_checker

    async def stream(self, request: RunRequest) -> AsyncIterator[RunEvent]:
        state = self._initial_state(request)
        try:
            async for event in _stream_graph_events(self.graph, state):
                yield event
        finally:
            if self.sandbox_executor is not None:
                await self.sandbox_executor.terminate_run(str(state.get("run_id") or ""))

    def _initial_state(self, request: RunRequest) -> AgentState:
        state = super()._initial_state(request)
        state.update({
            "tool_calls": [],
            "pending_tool_call": None,
            "pending_tool_turn": None,
            "tool_results": [],
            "completed_tool_call_keys": set(),
            "tool_round": 0,
            "max_tool_rounds": self.max_tool_rounds,
            "permissions": self.permissions,
            "enqueue_memory_commit": False,
            "grounding_evidence": [],
            "grounding_retry_count": 0,
            "grounding_retry_pending": False,
            "grounding_blocked": False,
            "response_retry_count": 0,
            "response_retry_pending": False,
            "response_verified": False,
            "request_route": "general",
        })
        return state


async def _stream_graph_events(graph, state: AgentState) -> AsyncIterator[RunEvent]:
    event_queue: asyncio.Queue[RunEvent] = asyncio.Queue()

    def event_sink(event: RunEvent) -> None:
        event_queue.put_nowait(event)

    state["event_sink"] = event_sink
    # LangGraph defaults recursion_limit to 25, which is lower than the number
    # of graph steps required to execute the configured eight tool rounds and
    # then finalize a response. Keep the graph budget derived from the runtime
    # tool-round budget so LangGraph cannot terminate a valid Run first.
    max_tool_rounds = max(0, int(state.get("max_tool_rounds") or 0))
    recursion_limit = max(25, 8 + (max_tool_rounds * 4))
    run_task = asyncio.create_task(
        graph.ainvoke(state, config={"recursion_limit": recursion_limit})
    )
    try:
        while True:
            if run_task.done() and event_queue.empty():
                break
            if await _cancel_requested(state):
                state["cancel_requested"] = True
                cancel_run(state)
                while not event_queue.empty():
                    yield event_queue.get_nowait()
                break
            try:
                yield await asyncio.wait_for(event_queue.get(), timeout=0.05)
            except (asyncio.TimeoutError, TimeoutError):
                continue
        if not run_task.done():
            run_task.cancel()
            with suppress(asyncio.CancelledError):
                await run_task
        else:
            await run_task
    finally:
        if not run_task.done():
            run_task.cancel()
            with suppress(asyncio.CancelledError):
                await run_task


async def _cancel_requested(state: AgentState) -> bool:
    checker = state.get("cancel_checker")
    if checker is None:
        return bool(state.get("cancel_requested"))
    if inspect.iscoroutinefunction(checker):
        result = checker(state)
    else:
        result = await sync_to_async(checker, thread_sensitive=True)(state)
    if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
        result = await result
    return bool(result)
