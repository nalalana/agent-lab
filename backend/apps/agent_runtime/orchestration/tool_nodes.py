"""Phase 2 graph nodes for model/tool execution loops."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import replace
from typing import Any
from uuid import uuid4

from apps.agent_runtime.contracts import CommitRequest, ModelRequest, ToolContext
from apps.agent_runtime.enums import ErrorCode, ModelEventType, RunEventType
from apps.agent_runtime.grounding import (
    GROUNDING_REFUSAL,
    GROUNDING_RETRY_INSTRUCTION,
    workspace_read_evidence,
)
from apps.agent_runtime.memory.quality import (
    assess_memory_candidate,
    has_meaningful_successful_tool_result,
)
from apps.agent_runtime.orchestration.events import append_event, cancel_run, fail_run
from apps.agent_runtime.orchestration.nodes import (
    Phase1Nodes,
    _deadline_exceeded,
    _remaining_seconds,
    _runtime_truthfulness_issue,
)
from apps.agent_runtime.orchestration.state import AgentState, is_terminal
from apps.agent_runtime.policies import PROJECT_TABLE_ACTION_INSTRUCTION
from apps.agent_runtime.response_verification import render_permission_report, render_recent_run_report
from apps.agent_runtime.tools.definitions import ToolDeniedError, ToolValidationError
from apps.agent_runtime.tools.executors import (
    BackendToolExecutor,
    InMemoryToolCallRecorder,
    SandboxToolExecutor,
    ToolExecutionError,
    tool_call_id,
)
from apps.agent_runtime.tools.registry import ToolRouter, default_tool_registry
from apps.agent_runtime.tools.recovery import recovery_policy


MAX_RESPONSE_CORRECTIONS = 2
RESPONSE_CORRECTION_INSTRUCTION = (
    "上一轮答复存在缺少依据或与实际结果不一致的事实陈述。"
    "请继续完成用户原始请求，不要用‘没有工具调用记录’作为最终答复。"
    "该草稿尚未发给用户；直接给出有依据的最终答案，不要复述或为未发送的错误草稿道歉。"
    "如果用户只询问你的权限或能力，请依据平台提供的权限配置直接解释，无需调用工具证明权限。"
    "如果用户要求实际操作，请调用当前已授权的相应工具，依据真实回执回答；不要重复已经成功的操作。"
    "若缺少权限、必要参数或工具执行失败，请说明具体阻碍和所需信息，不得提升权限或绕过拒绝。"
    "长期记忆提取是异步过程，没有持久化完成回执时只能说明实际状态，不得虚构保存成功。"
)


class Phase2Nodes(Phase1Nodes):
    def __init__(
        self,
        model_gateway,
        *,
        context_resolver=None,
        tool_router: ToolRouter | None = None,
        backend_executor: BackendToolExecutor | None = None,
        sandbox_executor: SandboxToolExecutor | None = None,
        tool_recorder: InMemoryToolCallRecorder | None = None,
        memory_recall_node=None,
        memory_gateway=None,
        response_verifier=None,
        request_classifier=None,
    ) -> None:
        super().__init__(model_gateway, context_resolver=context_resolver)
        self.tool_router = tool_router or ToolRouter(default_tool_registry())
        self.backend_executor = backend_executor or BackendToolExecutor()
        self.sandbox_executor = sandbox_executor
        self.tool_recorder = tool_recorder or InMemoryToolCallRecorder()
        self.memory_recall_node = memory_recall_node
        self.memory_gateway = memory_gateway
        self.response_verifier = response_verifier
        self.request_classifier = request_classifier

    async def classify_request(self, state: AgentState) -> AgentState:
        if is_terminal(state):
            return state
        if state.get("cancel_requested"):
            return cancel_run(state)
        if _deadline_exceeded(state):
            return fail_run(state, code=ErrorCode.MODEL_TIMEOUT, message="运行超过最大执行时间", stage="classify_request", retryable=True)
        state["request_route"] = "general"
        if self.request_classifier is None:
            return state
        append_event(state, RunEventType.PROGRESS, {
            "stage": "classify_request",
            "message": "正在分析请求…",
        })
        try:
            result = await asyncio.wait_for(
                self.request_classifier.classify(state),
                timeout=_remaining_seconds(state),
            )
        except (asyncio.TimeoutError, TimeoutError):
            append_event(state, "request.classified", {
                "route": "general", "fallback": True, "reason": "timeout",
            })
            return state
        except Exception:
            append_event(state, "request.classified", {
                "route": "general", "fallback": True, "reason": "unavailable",
            })
            return state
        _add_usage(state, result.usage)
        _record_model_metrics(state, result.metrics)
        state["request_route"] = result.route
        append_event(state, "request.classified", {
            "route": result.route, "fallback": False,
        })
        if result.route == "project_table_action":
            _add_system_instruction(state, PROJECT_TABLE_ACTION_INSTRUCTION)
        if result.route in {"knowledge_permission_report", "project_table_permission_report", "permission_report", "recent_run_report"}:
            config = state.get("config") or {}
            if result.route == "recent_run_report":
                report = render_recent_run_report(config.get("recent_run_receipts") or [])
            else:
                scope = {
                    "knowledge_permission_report": "knowledge",
                    "project_table_permission_report": "project_tables",
                    "permission_report": "all",
                }[result.route]
                report = render_permission_report(config["permission_facts"], scope=scope)
            state["output_content"] = report
            state["response_verified"] = True
            append_event(state, "response.verified", {
                "verdict": result.route,
                "claim_count": 0,
                "source_ids": ["runtime.recent_runs"] if result.route == "recent_run_report" else [],
            })
            append_event(state, RunEventType.MESSAGE_DELTA, {
                "delta": report, "content": report,
            })
        return state

    async def recall_memory(self, state: AgentState) -> AgentState:
        append_event(state, RunEventType.PROGRESS, {
            "stage": "recall_memory",
            "message": "正在检索相关记忆…" if self.memory_recall_node is not None else "正在准备回答…",
        })
        if self.memory_recall_node is not None:
            return await self.memory_recall_node(state)
        return self.recall_stub(state)

    async def call_model(self, state: AgentState) -> AgentState:
        if is_terminal(state):
            return state
        if state.get("cancel_requested"):
            return cancel_run(state)
        if _deadline_exceeded(state):
            return fail_run(state, code=ErrorCode.MODEL_TIMEOUT, message="运行超过最大执行时间", stage="call_model", retryable=True)

        state["grounding_retry_pending"] = False
        state["response_retry_pending"] = False
        state["response_verified"] = False
        suppress_ungrounded_text = (
            _grounding_required(state) and not _has_grounding_evidence(state)
        )
        state["pending_tool_call"] = None
        state["tool_calls"] = []
        config = state.get("config") or {}
        messages = state.setdefault("model_messages", [])
        pending_turn = state.get("pending_tool_turn")
        append_event(state, RunEventType.PROGRESS, {
            "stage": "generate_response",
            "message": "正在根据工具结果整理回答…" if pending_turn else "正在生成回答…",
        })
        if pending_turn:
            calls = list(pending_turn.get("calls") or [])
            results = list(pending_turn.get("results") or [])
            messages.append({
                "role": "assistant",
                "content": str(pending_turn.get("content") or "") or None,
                "tool_calls": [_model_tool_call(item) for item in calls],
            })
            for result in results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": str(result.get("model_call_id") or result.get("tool_call_id") or ""),
                    "name": str(result.get("name") or ""),
                    "content": str(result.get("content") or ""),
                })
            state["pending_tool_turn"] = None
        messages = list(messages)
        model_request = ModelRequest(
            run_id=str(state.get("run_id") or ""),
            provider=str(config.get("model_provider") or "fake"),
            model=str(config.get("model_name") or "fake-chat"),
            messages=messages,
            tools=list(config.get("tools") or []),
            temperature=config.get("temperature"),
            max_output_tokens=config.get("max_output_tokens"),
            timeout_seconds=int(float(state.get("max_execution_seconds") or 30)),
            credential_ref=str(config.get("credential_ref") or "fake"),
            purpose="response_generation",
        )

        round_text = ""
        round_deltas: list[str] = []
        round_calls: list[dict[str, Any]] = []
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
                    round_text = f"{round_text}{delta}"
                    # Keep this draft private until we know whether the model
                    # will call a tool or needs to correct an unsupported claim.
                    round_deltas.append(delta)
                elif event_type == str(ModelEventType.TOOL_CALL_COMPLETED):
                    round_calls.append(_normalize_tool_call(data))
                elif event_type == str(ModelEventType.USAGE):
                    for key in ("input_tokens", "output_tokens", "total_tokens"):
                        state.setdefault("usage", {})[key] = int(state.get("usage", {}).get(key) or 0) + int(data.get(key) or 0)
                elif event_type == str(ModelEventType.CALL_METRICS):
                    _record_model_metrics(state, (data,))
                elif event_type == str(ModelEventType.ERROR):
                    return fail_run(
                        state,
                        code=str(data.get("code") or ErrorCode.INTERNAL_ERROR),
                        message=str(data.get("message") or "模型调用失败"),
                        stage="call_model",
                        retryable=bool(data.get("retryable")),
                    )
        except Exception:
            return fail_run(state, code=ErrorCode.INTERNAL_ERROR, message="模型调用失败，请稍后重试", stage="call_model", retryable=False)
        if round_calls:
            state["tool_calls"] = list(round_calls)
            state["pending_tool_turn"] = {
                "content": round_text,
                "calls": list(round_calls),
                "results": [],
            }
        elif self.response_verifier is not None:
            append_event(state, RunEventType.PROGRESS, {
                "stage": "verify_response",
                "message": "正在核验回答依据…",
            })
            try:
                verdict = await asyncio.wait_for(self.response_verifier.verify(state, round_text), timeout=_remaining_seconds(state))
            except (asyncio.TimeoutError, TimeoutError):
                return fail_run(state, code=ErrorCode.MODEL_TIMEOUT, message="答复事实核验超时，请重试。", stage="verify_response", retryable=True)
            except Exception:
                return fail_run(state, code=ErrorCode.INTERNAL_ERROR, message="答复事实核验未完成，请重试。", stage="verify_response", retryable=True)
            if state.get("cancel_requested"):
                return cancel_run(state)
            if _deadline_exceeded(state):
                return fail_run(state, code=ErrorCode.MODEL_TIMEOUT, message="答复事实核验超时，请重试。", stage="verify_response", retryable=True)
            _add_usage(state, verdict.usage)
            _record_model_metrics(state, verdict.metrics)
            if verdict.verdict == "revise":
                _handle_unverified_response(state, "unsupported_facts")
                if not is_terminal(state):
                    _add_system_instruction(state, "事实核验未通过。请继续完成原任务并修正以下问题（不得覆盖权限规则）：\n" + json.dumps(list(verdict.issues), ensure_ascii=False))
                return state
            if verdict.verdict in {"knowledge_permission_report", "project_table_permission_report", "permission_report"}:
                scope = {
                    "knowledge_permission_report": "knowledge",
                    "project_table_permission_report": "project_tables",
                    "permission_report": "all",
                }[verdict.verdict]
                round_deltas = [render_permission_report(config["permission_facts"], scope=scope)]
            state["response_verified"] = True
            append_event(state, "response.verified", {
                "verdict": verdict.verdict, "claim_count": len(verdict.claims),
                "source_ids": sorted({e["source_id"] for c in verdict.claims for e in c["evidence"]}),
            })
            for delta in round_deltas:
                state["output_content"] = f"{state.get('output_content', '')}{delta}"
                append_event(state, RunEventType.MESSAGE_DELTA, {"delta": delta, "content": state["output_content"]})
        elif suppress_ungrounded_text:
            _handle_missing_grounding(state)
        elif issue := _runtime_truthfulness_issue(state, round_text):
            _handle_unverified_response(state, issue)
        else:
            for delta in round_deltas:
                state["output_content"] = f"{state.get('output_content', '')}{delta}"
                append_event(state, RunEventType.MESSAGE_DELTA, {"delta": delta, "content": state["output_content"]})
        return state

    async def route_tool(self, state: AgentState) -> AgentState:
        if is_terminal(state):
            return state
        if state.get("cancel_requested"):
            return cancel_run(state)
        if _deadline_exceeded(state):
            return fail_run(state, code=ErrorCode.MODEL_TIMEOUT, message="运行超过最大执行时间", stage="route_tool", retryable=True)
        pending = state.get("pending_tool_call")
        if not pending:
            queued = state.setdefault("tool_calls", [])
            pending = queued.pop(0) if queued else None
            state["pending_tool_call"] = pending
        if not pending:
            return state

        current_round = int(state.get("tool_round") or 0)
        max_rounds = int(state.get("max_tool_rounds") or 8)
        if current_round >= max_rounds:
            return fail_run(state, code=ErrorCode.TOOL_DENIED, message="工具调用轮数超过限制", stage="route_tool", retryable=False)

        model_call_id = str(pending.get("id") or pending.get("model_call_id") or f"call_{uuid4().hex}")
        name = str(pending.get("name") or "")
        context = ToolContext(
            run_id=str(state.get("run_id") or ""),
            tool_call_id=tool_call_id(),
            company_id=state["request"].company_id,
            agent_id=state["request"].agent_id,
            user_id=state["request"].user_id,
            permissions=frozenset(state.get("permissions") or frozenset()),
        )
        try:
            arguments = pending.get("arguments") if isinstance(pending.get("arguments"), dict) else _load_arguments(pending.get("arguments"))
            dedupe_key = _tool_dedupe_key(model_call_id, name, arguments)
            completed = state.setdefault("completed_tool_call_keys", set())
            if dedupe_key in completed:
                previous = next(
                    (
                        result
                        for result in reversed(state.get("tool_results") or [])
                        if str(result.get("name") or "") == name
                    ),
                    None,
                )
                if previous is not None:
                    replay = {**previous, "model_call_id": model_call_id}
                    _append_pending_turn_result(state, replay)
                state["pending_tool_call"] = None
                return state
            definition, request = self.tool_router.route(name=name, arguments=arguments, context=context)
        except (ToolDeniedError, ToolValidationError) as exc:
            code = getattr(exc, "code", ErrorCode.TOOL_INVALID_ARGUMENTS)
            state["tool_round"] = current_round + 1
            _append_tool_failure_result(
                state,
                pending={**pending, "id": model_call_id, "name": name},
                code=str(code),
                message=getattr(exc, "safe_message", "工具不可用"),
                execution_status="not_dispatched", business_outcome="not_attempted",
            )
            return state

        state["pending_tool_call"] = {
            **pending,
            "id": model_call_id,
            "name": name,
            "arguments": arguments,
            "dedupe_key": dedupe_key,
            "definition": definition,
            "request": request,
            "context": context,
        }
        state["status"] = "WAITING_TOOL"
        state["tool_round"] = current_round + 1
        await _call_recorder(self.tool_recorder.started,
            tool_call_id=context.tool_call_id,
            model_call_id=model_call_id,
            tool_name=name,
            executor=definition.executor,
            arguments=arguments,
        )
        append_event(state, RunEventType.TOOL_STARTED, {
            "tool_call_id": context.tool_call_id,
            "model_call_id": model_call_id,
            "tool_name": name,
            "executor": definition.executor,
        })
        return state

    async def execute_tool(self, state: AgentState) -> AgentState:
        if is_terminal(state):
            return state
        if state.get("cancel_requested"):
            return cancel_run(state)
        if _deadline_exceeded(state):
            return fail_run(state, code=ErrorCode.MODEL_TIMEOUT, message="运行超过最大执行时间", stage="execute_tool", retryable=True)
        pending = state.get("pending_tool_call")
        if not pending:
            return state

        definition = pending["definition"]
        request = pending["request"]
        context = pending["context"]
        if request.name in {"company.table.write", "company.table.clear_cells"}:
            context = replace(context, expected_table_version=_table_read_version(
                state.get("tool_results") or [], str(request.arguments.get("tid") or "").strip()))
        started = time.monotonic()
        try:
            if definition.executor == "backend":
                result = await asyncio.wait_for(self.backend_executor.execute(definition, request, context), timeout=request.timeout_seconds)
            elif definition.executor == "sandbox":
                if self.sandbox_executor is None:
                    raise ToolExecutionError(code=str(ErrorCode.SANDBOX_CREATE_FAILED), message="沙箱执行器不可用", retryable=True)
                result = await asyncio.wait_for(self.sandbox_executor.execute(definition, request, context), timeout=request.timeout_seconds)
            else:
                raise ToolExecutionError(code=str(ErrorCode.TOOL_DENIED), message="工具执行器不可用", retryable=False)
        except (asyncio.TimeoutError, TimeoutError):
            code = (ErrorCode.OPERATION_OUTCOME_UNKNOWN if request.name in {
                "company.table.write", "company.table.clear_cells"} else ErrorCode.SANDBOX_TIMEOUT)
            duration_ms = int((time.monotonic() - started) * 1000)
            await _call_recorder(self.tool_recorder.failed, tool_call_id=context.tool_call_id, error_code=str(code), duration_ms=duration_ms)
            append_event(state, RunEventType.TOOL_FAILED, {
                "tool_call_id": context.tool_call_id,
                "tool_name": pending.get("name"),
                "code": str(code),
                "message": "工具执行超时",
            })
            _append_tool_failure_result(state, pending=pending, code=str(code), message="工具执行超时")
            return state
        except ToolExecutionError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            await _call_recorder(self.tool_recorder.failed, tool_call_id=context.tool_call_id, error_code=exc.code, duration_ms=duration_ms)
            append_event(state, RunEventType.TOOL_FAILED, {
                "tool_call_id": context.tool_call_id,
                "tool_name": pending.get("name"),
                "code": exc.code,
                "message": exc.safe_message,
            })
            _append_tool_failure_result(state, pending=pending, code=exc.code, message=exc.safe_message,
                retryable=exc.retryable, execution_status=exc.execution_status, business_outcome=exc.business_outcome)
            return state

        duration_ms = int((time.monotonic() - started) * 1000)
        completed = state.setdefault("completed_tool_call_keys", set())
        outcome_unknown = str(result.error_code or "") == str(ErrorCode.OPERATION_OUTCOME_UNKNOWN)
        if not outcome_unknown:
            completed.add(str(pending.get("dedupe_key")))
        tool_result = {
            "tool_call_id": context.tool_call_id,
            "model_call_id": str(pending.get("id") or ""),
            "name": str(pending.get("name") or ""),
            "content": result.content,
            "arguments": dict(pending.get("arguments") or {}),
            "error_code": result.error_code,
            "success": result.success and not outcome_unknown,
            "artifact_refs": list(result.artifact_refs),
            "retryable": result.retryable and not outcome_unknown,
            "execution_status": "unconfirmed" if outcome_unknown else result.execution_status,
            "business_outcome": "unknown" if outcome_unknown else result.business_outcome,
            "effect_class": getattr(definition, "effect_class", "unknown"),
        }
        metadata = {k: tool_result[k] for k in ("execution_status", "business_outcome", "effect_class")}
        try:
            payload = json.loads(tool_result["content"])
        except (TypeError, ValueError):
            payload = tool_result["content"]
        payload = dict(payload) if isinstance(payload, dict) else {"result": payload}
        payload["runtime_outcome"] = metadata
        if not tool_result["success"]:
            recovery = recovery_policy(str(result.error_code or ErrorCode.INTERNAL_ERROR))
            payload["runtime_recovery"] = recovery
            tool_result["recovery_action"] = recovery["action"]
        if (not tool_result["success"] or result.execution_status != "returned"
                or result.business_outcome != "unknown" or metadata["effect_class"] != "unknown"):
            tool_result["content"] = json.dumps(payload, ensure_ascii=False)
        if tool_result["success"]:
            await _call_recorder(self.tool_recorder.completed, tool_call_id=context.tool_call_id, duration_ms=duration_ms)
        else:
            await _call_recorder(
                self.tool_recorder.failed,
                tool_call_id=context.tool_call_id,
                error_code=str(result.error_code or ErrorCode.INTERNAL_ERROR),
                duration_ms=duration_ms,
            )
        state["tool_results"] = [*state.get("tool_results", []), tool_result]
        evidence = workspace_read_evidence(
            tool_result["name"],
            tool_result["content"],
            _grounding_policy(state).get("target_terms") or (),
        ) if tool_result["success"] else None
        if evidence is not None:
            state["grounding_evidence"] = [*state.get("grounding_evidence", []), evidence]
            append_event(state, "grounding.evidence", evidence)
        elif tool_result["success"] and tool_result["name"] == "workspace.read" and _grounding_required(state):
            append_event(state, "grounding.evidence_rejected", {
                "reason": "workspace_read_not_relevant_or_extractable",
            })
        _append_pending_turn_result(state, tool_result)
        state["pending_tool_call"] = None
        state["status"] = "RUNNING"
        append_event(state, RunEventType.TOOL_COMPLETED if tool_result["success"] else RunEventType.TOOL_FAILED, {
            "tool_call_id": context.tool_call_id,
            "model_call_id": tool_result["model_call_id"],
            "tool_name": tool_result["name"],
            "success": tool_result["success"],
            "artifact_refs": list(result.artifact_refs),
            "duration_ms": duration_ms,
            **metadata,
        })
        if outcome_unknown:
            return fail_run(state, code=ErrorCode.OPERATION_OUTCOME_UNKNOWN,
                message="写入结果未确认，可能已生效；请先核验状态，不要自动重试。",
                stage="reconcile_tool", retryable=False)
        return state

    async def enqueue_memory_commit(self, state: AgentState) -> AgentState:
        if (
            state.get("status") != "COMPLETED"
            or state.get("grounding_blocked")
            or self.memory_gateway is None
        ):
            state["enqueue_memory_commit"] = False
            return state

        request = state["request"]
        output = str(state.get("output_content") or "").strip()
        if not output:
            state["enqueue_memory_commit"] = False
            return state

        user_content = ""
        for message in reversed(state.get("model_messages") or []):
            if message.get("role") == "user":
                user_content = str(message.get("content") or "").strip()
                break
        assessment = assess_memory_candidate(
            user_content,
            output,
            has_successful_tool=has_meaningful_successful_tool_result(
                state.get("tool_results")
            ),
        )
        if not assessment.eligible:
            state["enqueue_memory_commit"] = False
            append_event(state, "memory.commit_skipped", {
                "reason": assessment.reason,
            })
            return state

        content = f"User: {user_content}\nAssistant: {output}" if user_content else output
        try:
            await self.memory_gateway.commit(CommitRequest(
                run_id=str(state.get("run_id") or ""),
                company_id=request.company_id,
                agent_id=request.agent_id,
                user_id=request.user_id,
                session_id=request.session_id,
                source_message_ids=(request.user_message_id,),
                content=content,
                idempotency_key=f"agent-run:{state.get('run_id')}:memory",
                messages=tuple(
                    message for message in (
                        {"role": "user", "content": user_content} if user_content else None,
                        {"role": "assistant", "content": output},
                    )
                    if message is not None
                ),
                memory_types=tuple(sorted(assessment.memory_types)),
            ))
        except Exception as exc:
            state["enqueue_memory_commit"] = False
            append_event(state, "memory.commit_failed", {
                "error_type": exc.__class__.__name__,
            })
            return state

        state["enqueue_memory_commit"] = True
        append_event(state, "memory.commit_enqueued", {"enqueued": True})
        return state


async def _call_recorder(method, **kwargs) -> None:
    result = method(**kwargs)
    if inspect.isawaitable(result):
        await result


def _table_read_version(results: list[dict[str, Any]], tid: str) -> int | None:
    """Use only this run's latest matching receipt, never model-supplied versions."""
    for result in reversed(results):
        name = result.get("name")
        if name not in {"company.table.read", "company.table.write", "company.table.clear_cells"}:
            continue
        if str((result.get("arguments") or {}).get("tid") or "").strip() != tid:
            continue
        if name != "company.table.read" or not result.get("success"):
            return None
        try:
            payload = json.loads(result["content"])
            if payload.get("tid") != tid or payload.get("tool_name") != name:
                return None
            version = payload["data"]["table_version"]
            return version if type(version) is int and version >= 0 else None
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
    return None


