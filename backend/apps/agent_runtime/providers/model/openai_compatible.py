"""OpenAI-compatible streaming implementation of the frozen ModelGateway."""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from typing import Any, Callable

from asgiref.sync import sync_to_async
from django.conf import settings

from apps.agent_runtime.contracts import ModelEvent, ModelRequest
from apps.agent_runtime.enums import ErrorCode, ModelEventType

from .credentials import (
    DjangoModelCredentialLoader,
    ResolvedCredential,
    ResolvedModelSelection,
)
from .errors import ModelConfigurationError, normalize_model_error
from .text_tool_calls import MiniMaxTextToolCallParser, TextToolCallParseError


@dataclass
class _ToolCallBuffer:
    index: int
    call_id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass(frozen=True)
class ProviderHealth:
    provider: str
    model: str
    configured: bool
    healthy: bool
    latency_ms: int
    error_code: str | None = None


class OpenAICompatibleModelGateway:
    """Normalize OpenAI-compatible chunks into provider-independent events.

    The SDK's own retry feature is disabled.  This class retries only before an
    observable event is emitted, which enforces the no-engine-switch rule after
    the first token or tool-call fragment.
    """

    def __init__(
        self,
        *,
        credential_loader=None,
        client_factory: Callable[[ResolvedCredential, ModelRequest], Any] | None = None,
        max_retries: int | None = None,
        retry_base_delay: float | None = None,
    ) -> None:
        self.credential_loader = credential_loader or DjangoModelCredentialLoader()
        self.client_factory = client_factory or self._default_client_factory
        self.max_retries = max(
            0,
            int(
                getattr(settings, "AGENT_RUNTIME_MODEL_MAX_RETRIES", 1)
                if max_retries is None
                else max_retries
            ),
        )
        self.retry_base_delay = max(
            0.0,
            float(
                getattr(settings, "AGENT_RUNTIME_MODEL_RETRY_BASE_DELAY_SECONDS", 0.2)
                if retry_base_delay is None
                else retry_base_delay
            ),
        )

    async def stream(self, request: ModelRequest):
        emitted = False
        started = time.monotonic()
        first_output_at = None
        usage: dict[str, int] = {}
        for attempt in range(self.max_retries + 1):
            try:
                credential = await sync_to_async(
                    self.credential_loader.load,
                    thread_sensitive=True,
                )(request.credential_ref, request.provider)
                async for event in self._stream_once(request, credential):
                    emitted = True
                    event_type = str(event.event_type)
                    if first_output_at is None and event_type in {
                        str(ModelEventType.TEXT_DELTA),
                        str(ModelEventType.TOOL_CALL_DELTA),
                        str(ModelEventType.TOOL_CALL_COMPLETED),
                    }:
                        first_output_at = time.monotonic()
                    if event_type == str(ModelEventType.USAGE):
                        usage = {
                            key: int(event.data.get(key) or 0)
                            for key in ("input_tokens", "output_tokens", "total_tokens")
                        }
                    if event_type in {str(ModelEventType.FINISH), str(ModelEventType.ERROR)}:
                        now = time.monotonic()
                        yield ModelEvent(str(ModelEventType.CALL_METRICS), {
                            "purpose": request.purpose,
                            "duration_ms": int((now - started) * 1000),
                            "first_output_ms": (
                                int((first_output_at - started) * 1000)
                                if first_output_at is not None else None
                            ),
                            "attempts": attempt + 1,
                            "retry_count": attempt,
                            **usage,
                        })
                    yield event
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                normalized = normalize_model_error(exc)
                if normalized.retryable and not emitted and attempt < self.max_retries:
                    await asyncio.sleep(self.retry_base_delay * (2**attempt))
                    continue
                now = time.monotonic()
                yield ModelEvent(str(ModelEventType.CALL_METRICS), {
                    "purpose": request.purpose,
                    "duration_ms": int((now - started) * 1000),
                    "first_output_ms": None,
                    "attempts": attempt + 1,
                    "retry_count": attempt,
                    **usage,
                })
                yield ModelEvent(
                    str(ModelEventType.ERROR),
                    {
                        "code": normalized.code,
                        "message": normalized.message,
                        "retryable": normalized.retryable,
                    },
                )
                return

    async def check_health(self, selection: ResolvedModelSelection) -> ProviderHealth:
        """Perform a lightweight authenticated provider probe."""

        loop = asyncio.get_running_loop()
        started = loop.time()
        client = None
        try:
            credential = await sync_to_async(
                self.credential_loader.load,
                thread_sensitive=True,
            )(selection.credential_ref, selection.provider)
            request = ModelRequest(
                run_id="provider-health",
                provider=selection.provider,
                model=selection.model,
                messages=[],
                tools=[],
                temperature=None,
                max_output_tokens=None,
                timeout_seconds=min(selection.timeout_seconds, 10),
                credential_ref=selection.credential_ref,
            )
            client = self.client_factory(credential, request)
            await client.models.list()
            return ProviderHealth(
                selection.provider,
                selection.model,
                True,
                True,
                int((loop.time() - started) * 1000),
            )
        except ModelConfigurationError as exc:
            return ProviderHealth(
                selection.provider,
                selection.model,
                False,
                False,
                int((loop.time() - started) * 1000),
                exc.code,
            )
        except Exception as exc:
            normalized = normalize_model_error(exc)
            return ProviderHealth(
                selection.provider,
                selection.model,
                True,
                False,
                int((loop.time() - started) * 1000),
                normalized.code,
            )
        finally:
            await self._close(client)

    async def _stream_once(
        self,
        request: ModelRequest,
        credential: ResolvedCredential,
    ):
        client = self.client_factory(credential, request)
        upstream = None
        tool_calls: dict[int, _ToolCallBuffer] = {}
        text_tool_calls: list[_ToolCallBuffer] = []
        text_parser = (
            MiniMaxTextToolCallParser()
            if str(request.provider or "").strip().lower() == "minimax"
            else None
        )
        usage: dict[str, int] | None = None
        finish_reason: str | None = None
        try:
            upstream = await client.chat.completions.create(**self._request_payload(request))
            async for chunk in upstream:
                chunk_usage = _value(chunk, "usage")
                if chunk_usage is not None:
                    usage = {
                        "input_tokens": int(_value(chunk_usage, "prompt_tokens") or 0),
                        "output_tokens": int(_value(chunk_usage, "completion_tokens") or 0),
                        "total_tokens": int(_value(chunk_usage, "total_tokens") or 0),
                    }
                for choice in list(_value(chunk, "choices") or []):
                    delta = _value(choice, "delta")
                    text = _value(delta, "content") if delta is not None else None
                    if text:
                        if text_parser is None:
                            yield ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": str(text)})
                        else:
                            text_parts, parsed_calls = text_parser.feed(str(text))
                            for text_part in text_parts:
                                if text_part:
                                    yield ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": text_part})
                            for parsed_call in parsed_calls:
                                text_tool_calls.append(_ToolCallBuffer(
                                    index=len(tool_calls) + len(text_tool_calls),
                                    call_id=parsed_call.call_id,
                                    name=parsed_call.name,
                                    arguments=_dump_arguments(parsed_call.arguments),
                                ))
                    for fragment in list(_value(delta, "tool_calls") or []):
                        event = self._tool_call_delta(tool_calls, fragment)
                        yield ModelEvent(str(ModelEventType.TOOL_CALL_DELTA), event)
                    reason = _value(choice, "finish_reason")
                    if reason:
                        finish_reason = str(reason)

            if text_parser is not None:
                for text_part in text_parser.finish():
                    if text_part:
                        yield ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": text_part})

            completed_calls = [tool_calls[index] for index in sorted(tool_calls)]
            completed_calls.extend(text_tool_calls)
            for item in completed_calls:
                yield ModelEvent(
                    str(ModelEventType.TOOL_CALL_COMPLETED),
                    {
                        "index": item.index,
                        "id": item.call_id,
                        "name": item.name,
                        "arguments": item.arguments,
                    },
                )
            if usage is not None:
                yield ModelEvent(str(ModelEventType.USAGE), usage)
            yield ModelEvent(
                str(ModelEventType.FINISH),
                {"reason": finish_reason or "stop"},
            )
        except TextToolCallParseError as exc:
            yield ModelEvent(
                str(ModelEventType.ERROR),
                {
                    "code": str(ErrorCode.INTERNAL_ERROR),
                    "message": str(exc),
                    "retryable": False,
                },
            )
        finally:
            await self._close(upstream)
            await self._close(client)

    @staticmethod
    def _tool_call_delta(buffers: dict[int, _ToolCallBuffer], fragment: Any) -> dict:
        index = int(_value(fragment, "index") or 0)
        item = buffers.setdefault(index, _ToolCallBuffer(index=index))
        call_id = str(_value(fragment, "id") or "")
        function = _value(fragment, "function")
        name = str(_value(function, "name") or "")
        arguments = str(_value(function, "arguments") or "")
        if call_id:
            item.call_id = call_id
        if name:
            item.name += name
        if arguments:
            item.arguments += arguments
        return {
            "index": index,
            "id": call_id,
            "name": name,
            "arguments_delta": arguments,
        }

    @staticmethod
    def _request_payload(request: ModelRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.response_format is not None:
            payload["response_format"] = request.response_format
        if request.tools:
            payload["tools"] = request.tools
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        platform_provider = str(getattr(settings, "AGENT_RUNTIME_PLATFORM_PROVIDER_ID", "custom-qwen"))
        if request.provider == platform_provider and bool(
            getattr(settings, "AGENT_RUNTIME_PLATFORM_DISABLE_THINKING", True)
        ):
            payload["extra_body"] = {
                "chat_template_kwargs": {"enable_thinking": False},
            }
        return payload

    @staticmethod
    def _default_client_factory(credential: ResolvedCredential, request: ModelRequest):
        from openai import AsyncOpenAI

        kwargs = {
            "api_key": credential.api_key,
            "timeout": request.timeout_seconds,
            "max_retries": 0,
        }
        if credential.base_url:
            kwargs["base_url"] = credential.base_url
        return AsyncOpenAI(**kwargs)

    @staticmethod
    async def _close(resource: Any) -> None:
        if resource is None:
            return
        close = getattr(resource, "aclose", None) or getattr(resource, "close", None)
        if not callable(close):
            return
        result = close()
        if inspect.isawaitable(result):
            await result


def _value(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _dump_arguments(arguments: dict[str, Any]) -> str:
    import json

    return json.dumps(arguments or {}, ensure_ascii=False, separators=(",", ":"))
