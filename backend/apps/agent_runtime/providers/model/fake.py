"""Deterministic model gateway used by Phase 0 contract tests."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from apps.agent_runtime.contracts import ModelEvent, ModelRequest
from apps.agent_runtime.enums import ModelEventType


class FakeModelGateway:
    def __init__(
        self,
        events: Iterable[ModelEvent] | None = None,
        *,
        chunk_text: str = "你好，我是统一运行时。",
    ) -> None:
        self._events = list(events) if events is not None else [
            ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": chunk_text[:2]}),
            ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": chunk_text[2:]}),
            ModelEvent(str(ModelEventType.USAGE), {"input_tokens": 8, "output_tokens": 6, "total_tokens": 14}),
            ModelEvent(str(ModelEventType.FINISH), {"reason": "stop"}),
        ]

    async def stream(self, request: ModelRequest):
        for event in self._events:
            await asyncio.sleep(0)
            yield event
