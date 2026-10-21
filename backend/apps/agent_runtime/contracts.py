"""Frozen Phase 0 contracts for the unified agent runtime."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator, Literal, Protocol


@dataclass(frozen=True)
class RunRequest:
    request_id: str
    company_id: object
    agent_id: object
    user_id: object | None
    session_id: object
    source: Literal["agent_test", "public_client", "cron"]
    user_message_id: object
    locale: str
    trace_id: str


@dataclass(frozen=True)
class RunEvent:
    schema_version: int
    event_id: str
    run_id: str
    sequence: int
    event_type: str
    occurred_at: datetime
    data: dict[str, Any]


class AgentRuntimeFacade(Protocol):
    async def stream(self, request: RunRequest) -> AsyncIterator[RunEvent]:
        ...


@dataclass(frozen=True)
class ModelRequest:
    run_id: str
    provider: str
    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    temperature: float | None
    max_output_tokens: int | None
    timeout_seconds: int
    credential_ref: str
    response_format: dict[str, Any] | None = None
    purpose: str = "generation"


@dataclass(frozen=True)
class ModelEvent:
    event_type: str
    data: dict[str, Any]


class ModelGateway(Protocol):
    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        ...


@dataclass(frozen=True)
class RecallRequest:
    run_id: str
    company_id: object
    agent_id: object
    user_id: object | None
    session_id: object
    query: str
    scopes: tuple[str, ...]
    top_k: int


@dataclass(frozen=True)
class MemoryItem:
    memory_id: str
    scope: str
    content: str
    score: float
    source_ref: str | None


@dataclass(frozen=True)
class CommitRequest:
    run_id: str
    company_id: object
    agent_id: object
    user_id: object | None
    session_id: object
    source_message_ids: tuple[object, ...]
    content: str
    idempotency_key: str
    messages: tuple[dict[str, str], ...] = ()
    memory_types: tuple[str, ...] = ()


class MemoryGateway(Protocol):
    async def recall(self, request: RecallRequest) -> list[MemoryItem]:
        ...

    async def commit(self, request: CommitRequest) -> None:
        ...


@dataclass(frozen=True)
class ToolContext:
    run_id: str
    tool_call_id: str
    company_id: object
    agent_id: object
    user_id: object | None
    permissions: frozenset[str]
    expected_table_version: int | None = None


@dataclass(frozen=True)
class ToolRequest:
    name: str
    arguments: dict[str, Any]
    timeout_seconds: int


@dataclass(frozen=True)
class ToolResult:
    success: bool
    content: str
    artifact_refs: tuple[str, ...]
    error_code: str | None
    retryable: bool


@dataclass(frozen=True)
class SandboxSpec:
    run_id: str
    company_id: object
    template: str
    cpu_cores: int
    memory_mib: int
    ttl_seconds: int
    network_policy: Literal["isolated", "restricted", "public"]
    labels: dict[str, str]


@dataclass(frozen=True)
class SandboxHandle:
    sandbox_id: str
    provider: str
    external_id: str


@dataclass(frozen=True)
class ExecutionRequest:
    command: tuple[str, ...]
    cwd: str
    environment: dict[str, str]
    timeout_seconds: int
    input_refs: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    artifact_refs: tuple[str, ...]
    duration_ms: int
    truncated: bool


class SandboxProvider(Protocol):
    async def create(self, spec: SandboxSpec) -> SandboxHandle:
        ...

    async def execute(self, sandbox_id: str, request: ExecutionRequest) -> ExecutionResult:
        ...

    async def terminate(self, sandbox_id: str) -> None:
        ...


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    message: str
    retryable: bool
    stage: str
    trace_id: str
