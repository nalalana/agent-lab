from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

from django.test import SimpleTestCase

from apps.agent_runtime.contracts import ExecutionRequest, ExecutionResult, SandboxSpec
from apps.agent_runtime.enums import ErrorCode
from apps.agent_runtime.providers.sandbox.errors import (
    SandboxCancelledError,
    SandboxCreateError,
    SandboxTimeoutError,
    map_sandbox_error,
)
from apps.agent_runtime.providers.sandbox.fake import FakeSandboxProvider
from apps.agent_runtime.providers.sandbox.phase0_smoke import (
    ValidationFailure,
    test_execution_timeout,
    test_isolated_network,
    test_sandbox_specification,
)


class FakeSandboxProviderTests(SimpleTestCase):
    def test_create_execute_and_idempotent_terminate(self):
        async def exercise():
            provider = FakeSandboxProvider()
            handle = await provider.create(_spec())
            result = await provider.execute(handle.sandbox_id, _request())
            await provider.terminate(handle.sandbox_id)
            await provider.terminate(handle.sandbox_id)
            return provider, handle, result

        provider, handle, result = asyncio.run(exercise())

        UUID(handle.sandbox_id)
        self.assertEqual(handle.provider, "fake")
        self.assertEqual(result.stdout, "fake sandbox execution completed\n")
        self.assertEqual(provider.active_sandbox_ids, frozenset())
        self.assertEqual(provider.terminated_sandbox_ids, (handle.sandbox_id,))

    def test_provider_rejects_second_active_sandbox_for_same_run(self):
        async def exercise():
            provider = FakeSandboxProvider()
            await provider.create(_spec())
            with self.assertRaises(SandboxCreateError):
                await provider.create(_spec())

        asyncio.run(exercise())

    def test_provider_replays_scripted_timeout(self):
        async def exercise():
            provider = FakeSandboxProvider()
            handle = await provider.create(_spec())
            provider.enqueue_execution(SandboxTimeoutError("command timed out"))
            with self.assertRaises(SandboxTimeoutError):
                await provider.execute(handle.sandbox_id, _request())

        asyncio.run(exercise())


class SandboxErrorMappingTests(SimpleTestCase):
    def test_timeout_and_cancelled_errors_keep_the_frozen_codes(self):
        timeout = map_sandbox_error(
            SandboxTimeoutError("provider details must not leave the backend"),
            stage="sandbox.execute",
            trace_id="trace-timeout",
        )
        cancelled = map_sandbox_error(
            SandboxCancelledError("cancelled"),
            stage="sandbox.execute",
            trace_id="trace-cancelled",
        )

        self.assertEqual(timeout.code, str(ErrorCode.SANDBOX_TIMEOUT))
        self.assertTrue(timeout.retryable)
        self.assertEqual(cancelled.code, str(ErrorCode.RUN_CANCELLED))
        self.assertFalse(cancelled.retryable)

    def test_create_auth_error_is_safe_and_not_retryable(self):
        error = map_sandbox_error(
            RuntimeError("invalid api key e2b_sensitive_value"),
            stage="sandbox.create",
            trace_id="trace-create",
        )

        self.assertEqual(error.code, str(ErrorCode.SANDBOX_CREATE_FAILED))
        self.assertFalse(error.retryable)
        self.assertNotIn("e2b_sensitive_value", error.message)

    def test_unknown_execution_error_is_safe_and_retryable(self):
        error = map_sandbox_error(
            RuntimeError("provider returned an internal response"),
            stage="sandbox.execute",
            trace_id="trace-execution",
        )

        self.assertEqual(error.code, str(ErrorCode.INTERNAL_ERROR))
        self.assertTrue(error.retryable)
        self.assertEqual(error.message, "Sandbox operation failed")


class Phase0SmokeValidationTests(SimpleTestCase):
    def test_specification_records_observed_values_before_rejecting_wrong_memory(self):
        started_at = datetime(2026, 8, 11, tzinfo=timezone.utc)
        sandbox = SimpleNamespace(
            get_info=lambda: SimpleNamespace(
                cpu_count=2,
                memory_mb=1000,
                started_at=started_at,
                end_at=started_at + timedelta(seconds=300),
                state="running",
                allow_internet_access=False,
            )
        )
        report: dict[str, object] = {}

        with self.assertRaises(ValidationFailure):
            test_sandbox_specification(sandbox, report)

        self.assertEqual(report["sandbox_cpu_cores"], 2)
        self.assertEqual(report["sandbox_memory_mib"], 1000)
        self.assertTrue(report["sandbox_public_egress_disabled"])

    def test_specification_accepts_the_frozen_2c2g_isolated_configuration(self):
        started_at = datetime(2026, 8, 11, tzinfo=timezone.utc)
        sandbox = SimpleNamespace(
            get_info=lambda: SimpleNamespace(
                cpu_count=2,
                memory_mb=2048,
                started_at=started_at,
                end_at=started_at + timedelta(seconds=300),
                state="running",
                allow_internet_access=False,
            )
        )
        report: dict[str, object] = {}

        test_sandbox_specification(sandbox, report)

        self.assertTrue(report["sandbox_specification_verified"])
        self.assertTrue(report["sandbox_ttl_verified"])

    def test_network_and_execution_timeout_probes_require_the_expected_outcomes(self):
        network_sandbox = SimpleNamespace(
            run_code=lambda *_args, **_kwargs: SimpleNamespace(
                error=None,
                logs=SimpleNamespace(stdout=["egress-blocked\n"]),
            )
        )

        def raise_timeout(*_args, **_kwargs):
            raise RuntimeError("execution timed out")

        timeout_sandbox = SimpleNamespace(run_code=raise_timeout)
        report: dict[str, object] = {}

        test_isolated_network(network_sandbox, report)
        test_execution_timeout(timeout_sandbox, report)

        self.assertTrue(report["sandbox_isolated_network_verified"])
        self.assertTrue(report["sandbox_execution_timeout_verified"])


def _spec() -> SandboxSpec:
    return SandboxSpec(
        run_id="run-phase0-1",
        company_id="company-a",
        template="touming-p0-code",
        cpu_cores=2,
        memory_mib=2048,
        ttl_seconds=300,
        network_policy="isolated",
        labels={"schemaVersion": "1"},
    )


def _request() -> ExecutionRequest:
    return ExecutionRequest(
        command=("python", "-V"),
        cwd="/tmp/touming/run-phase0-1",
        environment={},
        timeout_seconds=60,
        input_refs=(),
    )
