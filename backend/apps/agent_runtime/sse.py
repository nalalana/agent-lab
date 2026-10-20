"""Adapters from internal RunEvent objects to the existing browser SSE contract."""

from __future__ import annotations

import asyncio
import queue
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, AsyncIterator

from apps.agent_runtime.contracts import RunEvent
from apps.agent_runtime.enums import ErrorCode, RunEventType


SAFE_ERROR_MESSAGES = {
    ErrorCode.AUTH_DENIED: "权限不足",
    ErrorCode.RISK_REJECTED: "内容未通过安全检查",
    ErrorCode.AGENT_DISABLED: "Agent 已停用",
    ErrorCode.MODEL_TIMEOUT: "模型响应超时，请稍后重试",
    ErrorCode.MODEL_AUTH_FAILED: "模型配置不可用",
    ErrorCode.MODEL_RATE_LIMITED: "模型服务繁忙，请稍后重试",
    ErrorCode.MEMORY_UNAVAILABLE: "长期记忆暂不可用，已降级回答",
    ErrorCode.TOOL_DENIED: "工具权限不足",
    ErrorCode.TOOL_INVALID_ARGUMENTS: "工具参数无效",
    ErrorCode.SANDBOX_CREATE_FAILED: "沙箱创建失败",
    ErrorCode.SANDBOX_TIMEOUT: "沙箱执行超时",
    ErrorCode.RUN_CANCELLED: "请求已取消",
    ErrorCode.INTERNAL_ERROR: "操作未完成，请稍后重试",
}


STAGE_LABELS = {
    "resolve_config": "Agent 配置读取",
    "classify_request": "请求意图分析",
    "recall_memory": "上下文准备",
    "recall_stub": "上下文准备",
    "call_model": "回答生成与工具选择",
    "route_tool": "工具路由与权限检查",
    "execute_tool": "工具执行",
    "verify_response": "回答事实核验",
    "finalize": "结果整理与保存",
    "create": "运行创建",
    "connect": "运行连接",
    "guard": "请求检查",
    "status": "运行状态读取",
    "terminate": "运行终止",
}
INTERNAL_STAGE_DETAILS = {
    "resolve_config": "Agent 配置读取未完成。",
    "classify_request": "请求意图分析未完成，尚未进入后续处理。",
    "recall_memory": "上下文准备未完成。",
    "recall_stub": "上下文准备未完成。",
    "call_model": "Agent 未能完成回答生成或工具选择。",
    "route_tool": "工具路由或权限检查未能完成。",
    "execute_tool": "工具执行未能完成。",
    "verify_response": "未能核实回答依据，无法确认本次请求的处理结果。请先检查相关内容后再重试。",
    "finalize": "结果整理或保存未完成。",
    "create": "运行任务创建未完成。",
    "connect": "运行连接未完成。",
    "guard": "请求前置检查未完成。",
    "status": "运行状态读取未完成。",
    "terminate": "运行终止步骤未完成。",
}


def safe_error_message(
    code: str | ErrorCode | None,
    fallback: str = "操作未完成，请稍后重试",
    stage: str | None = None,
) -> str:
    try:
        error_code = ErrorCode(str(code))
        message = SAFE_ERROR_MESSAGES[error_code]
    except (ValueError, KeyError, TypeError):
        error_code = None
        message = fallback

    stage_key = str(stage or "").strip()
    label = STAGE_LABELS.get(stage_key)
    if not label or error_code == ErrorCode.RUN_CANCELLED:
        return message
    if error_code == ErrorCode.INTERNAL_ERROR:
        detail = INTERNAL_STAGE_DETAILS.get(stage_key, "系统未能完成该步骤。")
    else:
        detail = message
    return f"{label}步骤遇到问题：{detail}"


def run_event_to_legacy_sse(
    event: RunEvent,
    *,
    accumulated_content: str = "",
) -> dict[str, Any] | None:
    event_type = str(event.event_type)
    data = dict(event.data or {})

    if event_type == str(RunEventType.RUN_STARTED):
        return {
            "event": "start",
            "data": {
                key: value
                for key, value in data.items()
                if key in {"conversation", "conversation_id", "session_key", "agent", "user_message"}
            },
        }
    if event_type == str(RunEventType.WARNING):
        message = str(data.get("message") or "").strip()
        return {"event": "status", "data": {"message": message}} if message else None
    if event_type == str(RunEventType.PROGRESS):
        message = str(data.get("message") or "").strip()
        return {"event": "status", "data": {"message": message}} if message else None
    if event_type == str(RunEventType.MESSAGE_DELTA):
        delta = str(data.get("delta") or "")
        content = str(data.get("content") or f"{accumulated_content}{delta}")
        return {"event": "delta", "data": {"delta": delta, "content": content}} if delta else None
    if event_type == str(RunEventType.MESSAGE_COMPLETED):
        return {
            "event": "complete",
            "data": {
                key: value
                for key, value in data.items()
                if key
                in {
                    "content",
                    "token_count",
                    "conversation",
                    "conversation_id",
                    "session_key",
                    "agent",
                    "tool_calls",
                    "assistant_message",
                }
            },
        }
    if event_type in {str(RunEventType.RUN_FAILED), str(RunEventType.RUN_CANCELLED)}:
        code = data.get("code") or data.get("error_code")
        return {
            "event": "error",
            "data": {
                "message": safe_error_message(
                    code,
                    str(data.get("message") or ""),
                    stage=str(data.get("stage") or ""),
                ),
                "content": str(data.get("content") or accumulated_content or ""),
            },
        }
    return None


