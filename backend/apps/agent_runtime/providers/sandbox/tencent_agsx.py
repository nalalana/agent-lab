"""Production Tencent AGSX provider through its E2B-compatible endpoint."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shlex
import time
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any

from django.conf import settings
from django.utils import timezone as django_timezone

from apps.agent_runtime.contracts import (
    ExecutionRequest,
    ExecutionResult,
    SandboxHandle,
    SandboxSpec,
)
from apps.agent_runtime.enums import ErrorCode

from .artifacts import ArtifactLimitExceeded, CosRunArtifactStore, truncate_utf8
from .concurrency import (
    RedisCompanySandboxLimiter,
    SandboxConcurrencyLimitExceeded,
    SandboxConcurrencyUnavailable,
)
from .errors import (
    SandboxCreateError,
    SandboxProviderError,
    SandboxTimeoutError,
)
from .metrics import SandboxMetrics
from .policy import DjangoSandboxPolicyResolver, ResolvedSandboxPolicy
from .repository import (
    ACTIVE_STATUSES,
    STATUS_CREATING,
    STATUS_TERMINATED,
    DjangoSandboxExecutionRepository,
    SandboxRecord,
)


logger = logging.getLogger("agent_runtime.sandbox")


class TencentAgsxSandboxProvider:
    """Create one isolated AGSX sandbox lazily for each Agent Run.

    The installed E2B SDK is synchronous. Every SDK, Redis, COS, and ORM call
    is therefore moved to a worker thread so model streaming is never blocked.
    """

    provider_name = "tencent_agsx"
    # AGSX envd chooses the command user at runtime. Keep the Run root directly
    # under the standard world-writable /tmp instead of relying on an image
    # directory owned by root, then create each Run directory with umask 077.
    run_root = PurePosixPath("/tmp/touming-runs")
    environment_allowlist = frozenset({"LANG", "LC_ALL", "LC_CTYPE", "TZ", "PYTHONIOENCODING"})
    output_manifest_marker = "TOUMING_OUTPUT_MANIFEST_V1"
    output_manifest_source = """\
# TOUMING_OUTPUT_MANIFEST_V1
import json
import os
import stat
import sys

root = sys.argv[1]
max_entries = int(sys.argv[2])
entries = []


def append_entry(path, entry_type, size=0):
    entries.append({
        "path": os.path.relpath(path, root),
        "type": entry_type,
        "size": size,
    })
    return len(entries) > max_entries


def raise_walk_error(error):
    raise error


def scan():
    for current_root, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
        onerror=raise_walk_error,
    ):
        for name in tuple(directory_names):
            path = os.path.join(current_root, name)
            metadata = os.lstat(path)
            if stat.S_ISLNK(metadata.st_mode):
                directory_names.remove(name)
                if append_entry(path, "symlink"):
                    return True
            elif not stat.S_ISDIR(metadata.st_mode):
                directory_names.remove(name)
                if append_entry(path, "other"):
                    return True

        for name in file_names:
            path = os.path.join(current_root, name)
            metadata = os.lstat(path)
            if stat.S_ISLNK(metadata.st_mode):
                entry_type = "symlink"
                size = 0
            elif stat.S_ISREG(metadata.st_mode):
                entry_type = "file"
                size = metadata.st_size
            else:
                entry_type = "other"
                size = 0
            if append_entry(path, entry_type, size):
                return True
    return False


