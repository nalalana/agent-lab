"""Tool execution adapters used by the Phase 2 graph."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from asgiref.sync import sync_to_async

from apps.agent_runtime.contracts import (
    ExecutionRequest,
    SandboxSpec,
    ToolContext,
    ToolRequest,
    ToolResult,
)
from apps.agent_runtime.enums import ErrorCode
from apps.agent_runtime.providers.sandbox.errors import SandboxProviderError, map_sandbox_error

from .definitions import ToolDefinition


class ToolExecutionError(RuntimeError):
    def __init__(self, *, code: str, message: str, retryable: bool = False) -> None:
        self.code = code
        self.safe_message = message
        self.retryable = retryable
        super().__init__(message)


@dataclass
class ToolAuditRecord:
    tool_call_id: str
    model_call_id: str
    tool_name: str
    executor: str
    status: str
    arguments_hash: str
    arguments_redacted: dict[str, Any]
    result_ref: str = ""
    duration_ms: int | None = None
    error_code: str = ""


@dataclass(frozen=True)
class SandboxInputFile:
    """One trusted backend payload to stage into a Run-scoped sandbox."""

    name: str
    content: bytes


@dataclass(frozen=True)
class SandboxOutputFile:
    """One validated sandbox output retrieved through the Run artifact store."""

    name: str
    content: bytes
    content_type: str
    artifact_ref: str


@dataclass
class InMemoryToolCallRecorder:
    records: list[ToolAuditRecord] = field(default_factory=list)

    def started(self, *, tool_call_id: str, model_call_id: str, tool_name: str, executor: str, arguments: dict[str, Any]) -> None:
        self.records.append(ToolAuditRecord(
            tool_call_id=tool_call_id,
            model_call_id=model_call_id,
            tool_name=tool_name,
            executor=executor,
            status="running",
            arguments_hash=_hash_arguments(arguments),
            arguments_redacted=_redact_arguments(arguments),
        ))

    def completed(self, *, tool_call_id: str, duration_ms: int | None = None, result_ref: str = "") -> None:
        record = self._record(tool_call_id)
        if record is not None:
            record.status = "completed"
            record.duration_ms = duration_ms
            record.result_ref = result_ref

    def failed(self, *, tool_call_id: str, error_code: str, duration_ms: int | None = None) -> None:
        record = self._record(tool_call_id)
        if record is not None:
            record.status = "failed"
            record.error_code = error_code
            record.duration_ms = duration_ms

    def _record(self, tool_call_id: str) -> ToolAuditRecord | None:
        for record in reversed(self.records):
            if record.tool_call_id == tool_call_id:
                return record
        return None


class BackendToolExecutor:
    async def execute(self, definition: ToolDefinition, request: ToolRequest, context: ToolContext) -> ToolResult:
        raise ToolExecutionError(code=str(ErrorCode.TOOL_DENIED), message="后端工具未实现", retryable=False)


class SandboxToolExecutor:
    def __init__(
        self,
        sandbox_provider,
        *,
        template: str = "python-2c2g",
        ttl_seconds: int = 300,
        audit_sink=None,
        artifact_store=None,
    ) -> None:
        self.sandbox_provider = sandbox_provider
        self.template = template
        self.ttl_seconds = ttl_seconds
        self.audit_sink = audit_sink
        # COS-backed artifact storage is only required for file transfer.  Keep
        # it lazy so python.execute and metadata-only workspace tools continue
        # to work in deployments that do not configure an artifact bucket.
        self._artifact_store = artifact_store
        self._sandbox_by_run: dict[str, str] = {}

    @property
    def artifact_store(self):
        if self._artifact_store is None:
            self._artifact_store = getattr(self.sandbox_provider, "artifact_store", None)
        return self._artifact_store

    async def execute(self, definition: ToolDefinition, request: ToolRequest, context: ToolContext) -> ToolResult:
        if request.name != "python.execute":
            raise ToolExecutionError(code=str(ErrorCode.TOOL_DENIED), message="沙箱工具未实现", retryable=False)
        try:
            sandbox_id = await self._ensure_sandbox(definition, context)
        except SandboxProviderError as exc:
            normalized = map_sandbox_error(exc, stage="sandbox.create", trace_id="")
            raise ToolExecutionError(code=normalized.code, message=normalized.message, retryable=normalized.retryable) from exc
        execution_request = ExecutionRequest(
            command=("python", "-c", str(request.arguments.get("code") or "")),
            cwd=".",
            environment={},
            timeout_seconds=request.timeout_seconds,
            input_refs=(),
        )
        try:
            result = await asyncio.wait_for(
                self.sandbox_provider.execute(sandbox_id, execution_request),
                timeout=max(0.001, float(request.timeout_seconds)),
            )
        except asyncio.TimeoutError as exc:
            raise ToolExecutionError(code=str(ErrorCode.SANDBOX_TIMEOUT), message="沙箱执行超时", retryable=True) from exc
        except SandboxProviderError as exc:
            normalized = map_sandbox_error(exc, stage="sandbox.execute", trace_id="")
            raise ToolExecutionError(code=normalized.code, message=normalized.message, retryable=normalized.retryable) from exc
        content = result.stdout if result.exit_code == 0 else result.stderr
        return ToolResult(
            success=result.exit_code == 0,
            content=content,
            artifact_refs=result.artifact_refs,
            error_code=None if result.exit_code == 0 else str(ErrorCode.INTERNAL_ERROR),
            retryable=False,
        )

    async def stage_files(
        self,
        definition: ToolDefinition,
        context: ToolContext,
        files: tuple[SandboxInputFile, ...],
    ) -> tuple[str, ...]:
        """Stage trusted company files without putting their bytes in model context."""

        if self.artifact_store is None:
            raise ToolExecutionError(
                code=str(ErrorCode.INTERNAL_ERROR),
                message="沙箱制品存储未配置",
                retryable=False,
            )
        try:
            sandbox_id = await self._ensure_sandbox(definition, context)
            refs = []
            for item in files:
                ref = await sync_to_async(self.artifact_store.put, thread_sensitive=False)(
                    company_id=context.company_id,
                    run_id=str(context.run_id),
                    kind="input",
                    name=item.name,
                    content=item.content,
                )
                refs.append(ref)
            return await self.sandbox_provider.upload(sandbox_id, tuple(refs))
        except ToolExecutionError:
            raise
        except SandboxProviderError as exc:
            normalized = map_sandbox_error(exc, stage="sandbox.upload", trace_id="")
            raise ToolExecutionError(
                code=normalized.code,
                message=normalized.message,
                retryable=normalized.retryable,
            ) from exc

    async def collect_output_files(self, context: ToolContext) -> tuple[SandboxOutputFile, ...]:
        """Collect only Provider-validated files from the current Run output directory."""

        sandbox_id = self._sandbox_by_run.get(str(context.run_id), "")
        if not sandbox_id:
            raise ToolExecutionError(
                code=str(ErrorCode.TOOL_INVALID_ARGUMENTS),
                message="当前 Run 尚未创建沙箱，请先调用 workspace.checkout 或 python.execute",
                retryable=False,
            )
        if self.artifact_store is None:
            raise ToolExecutionError(
                code=str(ErrorCode.INTERNAL_ERROR),
                message="沙箱制品存储未配置",
                retryable=False,
            )
        try:
            refs = await self.sandbox_provider.download(sandbox_id)
            output = []
            for ref in refs:
                payload = await sync_to_async(self.artifact_store.get, thread_sensitive=False)(
                    ref,
                    company_id=context.company_id,
                    run_id=str(context.run_id),
                )
                output.append(SandboxOutputFile(
                    name=payload.name,
                    content=bytes(payload.content),
                    content_type=str(payload.content_type or "application/octet-stream"),
                    artifact_ref=str(ref),
                ))
            return tuple(output)
        except SandboxProviderError as exc:
            normalized = map_sandbox_error(exc, stage="sandbox.download", trace_id="")
            raise ToolExecutionError(
                code=normalized.code,
                message=normalized.message,
                retryable=normalized.retryable,
            ) from exc

    async def terminate_run(self, run_id: str) -> None:
        sandbox_id = self._sandbox_by_run.pop(str(run_id), "")
        if sandbox_id:
            await self.sandbox_provider.terminate(sandbox_id)
            await self._audit("sandbox.completed", {"run_id": str(run_id), "sandbox_id": sandbox_id})

    async def _ensure_sandbox(self, definition: ToolDefinition, context: ToolContext) -> str:
        run_id = str(context.run_id)
        if run_id in self._sandbox_by_run:
            return self._sandbox_by_run[run_id]
        spec = SandboxSpec(
            run_id=run_id,
            company_id=context.company_id,
            template=self.template,
            cpu_cores=2,
            memory_mib=2048,
            ttl_seconds=self.ttl_seconds,
            network_policy=definition.network_policy,  # type: ignore[arg-type]
            labels={"agent_id": str(context.agent_id)},
        )
        handle = await self.sandbox_provider.create(spec)
        self._sandbox_by_run[run_id] = handle.sandbox_id
        await self._audit("sandbox.started", {
            "run_id": run_id,
            "sandbox_id": handle.sandbox_id,
            "provider": handle.provider,
            "template": spec.template,
            "cpu_cores": spec.cpu_cores,
            "memory_mib": spec.memory_mib,
            "network_policy": spec.network_policy,
            "ttl_seconds": spec.ttl_seconds,
        })
        return handle.sandbox_id

    async def _audit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.audit_sink is None:
            return
        if inspect.iscoroutinefunction(self.audit_sink):
            result = self.audit_sink(event_type, payload)
        else:
            result = await sync_to_async(self.audit_sink, thread_sensitive=True)(event_type, payload)
        if inspect.isawaitable(result):
            await result


def _hash_arguments(arguments: dict[str, Any]) -> str:
    encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _redact_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    redacted = {}
    for key, value in dict(arguments or {}).items():
        if any(token in str(key).lower() for token in ("key", "secret", "token", "password")):
            redacted[key] = "***"
        else:
            redacted[key] = value
    return redacted


def tool_call_id() -> str:
    return str(uuid4())
