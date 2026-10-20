"""Runtime state carried through the Phase 1 LangGraph."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from apps.agent_runtime.contracts import ErrorInfo, RunEvent, RunRequest

RunStatus = Literal["PENDING", "RUNNING", "WAITING_TOOL", "CANCELLING", "COMPLETED", "FAILED", "CANCELLED"]


class AgentState(TypedDict, total=False):
    request: RunRequest
    run_id: str
    status: RunStatus
    sequence: int
    events: list[RunEvent]
    model_messages: list[dict[str, Any]]
    memory_items: list[dict[str, Any]]
    output_content: str
    usage: dict[str, int]
    config: dict[str, Any]
    error: ErrorInfo | None
    cancel_requested: bool
    max_execution_seconds: float
    started_monotonic: float
    started_at: str
    completed_at: str
    event_sink: Any
    cancel_checker: Any
    tool_calls: list[dict[str, Any]]
    pending_tool_call: dict[str, Any] | None
    pending_tool_turn: dict[str, Any] | None
    tool_results: list[dict[str, Any]]
    completed_tool_call_keys: set[str]
    tool_round: int
    max_tool_rounds: int
    permissions: frozenset[str]
    enqueue_memory_commit: bool
    grounding_evidence: list[dict[str, Any]]
    grounding_retry_count: int
    grounding_retry_pending: bool
    grounding_blocked: bool
    response_retry_count: int
    response_retry_pending: bool
    response_verified: bool
    request_route: str

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}


def is_terminal(state: AgentState) -> bool:
    return str(state.get("status") or "") in TERMINAL_STATUSES