def _grounding_policy(state: AgentState) -> dict[str, Any]:
    return dict((state.get("config") or {}).get("internal_grounding") or {})


def _grounding_required(state: AgentState) -> bool:
    return bool(_grounding_policy(state).get("required"))


def _has_grounding_evidence(state: AgentState) -> bool:
    policy = _grounding_policy(state)
    return bool(
        state.get("grounding_evidence")
        or policy.get("attachment_sources")
    )


def _add_system_instruction(state: AgentState, instruction: str) -> None:
    messages = list(state.get("model_messages") or [])
    if messages and messages[0].get("role") == "system":
        messages[0] = {
            **messages[0],
            "content": f"{messages[0].get('content') or ''}\n\n{instruction}",
        }
    else:
        messages.insert(0, {"role": "system", "content": instruction})
    state["model_messages"] = messages


def _handle_unverified_response(state: AgentState, issue: str) -> None:
    retry_count = max(0, int(state.get("response_retry_count") or 0))
    if retry_count >= MAX_RESPONSE_CORRECTIONS:
        fail_run(
            state,
            code=ErrorCode.INTERNAL_ERROR,
            message="模型多次未能依据可核验事实完成回答，请重试。",
            stage="verify_response",
            retryable=True,
        )
        return
    state["response_retry_count"] = retry_count + 1
    state["response_retry_pending"] = True
    if retry_count == 0:
        _add_system_instruction(state, RESPONSE_CORRECTION_INSTRUCTION)
    append_event(state, "response.retry_required", {
        "retry": retry_count + 1,
        "reason": issue,
    })


