"""Fallback parser for providers that serialize tool calls into text.

Some OpenAI-compatible providers occasionally emit their native tool protocol
inside ``delta.content`` instead of populating ``delta.tool_calls``.  This
module recognizes that protocol without exposing the control markup as user
visible assistant text.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4


class TextToolCallParseError(ValueError):
    """Raised when a provider starts, but does not complete, a tool call."""


@dataclass(frozen=True)
class ParsedTextToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


class MiniMaxTextToolCallParser:
    """Incrementally remove and parse MiniMax native tool-call markup."""

    START = "<minimax:tool_call>"
    END = "</minimax:tool_call>"
    MAX_BLOCK_CHARS = 256 * 1024

    def __init__(self) -> None:
        self._buffer = ""
        self._inside = False

    def feed(self, value: str) -> tuple[list[str], list[ParsedTextToolCall]]:
        self._buffer += str(value or "")
        text_parts: list[str] = []
        calls: list[ParsedTextToolCall] = []

        while self._buffer:
            if self._inside:
                end_index = self._buffer.find(self.END)
                if end_index < 0:
                    if len(self._buffer) > self.MAX_BLOCK_CHARS:
                        raise TextToolCallParseError("工具调用内容超过长度限制")
                    break
                block = self._buffer[:end_index]
                self._buffer = self._buffer[end_index + len(self.END):]
                self._inside = False
                calls.extend(_parse_minimax_block(block))
                continue

            start_index = self._buffer.find(self.START)
            if start_index >= 0:
                if start_index:
                    text_parts.append(self._buffer[:start_index])
                self._buffer = self._buffer[start_index + len(self.START):]
                self._inside = True
                continue

            keep = _partial_suffix_length(self._buffer, self.START)
            emit_until = len(self._buffer) - keep
            if emit_until:
                text_parts.append(self._buffer[:emit_until])
                self._buffer = self._buffer[emit_until:]
            break

        return text_parts, calls

    def finish(self) -> list[str]:
        if self._inside:
            self._buffer = ""
            raise TextToolCallParseError("工具调用格式不完整")
        remaining = self._buffer
        self._buffer = ""
        return [remaining] if remaining else []


_INVOKE_RE = re.compile(
    r"<invoke\s+name\s*=\s*(['\"])(?P<name>[^'\"]+)\1\s*>(?P<body>[\s\S]*?)</invoke\s*>",
    re.IGNORECASE,
)
_SELF_CLOSING_INVOKE_RE = re.compile(
    r"<invoke\s+name\s*=\s*(['\"])(?P<name>[^'\"]+)\1\s*/>",
    re.IGNORECASE,
)
_PARAMETER_RE = re.compile(
    r"<parameter\s+name\s*=\s*(['\"])(?P<name>[^'\"]+)\1\s*>(?P<value>[\s\S]*?)</parameter\s*>",
    re.IGNORECASE,
)


def _parse_minimax_block(block: str) -> list[ParsedTextToolCall]:
    calls: list[ParsedTextToolCall] = []
    matches = list(_INVOKE_RE.finditer(block))
    matches.extend(_SELF_CLOSING_INVOKE_RE.finditer(block))
    matches.sort(key=lambda item: item.start())
    if not matches:
        raise TextToolCallParseError("工具调用缺少 invoke")

    for match in matches:
        name = html.unescape(str(match.group("name") or "")).strip()
        if not name:
            raise TextToolCallParseError("工具调用缺少名称")
        body = match.groupdict().get("body") or ""
        arguments = _parse_arguments(body)
        calls.append(ParsedTextToolCall(
            call_id=f"call_minimax_{uuid4().hex}",
            name=name,
            arguments=arguments,
        ))
    return calls


def _parse_arguments(body: str) -> dict[str, Any]:
    parameters = list(_PARAMETER_RE.finditer(body))
    if parameters:
        arguments: dict[str, Any] = {}
        for parameter in parameters:
            name = html.unescape(str(parameter.group("name") or "")).strip()
            if not name:
                raise TextToolCallParseError("工具参数缺少名称")
            value = html.unescape(str(parameter.group("value") or "")).strip()
            arguments[name] = _decode_parameter(value)
        return arguments

    candidate = html.unescape(str(body or "")).strip()
    if not candidate:
        return {}
    try:
        parsed = json.loads(candidate)
    except ValueError as exc:
        raise TextToolCallParseError("工具参数格式无效") from exc
    if not isinstance(parsed, dict):
        raise TextToolCallParseError("工具参数必须是对象")
    return parsed


def _decode_parameter(value: str) -> Any:
    if not value:
        return ""
    try:
        return json.loads(value)
    except ValueError:
        return value


def _partial_suffix_length(value: str, marker: str) -> int:
    maximum = min(len(value), len(marker) - 1)
    for size in range(maximum, 0, -1):
        if value.endswith(marker[:size]):
            return size
    return 0