print(json.dumps(
    {
        "version": 1,
        "entries": entries,
        "limit_exceeded": scan(),
    },
    ensure_ascii=True,
    separators=(",", ":"),
))
"""

    def __init__(
        self,
        *,
        repository=None,
        policy_resolver=None,
        limiter=None,
        artifact_store=None,
        sandbox_class=None,
        metrics=None,
        now=None,
    ) -> None:
        self.repository = repository or DjangoSandboxExecutionRepository()
        self.policy_resolver = policy_resolver or DjangoSandboxPolicyResolver()
        self.limiter = limiter or RedisCompanySandboxLimiter()
        self._artifact_store = artifact_store
        self.sandbox_class = sandbox_class or self._load_sandbox_class()
        self.metrics = metrics or SandboxMetrics()
        self.now = now or django_timezone.now
        self._connected: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def create(self, spec: SandboxSpec) -> SandboxHandle:
        if not bool(getattr(settings, "AGSX_ENABLED", False)):
            raise SandboxCreateError("AGSX sandbox execution is disabled")
        policy = await asyncio.to_thread(self.policy_resolver.resolve, spec)
        lock = self._locks.setdefault(str(spec.run_id), asyncio.Lock())
        async with lock:
            return await self._create_locked(spec, policy)

    async def _create_locked(
        self,
        spec: SandboxSpec,
        policy: ResolvedSandboxPolicy,
    ) -> SandboxHandle:
        lease_ttl = policy.ttl_seconds + int(
            getattr(settings, "AGSX_SANDBOX_CLEANUP_GRACE_SECONDS", 120)
        )
        try:
            await asyncio.to_thread(
                self.limiter.acquire,
                policy.company_id,
                str(spec.run_id),
                limit=policy.max_concurrent_runs,
                ttl_seconds=lease_ttl,
            )
        except SandboxConcurrencyLimitExceeded as exc:
            self.metrics.increment(
                "sandbox.quota.rejected.total",
                self._policy_labels(
                    policy,
                    outcome="rejected",
                    quota_scope=exc.scope,
                ),
            )
            raise
        except SandboxConcurrencyUnavailable:
            self.metrics.increment(
                "sandbox.quota.backend_unavailable.total",
                self._policy_labels(policy, outcome="failed"),
            )
            raise

        provisional_expiry = self.now() + timedelta(seconds=policy.ttl_seconds)
        try:
            record, created = await asyncio.to_thread(
                self.repository.reserve,
                run_id=str(spec.run_id),
                policy=policy,
                expires_at=provisional_expiry,
            )
        except Exception:
            await self._release_lease(policy.company_id, str(spec.run_id))
            raise

        if not created:
            if record.status in ACTIVE_STATUSES and record.external_id:
                return self._handle(record)
            if record.status == STATUS_CREATING:
                # The lease member is Run-scoped. Releasing it here would drop
                # the first Worker's still-valid company concurrency slot.
                raise SandboxCreateError("sandbox creation is already in progress")
            await self._release_lease(policy.company_id, str(spec.run_id))
            raise SandboxCreateError("sandbox execution is terminal or inconsistent")

        sandbox = None
        started = time.monotonic()
        try:
            sandbox = await asyncio.to_thread(
                self.sandbox_class.create,
                template=policy.template,
                timeout=policy.ttl_seconds,
                metadata=self._metadata(record, spec),
                envs={},
                secure=True,
                allow_internet_access=False,
                lifecycle={"on_timeout": "kill"},
                request_timeout=float(getattr(settings, "AGSX_REQUEST_TIMEOUT_SECONDS", 60)),
            )
            # Store the supplier ID immediately. If validation or cleanup fails,
            # the Reaper can still reconnect to this instance from durable state.
            record = await asyncio.to_thread(
                self.repository.mark_provider_instance,
                record.id,
                external_id=str(sandbox.sandbox_id),
            )
            info = await asyncio.to_thread(sandbox.get_info)
            self._validate_effective_configuration(info, policy)
            ready_at = self.now()
            expires_at = self._expires_at(info, ready_at, policy.ttl_seconds)
            record = await asyncio.to_thread(
                self.repository.mark_ready,
                record.id,
                external_id=str(sandbox.sandbox_id),
                ready_at=ready_at,
                expires_at=expires_at,
            )
            self._connected[record.id] = sandbox
            self.metrics.observe(
                "sandbox.create.duration_ms",
                (time.monotonic() - started) * 1000,
                self._labels(record, outcome="success"),
            )
            return self._handle(record)
        except Exception as exc:
            if sandbox is not None:
                try:
                    await asyncio.to_thread(sandbox.kill)
                except Exception:
                    logger.exception("failed to clean up AGSX sandbox after create error")
            safe_error = self._translate_exception(exc, stage="create")
            await self._mark_failed_safely(
                record.id,
                str(getattr(safe_error, "error_code", ErrorCode.SANDBOX_CREATE_FAILED)),
            )
            await self._release_lease(
                policy.company_id,
                str(spec.run_id),
                suppress_errors=True,
            )
            self.metrics.observe(
                "sandbox.create.duration_ms",
                (time.monotonic() - started) * 1000,
                self._labels(record, outcome="failed"),
            )
            raise safe_error from exc

    async def execute(self, sandbox_id: str, request: ExecutionRequest) -> ExecutionResult:
        record = await asyncio.to_thread(self.repository.get, sandbox_id)
        self._assert_executable(record)
        command = self._command(request.command)
        cwd = self._cwd(record, request.cwd)
        environment = self._environment(request.environment)
        timeout_seconds = self._execution_timeout(record, request.timeout_seconds)
        sandbox = await self._connect(record)
        await asyncio.to_thread(self.repository.mark_running, record.id)

        started = time.monotonic()
        stage = "prepare_directories"
        try:
            await asyncio.to_thread(self._prepare_run_directories, sandbox, record)
            if request.input_refs:
                stage = "upload_inputs"
                await asyncio.to_thread(
                    self._upload_inputs,
                    sandbox,
                    record,
                    request.input_refs,
                )
            stage = "run_command"
            result = await asyncio.to_thread(
                sandbox.commands.run,
                command,
                cwd=cwd,
                envs=environment,
                timeout=timeout_seconds,
            )
            stage = "process_command_output"
            stdout = str(getattr(result, "stdout", "") or "")
            stderr = str(getattr(result, "stderr", "") or "")
            stdout_value, stdout_truncated = truncate_utf8(
                stdout,
                int(getattr(settings, "AGENT_RUNTIME_SANDBOX_STDOUT_LIMIT_BYTES", 65536)),
            )
            stderr_value, stderr_truncated = truncate_utf8(
                stderr,
                int(getattr(settings, "AGENT_RUNTIME_SANDBOX_STDERR_LIMIT_BYTES", 65536)),
            )
            artifact_refs: list[str] = []
            stage = "store_command_overflow"
            if stdout_truncated:
                artifact_refs.append(
                    self._put_overflow(record, "stdout.txt", stdout.encode("utf-8"))
                )
            if stderr_truncated:
                artifact_refs.append(
                    self._put_overflow(record, "stderr.txt", stderr.encode("utf-8"))
                )
            stage = "collect_output_artifacts"
            artifact_refs.extend(await asyncio.to_thread(self._collect_outputs, sandbox, record))
            duration_ms = max(int((time.monotonic() - started) * 1000), 0)
            stage = "mark_ready"
            await asyncio.to_thread(self.repository.mark_ready_after_execute, record.id)
            self.metrics.observe(
                "sandbox.execute.duration_ms",
                duration_ms,
                self._labels(record, outcome="success"),
            )
            if stdout_truncated or stderr_truncated:
                self.metrics.increment("sandbox.output.truncated.total", self._labels(record))
            return ExecutionResult(
                exit_code=int(getattr(result, "exit_code", 1)),
                stdout=stdout_value,
                stderr=stderr_value,
                artifact_refs=tuple(artifact_refs),
                duration_ms=duration_ms,
                truncated=stdout_truncated or stderr_truncated,
            )
        except Exception as exc:
            exit_code = getattr(exc, "exit_code", None)
            if isinstance(exit_code, bool) or not isinstance(exit_code, int):
                exit_code = None
            logger.warning(
                "AGSX sandbox execution failed sandbox_execution=%s run=%s "
                "stage=%s error_type=%s exit_code=%s",
                record.id,
                record.run_id,
                stage,
                type(exc).__name__,
                exit_code,
            )
            safe_error = self._translate_exception(exc, stage="execute")
            await self._mark_failed_safely(
                record.id,
                str(getattr(safe_error, "error_code", ErrorCode.INTERNAL_ERROR)),
            )
            self.metrics.observe(
                "sandbox.execute.duration_ms",
                (time.monotonic() - started) * 1000,
                self._labels(record, outcome="failed"),
            )
            raise safe_error from exc

    async def terminate(self, sandbox_id: str) -> None:
        record = await asyncio.to_thread(self.repository.get, sandbox_id)
        if record.status == STATUS_TERMINATED:
            await self._release_lease(record.company_id, record.run_id, suppress_errors=True)
            self._locks.pop(record.run_id, None)
            return

        started = time.monotonic()
        try:
            if record.external_id:
                sandbox = self._connected.pop(record.id, None)
                if sandbox is None:
                    try:
                        sandbox = await asyncio.to_thread(
                            self.sandbox_class.connect,
                            record.external_id,
                            request_timeout=float(
                                getattr(settings, "AGSX_REQUEST_TIMEOUT_SECONDS", 60)
                            ),
                        )
                    except Exception as exc:
                        if not self._is_not_found(exc):
                            raise
                if sandbox is not None:
                    try:
                        await asyncio.to_thread(sandbox.kill)
                    except Exception as exc:
                        # Another cleanup path may win between connect and kill.
                        if not self._is_not_found(exc):
                            raise
            record = await self._persist_terminated(record)
            self.metrics.observe(
                "sandbox.usage.seconds",
                record.billed_seconds,
                self._labels(record, outcome="success"),
            )
            self.metrics.observe(
                "sandbox.terminate.duration_ms",
                (time.monotonic() - started) * 1000,
                self._labels(record, outcome="success"),
            )
        except Exception as exc:
            await self._mark_failed_safely(record.id, str(ErrorCode.INTERNAL_ERROR))
            self.metrics.increment(
                "sandbox.terminate.total",
                self._labels(record, outcome="failed"),
            )
            raise self._translate_exception(exc, stage="terminate") from exc

    async def get_status(self, sandbox_id: str) -> dict[str, object]:
        """Return a safe operational view; supplier IDs are deliberately omitted."""
        record = await asyncio.to_thread(self.repository.get, sandbox_id)
        running = False
        if record.external_id and record.status in ACTIVE_STATUSES:
            try:
                sandbox = await self._connect(record)
                running = bool(await asyncio.to_thread(sandbox.is_running))
            except Exception as exc:
                if not self._is_not_found(exc):
                    raise self._translate_exception(exc, stage="status") from exc
            if not running:
                record = await self._persist_terminated(record)
                self.metrics.observe(
                    "sandbox.usage.seconds",
                    record.billed_seconds,
                    self._labels(record, outcome="success"),
                )
                self.metrics.increment(
                    "sandbox.status.reconciled.total",
                    self._labels(record, outcome="terminated"),
                )
        return {
            "schemaVersion": 1,
            "sandbox_execution_id": record.id,
            "run_id": record.run_id,
            "provider": record.provider,
            "status": record.status,
            "running": running,
            "expires_at": record.expires_at.isoformat(),
        }

    async def upload(self, sandbox_id: str, input_refs: tuple[str, ...]) -> tuple[str, ...]:
        record = await asyncio.to_thread(self.repository.get, sandbox_id)
        self._assert_executable(record)
        sandbox = await self._connect(record)
        await asyncio.to_thread(self._prepare_run_directories, sandbox, record)
        return await asyncio.to_thread(self._upload_inputs, sandbox, record, input_refs)

    async def download(self, sandbox_id: str) -> tuple[str, ...]:
        record = await asyncio.to_thread(self.repository.get, sandbox_id)
        self._assert_executable(record)
        sandbox = await self._connect(record)
        return await asyncio.to_thread(self._collect_outputs, sandbox, record)

    async def _connect(self, record: SandboxRecord):
        sandbox = self._connected.get(record.id)
        if sandbox is not None:
            return sandbox
        if not record.external_id:
            raise SandboxProviderError("sandbox has no provider instance")
        try:
            sandbox = await asyncio.to_thread(
                self.sandbox_class.connect,
                record.external_id,
                request_timeout=float(getattr(settings, "AGSX_REQUEST_TIMEOUT_SECONDS", 60)),
            )
        except Exception as exc:
            if self._is_not_found(exc):
                raise SandboxProviderError("sandbox provider instance was not found") from exc
            raise self._translate_exception(exc, stage="connect") from exc
        self._connected[record.id] = sandbox
        return sandbox

    @classmethod
    def _prepare_run_directories(cls, sandbox, record: SandboxRecord) -> None:
        root = cls._run_directory(record)
        directories = [root / "input", root / "work", root / "output"]
        mkdir_command = shlex.join(["mkdir", "-p", *(str(path) for path in directories)])
        # Keep the model-facing path natural without broadening the publish
        # boundary: work/output is only an alias to this Run's protected output.
        output_alias = root / "work" / "output"
        link_command = shlex.join(["ln", "-sfn", "../output", str(output_alias)])
        command = f"umask 077 && {mkdir_command} && {link_command}"
        result = sandbox.commands.run(command, timeout=10)
        if int(getattr(result, "exit_code", 1)) != 0:
            raise SandboxProviderError("sandbox Run directories could not be created")

    def _upload_inputs(
        self,
        sandbox,
        record: SandboxRecord,
        refs: tuple[str, ...],
    ) -> tuple[str, ...]:
        store = self.artifact_store
        max_count = int(getattr(settings, "AGENT_RUNTIME_SANDBOX_MAX_ARTIFACT_COUNT", 20))
        max_total = int(
            getattr(settings, "AGENT_RUNTIME_SANDBOX_MAX_TOTAL_ARTIFACT_BYTES", 50 * 1024 * 1024)
        )
        if len(refs) > max_count:
            raise ArtifactLimitExceeded("sandbox input set exceeds configured limits")

        payloads = []
        total = 0
        for index, ref in enumerate(refs):
            payload = store.get(ref, company_id=record.company_id, run_id=record.run_id)
            total += len(payload.content)
            if total > max_total:
                raise ArtifactLimitExceeded("sandbox input set exceeds configured limits")
            path = self._run_directory(record) / "input" / f"{index:03d}-{payload.name}"
            payloads.append((path, payload.content))

        # Validate the whole set before writing so an oversized request cannot
        # leave a partially uploaded input directory behind.
        uploaded = []
        for path, content in payloads:
            sandbox.files.write(str(path), content)
            uploaded.append(str(path))
        if uploaded:
            # Inputs are immutable inside the sandbox. Agent code must copy data
            # to /work and publish results through /output explicitly.
            chmod_command = shlex.join(["chmod", "0444", "--", *uploaded])
            result = sandbox.commands.run(chmod_command, timeout=10)
            if int(getattr(result, "exit_code", 1)) != 0:
                raise SandboxProviderError("sandbox input permissions could not be restricted")
        return tuple(uploaded)

    def _collect_outputs(self, sandbox, record: SandboxRecord) -> tuple[str, ...]:
        output_dir = self._run_directory(record) / "output"
        max_count = int(getattr(settings, "AGENT_RUNTIME_SANDBOX_MAX_ARTIFACT_COUNT", 20))
        max_total = int(
            getattr(settings, "AGENT_RUNTIME_SANDBOX_MAX_TOTAL_ARTIFACT_BYTES", 50 * 1024 * 1024)
        )
        max_file = int(
            getattr(settings, "AGENT_RUNTIME_SANDBOX_MAX_ARTIFACT_BYTES", 10 * 1024 * 1024)
        )
        entries = self._output_manifest(sandbox, output_dir, max_count=max_count)
        files = []
        declared_total = 0
        for entry in entries:
            if not isinstance(entry, dict):
                raise SandboxProviderError("sandbox output manifest is invalid")
            entry_type = str(entry.get("type", "")).lower()
            if entry_type == "symlink":
                raise SandboxProviderError("sandbox output symlinks are not allowed")
            if entry_type != "file":
                raise SandboxProviderError("sandbox output contains an unsupported file type")
            raw_path = entry.get("path")
            if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
                raise SandboxProviderError("sandbox output manifest is invalid")
            relative_path = PurePosixPath(raw_path)
            if relative_path.is_absolute() or ".." in relative_path.parts:
                raise SandboxProviderError("sandbox output escaped the Run directory")
            candidate = output_dir / relative_path
            try:
                candidate.relative_to(output_dir)
            except ValueError as exc:
                raise SandboxProviderError("sandbox output escaped the Run directory") from exc
            raw_size = entry.get("size")
            if isinstance(raw_size, bool) or not isinstance(raw_size, int) or raw_size < 0:
                raise SandboxProviderError("sandbox output manifest is invalid")
            size = raw_size
            if size > max_file:
                raise ArtifactLimitExceeded("sandbox output exceeds the per-file limit")
            declared_total += size
            files.append((candidate, relative_path.name))
        if len(files) > max_count or declared_total > max_total:
            raise ArtifactLimitExceeded("sandbox output set exceeds configured limits")

        payloads = []
        actual_total = 0
        # Validate every payload before uploading any of them. SDK metadata is
        # useful for an early reject but is not a trusted size boundary.
        for path, name in files:
            content = bytes(sandbox.files.read(str(path), format="bytes"))
            actual_total += len(content)
            if len(content) > max_file or actual_total > max_total:
                raise ArtifactLimitExceeded("sandbox output set exceeds configured limits")
            payloads.append((name, content))

        refs = []
        for name, content in payloads:
            refs.append(
                self.artifact_store.put(
                    company_id=record.company_id,
                    run_id=record.run_id,
                    kind="output",
                    name=name,
                    content=content,
                )
            )
        if refs:
            self.metrics.observe("sandbox.artifact.bytes", actual_total, self._labels(record))
        return tuple(refs)

    def _output_manifest(
        self,
        sandbox,
        output_dir: PurePosixPath,
        *,
        max_count: int,
    ) -> list[dict[str, object]]:
        """List output files without relying on AGSX's optional files.list RPC.

        Tencent AGSX custom Tool compatibility guarantees command execution and
        file read/write operations, while directory-listing support can vary by
        runtime image. A fixed, non-user-controlled script produces a bounded
        manifest through the already-required command execution data plane.
        """

        command = shlex.join(
            (
                "python",
                "-c",
                self.output_manifest_source,
                str(output_dir),
                str(max(max_count, 0)),
            )
        )
        result = sandbox.commands.run(
            command,
            cwd=str(output_dir),
            envs={},
            timeout=10,
        )
        if int(getattr(result, "exit_code", 1)) != 0:
            raise SandboxProviderError("sandbox output manifest could not be generated")
        try:
            payload = json.loads(str(getattr(result, "stdout", "") or ""))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SandboxProviderError("sandbox output manifest is invalid") from exc
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise SandboxProviderError("sandbox output manifest is invalid")
        entries = payload.get("entries")
        limit_exceeded = payload.get("limit_exceeded")
        if not isinstance(entries, list) or not isinstance(limit_exceeded, bool):
            raise SandboxProviderError("sandbox output manifest is invalid")
        if limit_exceeded or len(entries) > max_count:
            raise ArtifactLimitExceeded("sandbox output set exceeds configured limits")
        return entries

    def _put_overflow(self, record: SandboxRecord, name: str, content: bytes) -> str:
        return self.artifact_store.put(
            company_id=record.company_id,
            run_id=record.run_id,
            kind="overflow",
            name=name,
            content=content,
            allow_overflow=True,
        )

    @property
    def artifact_store(self):
        if self._artifact_store is None:
            self._artifact_store = CosRunArtifactStore.from_settings()
        return self._artifact_store

    @classmethod
    def _cwd(cls, record: SandboxRecord, value: str) -> str:
        root = cls._run_directory(record)
        work = root / "work"
        candidate = PurePosixPath(str(value or work))
        if not candidate.is_absolute():
            candidate = work / candidate
        if ".." in candidate.parts:
            raise SandboxProviderError("sandbox cwd cannot contain parent traversal")
        try:
            candidate.relative_to(work)
        except ValueError as exc:
            raise SandboxProviderError(
                "sandbox cwd must stay inside the Run work directory"
            ) from exc
        return str(candidate)

    @staticmethod
    def _command(command: tuple[str, ...]) -> str:
        if not command or any("\x00" in str(part) for part in command):
            raise SandboxProviderError("sandbox command is invalid")
        if len(command) > 128 or sum(len(str(part)) for part in command) > 32768:
            raise SandboxProviderError("sandbox command is too large")
        return shlex.join(str(part) for part in command)

    @classmethod
    def _environment(cls, values: dict[str, str]) -> dict[str, str]:
        result = {}
        for key, value in dict(values or {}).items():
            name = str(key)
            if name not in cls.environment_allowlist:
                raise SandboxProviderError(f"sandbox environment variable is not allowed: {name}")
            text = str(value)
            if "\x00" in text or len(text) > 4096:
                raise SandboxProviderError("sandbox environment value is invalid")
            result[name] = text
        return result

    def _execution_timeout(self, record: SandboxRecord, requested: int) -> int:
        remaining = int(
            (self._as_utc(record.expires_at) - self._as_utc(self.now())).total_seconds()
        )
        timeout = min(max(int(requested), 1), max(remaining, 0))
        if timeout <= 0:
            raise SandboxTimeoutError("sandbox lifetime has expired")
        return timeout

    def _assert_executable(self, record: SandboxRecord) -> None:
        if record.status not in ACTIVE_STATUSES or not record.external_id:
            raise SandboxProviderError("sandbox is not active")
        if self._as_utc(record.expires_at) <= self._as_utc(self.now()):
            raise SandboxTimeoutError("sandbox lifetime has expired")

    @classmethod
    def _run_directory(cls, record: SandboxRecord) -> PurePosixPath:
        # Run IDs are UUIDs by contract. Re-hashing provides a safe fallback for
        # malformed historical records without placing arbitrary text in paths.
        try:
            run_component = str(__import__("uuid").UUID(str(record.run_id)))
        except (TypeError, ValueError, AttributeError):
            run_component = hashlib.sha256(str(record.run_id).encode("utf-8")).hexdigest()[:32]
        return cls.run_root / run_component

    @staticmethod
    def _metadata(record: SandboxRecord, spec: SandboxSpec) -> dict[str, str]:
        return {
            "schemaVersion": "1",
            "service": "touming",
            "sandbox_execution_id": record.id,
            "run_id": str(spec.run_id),
            "company_scope": hashlib.sha256(str(spec.company_id).encode("utf-8")).hexdigest()[:24],
        }

    def _validate_effective_configuration(
        self,
        info,
        policy: ResolvedSandboxPolicy,
    ) -> None:
        self._validate_network_isolation(info, policy)
        observed_cpu = int(getattr(info, "cpu_count", 0) or 0)
        observed_memory = int(getattr(info, "memory_mb", 0) or 0)
        matches = observed_cpu == policy.cpu_cores and observed_memory == policy.memory_mib
        if not matches:
            self.metrics.increment(
                "sandbox.resource_mismatch.total",
                {"provider": self.provider_name},
            )
            if bool(getattr(settings, "AGSX_ENFORCE_EFFECTIVE_RESOURCES", False)):
                raise SandboxCreateError("sandbox effective resources do not match policy")
            logger.warning(
                "AGSX effective resources differ from policy",
                extra={
                    "observed_cpu": observed_cpu,
                    "observed_memory_mib": observed_memory,
                    "expected_cpu": policy.cpu_cores,
                    "expected_memory_mib": policy.memory_mib,
                },
            )

    def _validate_network_isolation(
        self,
        info,
        policy: ResolvedSandboxPolicy,
    ) -> None:
        internet_allowed = getattr(info, "allow_internet_access", None)
        if internet_allowed is False:
            return
        if internet_allowed is True:
            raise SandboxCreateError("sandbox public egress was not disabled")
        if internet_allowed is None and policy.network_policy == "isolated":
            # Tencent AGSX's E2B-compatible get_info response currently omits
            # this flag. The production deployment path independently enforces
            # NetworkMode=SANDBOX on the Tool, and Instances inherit that mode.
            # Only accept the omission when the resolved runtime policy agrees.
            self.metrics.increment(
                "sandbox.network_metadata_unavailable.total",
                {"provider": self.provider_name},
            )
            logger.info(
                "AGSX omitted network isolation metadata; "
                "using deployment-verified Tool policy",
                extra={"expected_network_policy": policy.network_policy},
            )
            return
        raise SandboxCreateError("sandbox network isolation could not be verified")

    @staticmethod
    def _expires_at(info, ready_at: datetime, ttl_seconds: int) -> datetime:
        value = getattr(info, "end_at", None)
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return ready_at + timedelta(seconds=ttl_seconds)

    @staticmethod
    def _handle(record: SandboxRecord) -> SandboxHandle:
        return SandboxHandle(
            sandbox_id=record.id,
            provider=record.provider,
            external_id=record.external_id,
        )

    async def _release_lease(
        self,
        company_id: object,
        run_id: str,
        *,
        suppress_errors: bool = False,
    ) -> None:
        try:
            await asyncio.to_thread(self.limiter.release, company_id, run_id)
        except Exception:
            if not suppress_errors:
                raise
            logger.exception("failed to release sandbox concurrency lease")

    async def _persist_terminated(self, record: SandboxRecord) -> SandboxRecord:
        terminated_at = self.now()
        billed_seconds = self._billed_seconds(record, terminated_at)
        await asyncio.to_thread(
            self.repository.mark_terminated,
            record.id,
            terminated_at=terminated_at,
            billed_seconds=billed_seconds,
        )
        await self._release_lease(record.company_id, record.run_id, suppress_errors=True)
        self._connected.pop(record.id, None)
        self._locks.pop(record.run_id, None)
        return await asyncio.to_thread(self.repository.get, record.id)

    async def _mark_failed_safely(self, record_id: str, error_code: str) -> None:
        try:
            await asyncio.to_thread(self.repository.mark_failed, record_id, error_code)
        except Exception:
            # The original provider failure is more useful to callers. The DB
            # failure remains restricted to server logs for operator recovery.
            logger.exception(
                "failed to persist sandbox failure state",
                extra={"sandbox_execution_id": record_id},
            )

    @classmethod
    def _billed_seconds(cls, record: SandboxRecord, terminated_at: datetime) -> int:
        elapsed = max(
            int(cls._seconds_between(terminated_at, record.created_at)),
            0,
        )
        lifetime = max(
            int(cls._seconds_between(record.expires_at, record.created_at)),
            0,
        )
        return min(elapsed, lifetime)

    @classmethod
    def _seconds_between(cls, later: datetime, earlier: datetime) -> float:
        return (cls._as_utc(later) - cls._as_utc(earlier)).total_seconds()

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        """Normalize Django-naive and SDK-aware timestamps for arithmetic."""
        if django_timezone.is_naive(value):
            value = django_timezone.make_aware(
                value,
                django_timezone.get_current_timezone(),
            )
        return value.astimezone(timezone.utc)

    @staticmethod
    def _translate_exception(error: Exception, *, stage: str) -> SandboxProviderError:
        if isinstance(error, SandboxProviderError):
            return error
        description = str(error).lower()
        name = type(error).__name__.lower()
        if "timeout" in name or "timeout" in description or "timed out" in description:
            return SandboxTimeoutError("sandbox operation timed out")
        if stage == "create":
            return SandboxCreateError("sandbox could not be created")
        return SandboxProviderError("sandbox operation failed")

    @staticmethod
    def _is_not_found(error: Exception) -> bool:
        return "notfound" in type(error).__name__.lower() or "not found" in str(error).lower()

    @classmethod
    def _labels(
        cls,
        record: SandboxRecord,
        *,
        outcome: str | None = None,
    ) -> dict[str, str]:
        labels = {
            "provider": record.provider,
            "template": record.template,
            "environment": str(
                getattr(settings, "ENVIRONMENT", os.getenv("ENVIRONMENT", "unknown"))
            ),
        }
        if outcome:
            labels["outcome"] = outcome
        return labels

    @classmethod
    def _policy_labels(
        cls,
        policy: ResolvedSandboxPolicy,
        *,
        outcome: str,
        quota_scope: str | None = None,
    ) -> dict[str, str]:
        labels = {
            "provider": cls.provider_name,
            "template": policy.template,
            "environment": str(
                getattr(settings, "ENVIRONMENT", os.getenv("ENVIRONMENT", "unknown"))
            ),
            "outcome": outcome,
        }
        if quota_scope:
            labels["quota_scope"] = quota_scope
        return labels

    @staticmethod
    def _load_sandbox_class():
        from e2b_code_interpreter import Sandbox

        return Sandbox