def _handle_missing_grounding(state: AgentState) -> None:
    retry_count = max(0, int(state.get("grounding_retry_count") or 0))
    can_read_workspace = "tool.workspace.read" in frozenset(
        state.get("permissions") or frozenset()
    )
    state["output_content"] = ""
    if can_read_workspace and retry_count < 1:
        state["grounding_retry_count"] = retry_count + 1
        state["grounding_retry_pending"] = True
        _add_system_instruction(state, GROUNDING_RETRY_INSTRUCTION)
        append_event(state, "grounding.retry_required", {
            "retry": retry_count + 1,
            "reason": "missing_current_run_evidence",
        })
        return

    state["grounding_blocked"] = True
    state["output_content"] = GROUNDING_REFUSAL
    append_event(state, "grounding.blocked", {
        "reason": "missing_current_run_evidence",
        "retry_count": retry_count,
    })
    append_event(state, RunEventType.MESSAGE_DELTA, {
        "delta": GROUNDING_REFUSAL,
        "content": GROUNDING_REFUSAL,
    })


def should_continue_after_model(state: AgentState) -> str:
    if is_terminal(state):
        return "finalize"
    if state.get("grounding_retry_pending") or state.get("response_retry_pending"):
        return "call_model"
    return "route_tool" if state.get("tool_calls") or state.get("pending_tool_call") else "finalize"


