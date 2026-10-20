"""Phase 1 graph nodes for the unified agent runtime."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from apps.agent_runtime.contracts import ModelRequest
from apps.agent_runtime.enums import ErrorCode, ModelEventType, RunEventType
from apps.agent_runtime.orchestration.events import append_event, cancel_run, fail_run
from apps.agent_runtime.orchestration.state import AgentState, is_terminal


UNVERIFIED_TOOL_CLAIM_MESSAGE = (
    "本次运行没有可核验的成功工具调用记录，因此无法确认所请求的操作已经完成。"
)
MEMORY_COMMIT_PENDING_MESSAGE = (
    "该信息可由运行时进行异步记忆提取，但当前没有持久化完成回执，"
    "因此不能声称已经写入长期记忆。"
)
_TOOL_SUCCESS_CLAIM_RE = re.compile(
    r"(?:已|已经)(?:成功)?(?:调用|执行|运行|读取|写入|保存|发布)"
    r"|\b(?:successfully\s+)?(?:executed|ran|wrote|saved|published)\b"
    # 'read' also names a permission/tool and the present-tense capability.
    # Require an affirmative subject/auxiliary or an explicit success marker.
    r"|\b(?:(?:I|we)(?:['’]ve|\s+(?:have|had))?|has|have|had|was|were|been)"
    r"\s+(?:(?:already|successfully)\s+)*read\b"
    r"|\bsuccessfully\s+read\b",
    re.IGNORECASE,
)
_MEMORY_SUCCESS_CLAIM_RE = re.compile(
    r"(?:已|已经)(?:成功)?(?:写入|保存到?|存入|记住)(?:长期)?记忆"
    r"|\b(?:saved|written|stored).{0,24}long[- ]term memory\b",
    re.IGNORECASE,
)


def _tool_call_receipts(state: AgentState) -> list[dict[str, Any]]:
    receipts = []
    for result in state.get("tool_results") or []:
        if not isinstance(result, dict):
            continue
        receipt = {
            "name": str(result.get("name") or ""),
            "status": "completed" if result.get("success") is True else "failed",
        }
        tool_call_id = str(result.get("tool_call_id") or "")
        model_call_id = str(result.get("model_call_id") or "")
        if tool_call_id:
            receipt["tool_call_id"] = tool_call_id
        if model_call_id:
            receipt["model_call_id"] = model_call_id
        if receipt["name"]:
            receipts.append(receipt)
    return receipts


def _runtime_truthfulness_issue(state: AgentState, content: str) -> str | None:
    if _MEMORY_SUCCESS_CLAIM_RE.search(content):
        return "memory_commit_unconfirmed"
    has_successful_tool = any(
        isinstance(result, dict) and result.get("success") is True
        for result in state.get("tool_results") or []
    )
    if not has_successful_tool and _TOOL_SUCCESS_CLAIM_RE.search(content):
        return "tool_execution_unconfirmed"
    return None


def _enforce_runtime_truthfulness(state: AgentState, content: str) -> str:
    issue = _runtime_truthfulness_issue(state, content)
    if issue == "memory_commit_unconfirmed":
        append_event(state, RunEventType.WARNING, {
            "message": "已阻止未经确认的长期记忆成功声明。",
        })
        return MEMORY_COMMIT_PENDING_MESSAGE
    if issue == "tool_execution_unconfirmed":
        append_event(state, RunEventType.WARNING, {
            "message": "已阻止缺少工具回执的执行成功声明。",
        })
        return UNVERIFIED_TOOL_CLAIM_MESSAGE
    return content


class Phase1Nodes:
    def __init__(self, model_gateway, context_resolver=None) -> None:
        self.model_gateway = model_gateway
        self.context_resolver = context_resolver

    def guard(self, state: AgentState) -> AgentState:
        if is_terminal(state):
            return state
        if state.get("cancel_requested"):
            return cancel_run(state)
        if _deadline_exceeded(state):
            return fail_run(state, code=ErrorCode.MODEL_TIMEOUT, message="运行超过最大执行时间", stage="guard", retryable=True)

        request = state["request"]
        state["status"] = "RUNNING"
        state["started_monotonic"] = state.get("started_monotonic") or time.monotonic()
        append_event(state, RunEventType.RUN_STARTED, {
            "request_id": request.request_id,
            "company_id": str(request.company_id),
            "agent_id": str(request.agent_id),
            "session_id": str(request.session_id),
            "source": request.source,
            "trace_id": request.trace_id,
        })
        return state

    def resolve_config(self, state: AgentState) -> AgentState:
        if is_terminal(state):
            return state
        if state.get("cancel_requested"):
            return cancel_run(state)
        if _deadline_exceeded(state):
            return fail_run(state, code=ErrorCode.MODEL_TIMEOUT, message="运行超过最大执行时间", stage="resolve_config", retryable=True)

        request = state["request"]
        try:
            if self.context_resolver is not None:
                context = self.context_resolver(request)
                state["config"] = dict(context.get("config") or {})
                state["model_messages"] = list(context.get("model_messages") or [])
            else:
                # The deterministic fallback keeps graph tests independent from
                # Django configuration and external model credentials.
                content = str(getattr(request, "user_message_id", "") or "")
                state["config"] = {
                    "model_provider": "fake",
                    "model_name": "fake-chat",
                    "credential_ref": "fake",
                    "temperature": None,
                    "max_output_tokens": None,
                }
                state["model_messages"] = state.get("model_messages") or [
                    {"role": "user", "content": content},
                ]
        except Exception as exc:
            return fail_run(
                state,
                code=getattr(exc, "code", ErrorCode.INTERNAL_ERROR),
                message=getattr(exc, "safe_message", "运行配置不可用"),
                stage="resolve_config",
                retryable=bool(getattr(exc, "retryable", False)),
            )
        return state

    def recall_stub(self, state: AgentState) -> AgentState:
        if is_terminal(state):
            return state
        if state.get("cancel_requested"):
            return cancel_run(state)
        if _deadline_exceeded(state):
            return fail_run(state, code=ErrorCode.MODEL_TIMEOUT, message="运行超过最大执行时间", stage="recall_stub", retryable=True)

        state["memory_items"] = []
        return state

    async def call_model(self, state: AgentState) -> AgentState:
        if is_terminal(state):
            return state
        if state.get("cancel_requested"):
            return cancel_run(state)
        if _deadline_exceeded(state):
            return fail_run(state, code=ErrorCode.MODEL_TIMEOUT, message="运行超过最大执行时间", stage="call_model", retryable=True)

        config = state.get("config") or {}
        model_request = ModelRequest(
            run_id=str(state.get("run_id") or ""),
            provider=str(config.get("model_provider") or "fake"),
            model=str(config.get("model_name") or "fake-chat"),
            messages=list(state.get("model_messages") or []),
            tools=[],
            temperature=config.get("temperature"),
            max_output_tokens=config.get("max_output_tokens"),
            timeout_seconds=int(float(state.get("max_execution_seconds") or 30)),
            credential_ref=str(config.get("credential_ref") or "fake"),
        )

        try:
            model_events = self.model_gateway.stream(model_request)
            while True:
                try:
                    model_event = await asyncio.wait_for(anext(model_events), timeout=_remaining_seconds(state))
                except StopAsyncIteration:
                    break
                except (asyncio.TimeoutError, TimeoutError):
                    return fail_run(state, code=ErrorCode.MODEL_TIMEOUT, message="模型响应超时", stage="call_model", retryable=True)

                if state.get("cancel_requested"):
                    return cancel_run(state)
                if _deadline_exceeded(state):
                    return fail_run(state, code=ErrorCode.MODEL_TIMEOUT, message="运行超过最大执行时间", stage="call_model", retryable=True)

                event_type = str(model_event.event_type)
                data = dict(model_event.data or {})
                if event_type == str(ModelEventType.TEXT_DELTA):
                    delta = str(data.get("delta") or "")
                    if not delta:
                        continue
                    state["output_content"] = f"{state.get('output_content', '')}{delta}"
                    append_event(state, RunEventType.MESSAGE_DELTA, {
                        "delta": delta,
                        "content": state["output_content"],
                    })
                elif event_type == str(ModelEventType.USAGE):
                    state["usage"] = {
                        "input_tokens": int(data.get("input_tokens") or 0),
                        "output_tokens": int(data.get("output_tokens") or 0),
                        "total_tokens": int(data.get("total_tokens") or 0),
                    }
                elif event_type == str(ModelEventType.ERROR):
                    return fail_run(
                        state,
                        code=str(data.get("code") or ErrorCode.INTERNAL_ERROR),
                        message=str(data.get("message") or "模型调用失败"),
                        stage="call_model",
                        retryable=bool(data.get("retryable")),
                    )
        except Exception:
            # SDK details can contain endpoints or response fragments.  Keep
            # them out of RunEvent and expose only a stable safe message.
            return fail_run(
                state,
                code=ErrorCode.INTERNAL_ERROR,
                message="模型调用失败，请稍后重试",
                stage="call_model",
                retryable=False,
            )
        return state

    def finalize(self, state: AgentState) -> AgentState:
        if is_terminal(state):
            return state
        if state.get("cancel_requested"):
            return cancel_run(state)
        if _deadline_exceeded(state):
            return fail_run(state, code=ErrorCode.MODEL_TIMEOUT, message="运行超过最大执行时间", stage="finalize", retryable=True)

        content = str(state.get("output_content") or "")
        if not content:
            return fail_run(state, code=ErrorCode.INTERNAL_ERROR, message="模型未返回内容", stage="finalize", retryable=True)

        if not state.get("response_verified"):
            content = _enforce_runtime_truthfulness(state, content)
        state["output_content"] = content
        usage = state.get("usage") or {}
        state["status"] = "COMPLETED"
        append_event(state, RunEventType.MESSAGE_COMPLETED, {
            "content": content,
            "token_count": int(usage.get("total_tokens") or usage.get("output_tokens") or 0),
            "tool_calls": _tool_call_receipts(state),
        })
        append_event(state, RunEventType.RUN_COMPLETED, {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        })
        return state


def _deadline_exceeded(state: AgentState) -> bool:
    max_seconds = float(state.get("max_execution_seconds") or 0)
    started = float(state.get("started_monotonic") or 0)
    return bool(max_seconds and started and time.monotonic() - started > max_seconds)


def _remaining_seconds(state: AgentState) -> float | None:
    max_seconds = float(state.get("max_execution_seconds") or 0)
    started = float(state.get("started_monotonic") or 0)
    if not max_seconds or not started:
        return None
    return max(max_seconds - (time.monotonic() - started), 0.001)