def run_events_to_legacy_sse(events: Iterable[RunEvent]) -> list[dict[str, Any]]:
    accumulated_content = ""
    output: list[dict[str, Any]] = []
    for event in events:
        item = run_event_to_legacy_sse(event, accumulated_content=accumulated_content)
        if item is None:
            continue
        if item["event"] == "delta":
            accumulated_content = str(item["data"].get("content") or accumulated_content)
        output.append(item)
    return output


def run_event_to_legacy_runtime(event: RunEvent) -> dict[str, Any] | None:
    """Return the flat stream shape consumed by the existing CompanyService.

    Views add the final SSE ``data`` envelope. Keeping this adapter flat lets
    OpenClaw and the unified runtime share persistence and browser compatibility
    behavior.
    """

    event_type = str(event.event_type)
    data = dict(event.data or {})
    base = {"run_id": event.run_id}
    if event_type == str(RunEventType.RUN_STARTED):
        return {"event": "start", **base}
    if event_type == str(RunEventType.WARNING):
        message = str(data.get("message") or "").strip()
        return {"event": "status", "message": message, **base} if message else None
    if event_type == str(RunEventType.PROGRESS):
        message = str(data.get("message") or "").strip()
        return {"event": "status", "message": message, **base} if message else None
    if event_type == str(RunEventType.MESSAGE_DELTA):
        delta = str(data.get("delta") or "")
        return {"event": "delta", "delta": delta, **base} if delta else None
    if event_type == str(RunEventType.MESSAGE_COMPLETED):
        return {
            "event": "complete",
            "content": str(data.get("content") or ""),
            "token_count": int(data.get("token_count") or 0),
            "tool_calls": list(data.get("tool_calls") or []),
            **base,
        }
    if event_type in {str(RunEventType.RUN_FAILED), str(RunEventType.RUN_CANCELLED)}:
        code = data.get("code") or data.get("error_code")
        return {
            "event": "error",
            "code": str(code or ErrorCode.INTERNAL_ERROR),
            "message": safe_error_message(
                code,
                stage=str(data.get("stage") or ""),
            ),
            "content": str(data.get("content") or ""),
            **base,
        }
    return None


@dataclass(frozen=True)
class _BridgeFailure:
    error: BaseException


class AsyncIteratorBridge:
    """Consume one async iterator from synchronous Django streaming code.

    A dedicated event-loop thread owns the async generator for its complete
    lifetime. The bounded queue applies backpressure, and ``close`` cancels the
    provider task when the browser disconnects.
    """

    _DONE = object()

    def __init__(self, source: AsyncIterator[Any], *, max_queue_size: int = 64) -> None:
        self.source = source
        self.items: queue.Queue = queue.Queue(maxsize=max(1, max_queue_size))
        self.stop_requested = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task | None = None
        self.thread = threading.Thread(
            target=self._run,
            name="agent-runtime-stream",
            daemon=True,
        )
        self.thread.start()

    def __iter__(self):
        return self

    def __next__(self):
        item = self.items.get()
        if item is self._DONE:
            self.close()
            raise StopIteration
        if isinstance(item, _BridgeFailure):
            self.close()
            raise item.error
        return item

    def close(self) -> None:
        self.stop_requested.set()
        if self.loop is not None and self.task is not None and not self.task.done():
            self.loop.call_soon_threadsafe(self.task.cancel)
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=1)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        self.task = loop.create_task(self._consume())
        try:
            loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            pass
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    async def _consume(self) -> None:
        try:
            async for item in self.source:
                if self.stop_requested.is_set() or not await self._put(item):
                    break
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self._put(_BridgeFailure(exc))
        finally:
            close = getattr(self.source, "aclose", None)
            if callable(close):
                await close()
            await self._put(self._DONE, force=True)

    async def _put(self, item: Any, *, force: bool = False) -> bool:
        while force or not self.stop_requested.is_set():
            try:
                self.items.put(item, timeout=0.05)
                return True
            except queue.Full:
                await asyncio.sleep(0)
                if force and self.stop_requested.is_set():
                    # A disconnected consumer will never drain the queue.
                    return False
        return False


def iter_legacy_runtime_events(
    source: AsyncIterator[RunEvent],
    *,
    max_queue_size: int = 64,
):
    bridge = AsyncIteratorBridge(source, max_queue_size=max_queue_size)
    try:
        for event in bridge:
            item = run_event_to_legacy_runtime(event)
            if item is not None:
                yield item
    finally:
        bridge.close()