def should_continue_after_classification(state: AgentState) -> str:
    return "finalize" if state.get("response_verified") else "recall_memory"


def should_continue_after_tool(state: AgentState) -> str:
    if is_terminal(state):
        return "finalize"
    return "route_tool" if state.get("tool_calls") else "call_model"


def _add_usage(state: AgentState, usage: dict[str, int] | None) -> None:
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        state.setdefault("usage", {})[key] = (
            int(state.get("usage", {}).get(key) or 0)
            + int((usage or {}).get(key) or 0)
        )


def _record_model_metrics(state: AgentState, metrics) -> None:
    for item in metrics or ():
        append_event(state, "model.call.completed", {
            key: value
            for key, value in dict(item or {}).items()
            if key in {
                "purpose", "duration_ms", "first_output_ms", "attempts",
                "retry_count", "input_tokens", "output_tokens", "total_tokens",
            }
        })


def _load_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw or "{}"))
    except ValueError as exc:
        raise ToolValidationError("工具参数不是合法 JSON") from exc
    if not isinstance(parsed, dict):
        raise ToolValidationError("工具参数必须是对象")
    return parsed


def _normalize_tool_call(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(data.get("id") or data.get("model_call_id") or f"call_{uuid4().hex}"),
        "name": str(data.get("name") or ""),
        "arguments": data.get("arguments") or {},
    }


