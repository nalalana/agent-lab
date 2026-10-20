"""Deterministic in-memory sandbox provider used by Phase 0 tests.

The fake verifies the frozen provider contract without launching a local process,
reading environment variables, or making a request to Tencent AGSX.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque
from uuid import uuid4

from apps.agent_runtime.contracts import (
    ExecutionRequest,
    ExecutionResult,
    SandboxHandle,
    SandboxSpec,
)

from .errors import SandboxCreateError, SandboxProviderError


@dataclass
class FakeSandboxProvider:
    """A scripted provider with one active sandbox allowed for each Run.

    Tests may enqueue an ``ExecutionResult`` or an exception.  This lets callers
    cover success, provider failure, and timeout paths deterministically.
    """

    provider_name: str = "fake"
    create_error: Exception | None = None
    execution_outcomes: Deque[ExecutionResult | Exception] = field(default_factory=deque)
    _active_specs: dict[str, SandboxSpec] = field(default_factory=dict, init=False)
    _terminated_sandbox_ids: list[str] = field(default_factory=list, init=False)
    _created_sandbox_ids: list[str] = field(default_factory=list, init=False)
    _uploaded_input_refs: dict[str, tuple[str, ...]] = field(default_factory=dict, init=False)
    _download_refs: dict[str, tuple[str, ...]] = field(default_factory=dict, init=False)

    @property
    def active_sandbox_ids(self) -> frozenset[str]:
        """Return only active IDs so tests cannot mutate internal state."""
        return frozenset(self._active_specs)

    @property
    def terminated_sandbox_ids(self) -> tuple[str, ...]:
        """Expose termination history for cleanup assertions."""
        return tuple(self._terminated_sandbox_ids)

    @property
    def create_count(self) -> int:
        return len(self._created_sandbox_ids)

    @property
    def uploaded_input_refs(self) -> dict[str, tuple[str, ...]]:
        return dict(self._uploaded_input_refs)

    def enqueue_execution(self, outcome: ExecutionResult | Exception) -> None:
        """Queue the next execution result without invoking any real command."""
        self.execution_outcomes.append(outcome)

    async def create(self, spec: SandboxSpec) -> SandboxHandle:
        """Create a fake handle after enforcing P0 invariants."""
        if self.create_error is not None:
            raise self.create_error
        if spec.cpu_cores <= 0 or spec.memory_mib <= 0 or spec.ttl_seconds <= 0:
            raise SandboxCreateError("sandbox resource values must be positive")
        if any(active_spec.run_id == spec.run_id for active_spec in self._active_specs.values()):
            raise SandboxCreateError("only one active sandbox is allowed for a run")

        sandbox_id = str(uuid4())
        self._active_specs[sandbox_id] = spec
        self._created_sandbox_ids.append(sandbox_id)
        # The external ID intentionally stays inside the provider-facing handle.
        return SandboxHandle(
            sandbox_id=sandbox_id,
            provider=self.provider_name,
            external_id=f"fake-{uuid4()}",
        )

    async def execute(self, sandbox_id: str, request: ExecutionRequest) -> ExecutionResult:
        """Return a scripted outcome for an active fake sandbox."""
        if sandbox_id not in self._active_specs:
            raise SandboxProviderError("sandbox is not active")
        if not request.command:
            raise SandboxProviderError("sandbox command must not be empty")
        if request.timeout_seconds <= 0:
            raise SandboxProviderError("sandbox command timeout must be positive")

        if self.execution_outcomes:
            outcome = self.execution_outcomes.popleft()
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return ExecutionResult(
            exit_code=0,
            stdout="fake sandbox execution completed\n",
            stderr="",
            artifact_refs=(),
            duration_ms=0,
            truncated=False,
        )

    async def terminate(self, sandbox_id: str) -> None:
        """Terminate idempotently; a repeated cleanup call remains successful."""
        self._active_specs.pop(sandbox_id, None)
        if sandbox_id not in self._terminated_sandbox_ids:
            self._terminated_sandbox_ids.append(sandbox_id)

    async def upload(self, sandbox_id: str, input_refs: tuple[str, ...]) -> tuple[str, ...]:
        if sandbox_id not in self._active_specs:
            raise SandboxProviderError("sandbox is not active")
        refs = tuple(str(ref) for ref in input_refs)
        self._uploaded_input_refs[sandbox_id] = refs
        return refs

    async def download(self, sandbox_id: str) -> tuple[str, ...]:
        if sandbox_id not in self._active_specs:
            raise SandboxProviderError("sandbox is not active")
        return self._download_refs.get(sandbox_id, ())

    def set_download_refs(self, sandbox_id: str, refs: tuple[str, ...]) -> None:
        self._download_refs[sandbox_id] = tuple(str(ref) for ref in refs)

    async def get_status(self, sandbox_id: str) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "sandbox_execution_id": sandbox_id,
            "provider": self.provider_name,
            "status": "ready" if sandbox_id in self._active_specs else "terminated",
            "running": sandbox_id in self._active_specs,
        }
