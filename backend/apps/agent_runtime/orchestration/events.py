"""RunEvent construction helpers for orchestration nodes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from apps.agent_runtime.contracts import ErrorInfo, RunEvent
from apps.agent_runtime.enums import ErrorCode, RunEventType
from apps.agent_runtime.orchestration.state import AgentState

SCHEMA_VERSION = 1


def append_event(state: AgentState, event_type: str | RunEventType, data: dict[str, Any] | None = None) -> AgentState:
    sequence = int(state.get("sequence") or 0) + 1
    event = RunEvent(
        schema_version=SCHEMA_VERSION,
        event_id=str(uuid4()),
        run_id=str(state.get("run_id") or ""),
        sequence=sequence,
        event_type=str(event_type),
        occurred_at=datetime.now(timezone.utc),
        data=dict(data or {}),
    )
    state["sequence"] = sequence
    state["events"] = [*state.get("events", []), event]
    sink = state.get("event_sink")
    if callable(sink):
        sink(event)
    return state


def fail_run(
    state: AgentState,
    *,
    code: str | ErrorCode,
    message: str,
    stage: str,
    retryable: bool = False,
) -> AgentState:
    trace_id = state.get("request").trace_id if state.get("request") else ""
    error = ErrorInfo(str(code), message, retryable, stage, trace_id)
    state["status"] = "FAILED"
    state["error"] = error
    append_event(state, RunEventType.RUN_FAILED, {
        "code": error.code,
        "message": error.message,
        "retryable": error.retryable,
        "stage": error.stage,
        "trace_id": error.trace_id,
        "content": state.get("output_content", ""),
        **{key: max(0, int(value or 0)) for key, value in (state.get("usage") or {}).items()
           if key in {"input_tokens", "output_tokens", "total_tokens"}},
    })
    return state


def cancel_run(state: AgentState) -> AgentState:
    trace_id = state.get("request").trace_id if state.get("request") else ""
    state["status"] = "CANCELLED"
    state["error"] = ErrorInfo(str(ErrorCode.RUN_CANCELLED), "请求已取消", False, "guard", trace_id)
    append_event(state, RunEventType.RUN_CANCELLED, {
        "code": str(ErrorCode.RUN_CANCELLED),
        "message": "请求已取消",
        "trace_id": trace_id,
        "content": state.get("output_content", ""),
    })
    return state