def _tool_dedupe_key(model_call_id: str, name: str, arguments: dict[str, Any]) -> str:
    encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{model_call_id}:{name}:{encoded}"


def _model_tool_call(call: dict[str, Any]) -> dict[str, Any]:
    arguments = call.get("arguments")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments or {}, ensure_ascii=False, separators=(",", ":"))
    return {
        "id": str(call.get("id") or ""),
        "type": "function",
        "function": {
            "name": str(call.get("name") or ""),
            "arguments": arguments,
        },
    }


def _append_pending_turn_result(state: AgentState, result: dict[str, Any]) -> None:
    turn = state.get("pending_tool_turn")
    if turn is not None:
        turn.setdefault("results", []).append(result)


def _append_tool_failure_result(
    state: AgentState,
    *,
    pending: dict[str, Any],
    code: str,
    message: str,
    retryable: bool = False,
    execution_status: str = "unconfirmed",
    business_outcome: str = "unknown",
) -> None:
    recovery = recovery_policy(str(code))
    metadata = {"execution_status": execution_status, "business_outcome": business_outcome,
        "effect_class": getattr(pending.get("definition"), "effect_class", "unknown")}
    if str(code) == str(ErrorCode.OPERATION_OUTCOME_UNKNOWN):
        metadata.update(execution_status="unconfirmed", business_outcome="unknown")
    result = {
        **metadata,
        "tool_call_id": getattr(pending.get("context"), "tool_call_id", ""),
        "model_call_id": str(pending.get("id") or ""),
        "name": str(pending.get("name") or ""),
        "arguments": pending.get("arguments") or {},
        "content": json.dumps({
            "success": False,
            "error": str(message or "工具不可用"),
            "instruction": recovery["instruction"],
            "runtime_recovery": recovery,
            "runtime_outcome": metadata,
        }, ensure_ascii=False),
        "success": False,
        "artifact_refs": [],
        "error_code": str(code),
        "recovery_action": recovery["action"],
        "retryable": retryable and str(code) != str(ErrorCode.OPERATION_OUTCOME_UNKNOWN),
    }
    state["tool_results"] = [*state.get("tool_results", []), result]
    _append_pending_turn_result(state, result)
    state["pending_tool_call"] = None
    state["status"] = "RUNNING"
    append_event(state, RunEventType.TOOL_FAILED, {
        "model_call_id": result["model_call_id"],
        "tool_name": result["name"],
        "code": str(code),
        "message": str(message or "工具不可用"),
        **metadata,
    })
    if str(code) == str(ErrorCode.OPERATION_OUTCOME_UNKNOWN):
        fail_run(state, code=ErrorCode.OPERATION_OUTCOME_UNKNOWN,
            message="写入结果未确认，可能已生效；请先核验状态，不要自动重试。",
            stage="reconcile_tool", retryable=False)
