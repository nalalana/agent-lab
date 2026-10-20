"""Phase 2-C lifecycle, isolation, limits, and Reaper tests."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.test import SimpleTestCase, override_settings
from django.utils import timezone as django_timezone
from e2b.sandbox.commands.command_handle import CommandExitException

from apps.agent_runtime.checks import check_agsx_configuration
from apps.agent_runtime.contracts import ExecutionRequest, SandboxSpec
from apps.agent_runtime.providers.sandbox.artifacts import (
    ArtifactAccessDenied,
    ArtifactLimitExceeded,
)
from apps.agent_runtime.providers.sandbox.concurrency import (
    InMemoryCompanySandboxLimiter,
    SandboxConcurrencyLimitExceeded,
)
from apps.agent_runtime.providers.sandbox.errors import (
    SandboxCreateError,
    SandboxProviderError,
    SandboxTimeoutError,
)
from apps.agent_runtime.providers.sandbox.metrics import (
    NullSandboxMetrics,
    SandboxMetrics,
)
from apps.agent_runtime.providers.sandbox.policy import ResolvedSandboxPolicy
from apps.agent_runtime.providers.sandbox.reaper import SandboxReaper
from apps.agent_runtime.providers.sandbox.repository import (
    STATUS_CREATING,
    STATUS_FAILED,
    STATUS_READY,
    STATUS_RUNNING,
    STATUS_TERMINATED,
    SandboxRecord,
)
from apps.agent_runtime.providers.sandbox.tasks import reap_expired_sandboxes
from apps.agent_runtime.providers.sandbox.tencent_agsx import TencentAgsxSandboxProvider
from config.logging import SandboxMetricJsonFormatter


NOW = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)


class FakeRepository:
    def __init__(self):
        self.records: dict[str, SandboxRecord] = {}
        self.by_run: dict[str, str] = {}

    def reserve(self, *, run_id, policy, expires_at):
        if run_id in self.by_run:
            return self.records[self.by_run[run_id]], False
        record = SandboxRecord(
            id=str(uuid4()),
            run_id=str(run_id),
            company_id=policy.company_id,
            provider="tencent_agsx",
            external_id="",
            status=STATUS_CREATING,
            template=policy.template,
            cpu_cores=policy.cpu_cores,
            memory_mib=policy.memory_mib,
            network_policy=policy.network_policy,
            expires_at=expires_at,
            created_at=NOW,
        )
        self.records[record.id] = record
        self.by_run[record.run_id] = record.id
        return record, True

    def get(self, record_id):
        return self.records[str(record_id)]

    def mark_provider_instance(self, record_id, *, external_id):
        self.records[record_id] = replace(
            self.records[record_id],
            external_id=str(external_id),
        )
        return self.records[record_id]

    def mark_ready(self, record_id, *, external_id, ready_at, expires_at):
        self.records[record_id] = replace(
            self.records[record_id],
            external_id=external_id,
            status=STATUS_READY,
            ready_at=ready_at,
            expires_at=expires_at,
        )
        return self.records[record_id]

    def mark_running(self, record_id):
        self.records[record_id] = replace(self.records[record_id], status=STATUS_RUNNING)

    def mark_ready_after_execute(self, record_id):
        self.records[record_id] = replace(self.records[record_id], status=STATUS_READY)

    def mark_failed(self, record_id, error_code):
        self.records[record_id] = replace(
            self.records[record_id], status=STATUS_FAILED, error_code=str(error_code)
        )

    def mark_terminated(self, record_id, *, terminated_at, billed_seconds):
        self.records[record_id] = replace(
            self.records[record_id],
            status=STATUS_TERMINATED,
            terminated_at=terminated_at,
            billed_seconds=billed_seconds,
            error_code="",
        )

    def find_reapable(self, *, now, limit):
        return tuple(
            record
            for record in self.records.values()
            if record.status in {STATUS_READY, STATUS_RUNNING}
            or (record.status == STATUS_FAILED and record.external_id)
        )[:limit]


class FakePolicyResolver:
    def resolve(self, spec):
        return ResolvedSandboxPolicy(
            company_id=spec.company_id,
            template="touming-code",
            cpu_cores=2,
            memory_mib=2048,
            ttl_seconds=300,
            network_policy="isolated",
            max_concurrent_runs=1,
        )


class FakeArtifactStore:
    def __init__(self):
        self.put_calls = []

    def get(self, ref, *, company_id, run_id):
        if ref != f"input:{company_id}:{run_id}":
            raise ArtifactAccessDenied("wrong tenant")
        return SimpleNamespace(name="input.txt", content=b"input", ref=ref)

    def put(self, **kwargs):
        self.put_calls.append(kwargs)
        return f"artifact:{kwargs['kind']}:{kwargs['name']}"


class FakeFiles:
    def __init__(self):
        self.content = {}
        self.output_entries = []

    def write(self, path, content):
        self.content[path] = bytes(content) if not isinstance(content, str) else content.encode()

    def list(self, path, depth=1):
        raise AssertionError("AGSX files.list must not be used for output collection")

    def read(self, path, format="text"):
        value = self.content[path]
        return bytearray(value) if format == "bytes" else value.decode()


class FakeCommands:
    def __init__(self, files):
        self.files = files
        self.calls = []
        self.user_calls = []
        self.result = SimpleNamespace(exit_code=0, stdout="ok", stderr="")
        self.manifest_result = None
        self.prepare_error = None

    def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command.startswith("umask 077 && mkdir "):
            if self.prepare_error is not None:
                raise self.prepare_error
            return SimpleNamespace(exit_code=0, stdout="", stderr="")
        if TencentAgsxSandboxProvider.output_manifest_marker in command:
            if self.manifest_result is not None:
                return self.manifest_result
            output_dir = f"{kwargs['cwd'].rstrip('/')}/"
            entries = []
            for entry in self.files.output_entries:
                entry_path = str(entry.path)
                relative_path = (
                    entry_path[len(output_dir):]
                    if entry_path.startswith(output_dir)
                    else entry_path
                )
                raw_type = getattr(entry, "type", "")
                entry_type = str(getattr(raw_type, "value", raw_type)).lower()
                if getattr(entry, "symlink_target", None):
                    entry_type = "symlink"
                entries.append({
                    "path": relative_path,
                    "type": entry_type,
                    "size": int(getattr(entry, "size", 0)),
                })
            return SimpleNamespace(
                exit_code=0,
                stdout=json.dumps({
                    "version": 1,
                    "entries": entries,
                    "limit_exceeded": False,
                }),
                stderr="",
            )
        self.user_calls.append((command, kwargs))
        return self.result


class FakeSandbox:
    def __init__(
        self,
        sandbox_id="agsx-1",
        *,
        info=None,
        get_info_error=None,
        kill_error=None,
    ):
        self.sandbox_id = sandbox_id
        self.files = FakeFiles()
        self.commands = FakeCommands(self.files)
        self.kill_calls = 0
        self.info = info
        self.get_info_error = get_info_error
        self.kill_error = kill_error

    def get_info(self):
        if self.get_info_error is not None:
            raise self.get_info_error
        if self.info is not None:
            return self.info
        # The current cloud Tool reports 1000 MiB. Enforcement is intentionally
        # disabled during this task, while the mismatch remains observable.
        return SimpleNamespace(
            cpu_count=2,
            memory_mb=1000,
            allow_internet_access=False,
            end_at=NOW + timedelta(seconds=300),
        )

    def is_running(self):
        return self.kill_calls == 0

    def kill(self):
        self.kill_calls += 1
        if self.kill_error is not None:
            raise self.kill_error
        return self.kill_calls == 1


class FakeSandboxClass:
    create_calls = []
    instances = {}
    info = None
    get_info_error = None
    kill_error = None

    @classmethod
    def reset(cls):
        cls.create_calls = []
        cls.instances = {}
        cls.info = None
        cls.get_info_error = None
        cls.kill_error = None

    @classmethod
    def create(cls, **kwargs):
        cls.create_calls.append(kwargs)
        sandbox = FakeSandbox(
            f"agsx-{len(cls.create_calls)}",
            info=cls.info,
            get_info_error=cls.get_info_error,
            kill_error=cls.kill_error,
        )
        cls.instances[sandbox.sandbox_id] = sandbox
        return sandbox

    @classmethod
    def connect(cls, external_id, **kwargs):
        if external_id not in cls.instances:
            raise RuntimeError("not found")
        return cls.instances[external_id]


class RecordingMetrics(NullSandboxMetrics):
    def __init__(self):
        self.events = []

    def observe(self, name, value, labels=None):
        self.events.append((str(name), float(value), dict(labels or {})))


@override_settings(
    AGSX_ENABLED=True,
    AGSX_ENFORCE_EFFECTIVE_RESOURCES=False,
    AGSX_REQUEST_TIMEOUT_SECONDS=5,
    AGSX_SANDBOX_CLEANUP_GRACE_SECONDS=10,
    AGENT_RUNTIME_SANDBOX_STDOUT_LIMIT_BYTES=4,
    AGENT_RUNTIME_SANDBOX_STDERR_LIMIT_BYTES=4,
    AGENT_RUNTIME_SANDBOX_MAX_ARTIFACT_BYTES=1024,
    AGENT_RUNTIME_SANDBOX_MAX_TOTAL_ARTIFACT_BYTES=2048,
    AGENT_RUNTIME_SANDBOX_MAX_OVERFLOW_BYTES=1024,
    AGENT_RUNTIME_SANDBOX_MAX_ARTIFACT_COUNT=5,
)
class TencentAgsxProviderTests(SimpleTestCase):
    def setUp(self):
        FakeSandboxClass.reset()
        self.repository = FakeRepository()
        self.limiter = InMemoryCompanySandboxLimiter()
        self.artifacts = FakeArtifactStore()
        self.metrics = RecordingMetrics()
        self.provider = TencentAgsxSandboxProvider(
            repository=self.repository,
            policy_resolver=FakePolicyResolver(),
            limiter=self.limiter,
            artifact_store=self.artifacts,
            sandbox_class=FakeSandboxClass,
            metrics=self.metrics,
            now=lambda: NOW,
        )
        self.run_id = str(uuid4())

    def test_default_provider_and_reaper_clocks_follow_django_timezone_mode(self):
        provider = TencentAgsxSandboxProvider(
            repository=self.repository,
            policy_resolver=FakePolicyResolver(),
            limiter=self.limiter,
            artifact_store=self.artifacts,
            sandbox_class=FakeSandboxClass,
            metrics=self.metrics,
        )
        reaper = SandboxReaper(
            provider,
            self.repository,
            metrics=self.metrics,
        )

        self.assertEqual(
            django_timezone.is_aware(provider.now()),
            bool(settings.USE_TZ),
        )
        self.assertEqual(
            django_timezone.is_aware(reaper.now()),
            bool(settings.USE_TZ),
        )

    def test_create_is_isolated_and_idempotent_for_same_run(self):
        async def exercise():
            first = await self.provider.create(self._spec())
            second = await self.provider.create(self._spec())
            return first, second

        first, second = asyncio.run(exercise())

        self.assertEqual(first.sandbox_id, second.sandbox_id)
        self.assertEqual(len(FakeSandboxClass.create_calls), 1)
        options = FakeSandboxClass.create_calls[0]
        self.assertFalse(options["allow_internet_access"])
        self.assertEqual(options["envs"], {})
        self.assertNotIn("E2B_API_KEY", str(options))
        self.assertEqual(self.limiter.active_count("company-a"), 1)

    def test_create_accepts_missing_network_flag_for_isolated_tool_policy(self):
        FakeSandboxClass.info = self._sandbox_info(allow_internet_access=None)

        handle = asyncio.run(self.provider.create(self._spec()))

        sandbox = FakeSandboxClass.instances[handle.external_id]
        self.assertEqual(self.repository.get(handle.sandbox_id).status, STATUS_READY)
        self.assertEqual(sandbox.commands.calls, [])
        self.assertIn(
            (
                "sandbox.network_metadata_unavailable.total",
                1.0,
                {"provider": "tencent_agsx"},
            ),
            self.metrics.events,
        )

    def test_create_rejects_explicitly_enabled_public_egress(self):
        FakeSandboxClass.info = self._sandbox_info(allow_internet_access=True)

        with self.assertRaises(SandboxCreateError):
            asyncio.run(self.provider.create(self._spec()))

        sandbox = FakeSandboxClass.instances["agsx-1"]
        self.assertEqual(sandbox.kill_calls, 1)
        self.assertEqual(sandbox.commands.calls, [])
        record = next(iter(self.repository.records.values()))
        self.assertEqual(record.status, STATUS_FAILED)

    def test_missing_network_flag_requires_isolated_runtime_policy(self):
        info = self._sandbox_info(allow_internet_access=None)
        policy = replace(
            FakePolicyResolver().resolve(self._spec()),
            network_policy="public",
        )

        with self.assertRaises(SandboxCreateError):
            self.provider._validate_network_isolation(info, policy)

    def test_create_rejects_unknown_network_metadata(self):
        FakeSandboxClass.info = self._sandbox_info(
            allow_internet_access="unknown"
        )

        with self.assertRaises(SandboxCreateError):
            asyncio.run(self.provider.create(self._spec()))

        self.assertEqual(FakeSandboxClass.instances["agsx-1"].kill_calls, 1)
        record = next(iter(self.repository.records.values()))
        self.assertEqual(record.status, STATUS_FAILED)

    def test_company_quota_rejection_is_observable_without_cloud_create(self):
        self.limiter.acquire(
            "company-a",
            "occupied-run",
            limit=1,
            ttl_seconds=60,
        )

        with self.assertRaises(SandboxConcurrencyLimitExceeded):
            asyncio.run(self.provider.create(self._spec()))

        self.assertEqual(FakeSandboxClass.create_calls, [])
        self.assertIn(
            (
                "sandbox.quota.rejected.total",
                1.0,
                {
                    "provider": "tencent_agsx",
                    "template": "touming-code",
                    "environment": str(getattr(settings, "ENVIRONMENT", "unknown")),
                    "outcome": "rejected",
                    "quota_scope": "company",
                },
            ),
            self.metrics.events,
        )

    def test_failed_post_create_cleanup_keeps_external_id_for_reaper(self):
        FakeSandboxClass.get_info_error = RuntimeError("control plane unavailable")
        FakeSandboxClass.kill_error = RuntimeError("cleanup unavailable")

        with self.assertRaises(SandboxCreateError):
            asyncio.run(self.provider.create(self._spec()))

        record = next(iter(self.repository.records.values()))
        self.assertEqual(record.status, STATUS_FAILED)
        self.assertEqual(record.external_id, "agsx-1")
        self.assertEqual(
            tuple(item.id for item in self.repository.find_reapable(now=NOW, limit=10)),
            (record.id,),
        )

    def test_second_worker_does_not_release_in_progress_run_lease(self):
        policy = FakePolicyResolver().resolve(self._spec())
        self.repository.reserve(
            run_id=self.run_id,
            policy=policy,
            expires_at=NOW + timedelta(seconds=300),
        )

        with self.assertRaises(SandboxProviderError):
            asyncio.run(self.provider.create(self._spec()))

        self.assertEqual(self.limiter.active_count("company-a"), 1)

    def test_create_maps_work_output_to_the_protected_run_output_directory(self):
        async def exercise():
            handle = await self.provider.create(self._spec())
            await self.provider.execute(
                handle.sandbox_id,
                ExecutionRequest(
                    command=("python", "-c", "print('ok')"),
                    cwd=f"/tmp/touming-runs/{self.run_id}/work",
                    environment={},
                    timeout_seconds=60,
                    input_refs=(),
                ),
            )
            return handle

        handle = asyncio.run(exercise())
        sandbox = FakeSandboxClass.instances[handle.external_id]
        prepare_commands = [
            command
            for command, _kwargs in sandbox.commands.calls
            if command.startswith("umask 077 && mkdir ")
        ]

        self.assertEqual(len(prepare_commands), 1)
        self.assertIn(
            f"ln -sfn ../output /tmp/touming-runs/{self.run_id}/work/output",
            prepare_commands[0],
        )

    def test_execute_uploads_input_truncates_logs_and_collects_output(self):
        async def exercise():
            handle = await self.provider.create(self._spec())
            sandbox = FakeSandboxClass.instances[handle.external_id]
            output_path = f"/tmp/touming-runs/{self.run_id}/output/result.txt"
            sandbox.files.content[output_path] = b"result"
            sandbox.files.output_entries = [SimpleNamespace(
                type=SimpleNamespace(value="file"),
                symlink_target=None,
                path=output_path,
                name="result.txt",
                size=6,
            )]
            sandbox.commands.result = SimpleNamespace(
                exit_code=0,
                stdout="abcdef",
                stderr="",
            )
            result = await self.provider.execute(
                handle.sandbox_id,
                ExecutionRequest(
                    command=("python", "script with spaces.py"),
                    cwd=f"/tmp/touming-runs/{self.run_id}/work",
                    environment={"PYTHONIOENCODING": "utf-8"},
                    timeout_seconds=60,
                    input_refs=(f"input:company-a:{self.run_id}",),
                ),
            )
            return handle, sandbox, result

        handle, sandbox, result = asyncio.run(exercise())

        self.assertEqual(result.stdout, "abcd")
        self.assertTrue(result.truncated)
        self.assertIn("artifact:overflow:stdout.txt", result.artifact_refs)
        self.assertIn("artifact:output:result.txt", result.artifact_refs)
        command = sandbox.commands.user_calls[-1][0]
        self.assertEqual(command, "python 'script with spaces.py'")
        self.assertEqual(self.repository.get(handle.sandbox_id).status, STATUS_READY)

    def test_execute_logs_failure_stage_without_provider_error_detail(self):
        async def exercise():
            handle = await self.provider.create(self._spec())
            sandbox = FakeSandboxClass.instances[handle.external_id]
            sandbox.commands.manifest_result = SimpleNamespace(
                exit_code=1,
                stdout="",
                stderr="credential=must-not-enter-logs",
            )
            with patch(
                "apps.agent_runtime.providers.sandbox.tencent_agsx.logger.warning"
            ) as warning:
                with self.assertRaises(SandboxProviderError):
                    await self.provider.execute(
                        handle.sandbox_id,
                        ExecutionRequest(
                            command=("python", "-V"),
                            cwd=".",
                            environment={},
                            timeout_seconds=60,
                            input_refs=(),
                        ),
                    )
            return handle, warning

        handle, warning = asyncio.run(exercise())

        self.assertEqual(self.repository.get(handle.sandbox_id).status, STATUS_FAILED)
        warning.assert_called_once()
        log_arguments = warning.call_args.args
        self.assertEqual(
            log_arguments[-3:],
            ("collect_output_artifacts", "SandboxProviderError", None),
        )
        self.assertNotIn("must-not-enter-logs", " ".join(map(str, log_arguments)))

    def test_execute_logs_prepare_directory_exit_code_without_stderr(self):
        async def exercise():
            handle = await self.provider.create(self._spec())
            sandbox = FakeSandboxClass.instances[handle.external_id]
            sandbox.commands.prepare_error = CommandExitException(
                stderr="permission denied: must-not-enter-logs",
                stdout="",
                exit_code=1,
                error=None,
            )
            with patch(
                "apps.agent_runtime.providers.sandbox.tencent_agsx.logger.warning"
            ) as warning:
                with self.assertRaises(SandboxProviderError):
                    await self.provider.execute(
                        handle.sandbox_id,
                        ExecutionRequest(
                            command=("python", "-V"),
                            cwd=".",
                            environment={},
                            timeout_seconds=60,
                            input_refs=(),
                        ),
                    )
            return handle, sandbox, warning

        handle, sandbox, warning = asyncio.run(exercise())

        self.assertEqual(self.repository.get(handle.sandbox_id).status, STATUS_FAILED)
        prepare_command = sandbox.commands.calls[-1][0]
        self.assertTrue(prepare_command.startswith("umask 077 && mkdir -p "))
        self.assertIn(f"/tmp/touming-runs/{self.run_id}/work", prepare_command)
        warning.assert_called_once()
        log_arguments = warning.call_args.args
        self.assertEqual(
            log_arguments[-3:],
            ("prepare_directories", "CommandExitException", 1),
        )
        self.assertNotIn("must-not-enter-logs", " ".join(map(str, log_arguments)))

    def test_execute_and_terminate_accept_naive_database_timestamps(self):
        async def exercise():
            handle = await self.provider.create(self._spec())
            record = self.repository.get(handle.sandbox_id)
            current_timezone = django_timezone.get_current_timezone()

            def database_value(value):
                if value is None or django_timezone.is_naive(value):
                    return value
                return django_timezone.make_naive(value, current_timezone)

            self.repository.records[handle.sandbox_id] = replace(
                record,
                created_at=database_value(record.created_at),
                expires_at=database_value(record.expires_at),
                ready_at=database_value(record.ready_at),
            )
            sandbox = FakeSandboxClass.instances[handle.external_id]
            sandbox.commands.result = SimpleNamespace(
                exit_code=0,
                stdout="root",
                stderr="",
            )
            result = await self.provider.execute(
                handle.sandbox_id,
                ExecutionRequest(
                    command=("python", "-c", "import getpass; print(getpass.getuser())"),
                    cwd=".",
                    environment={},
                    timeout_seconds=60,
                    input_refs=(),
                ),
            )
            await self.provider.terminate(handle.sandbox_id)
            return handle, sandbox, result

        handle, sandbox, result = asyncio.run(exercise())

        self.assertEqual(result.stdout, "root")
        self.assertEqual(
            sandbox.commands.user_calls[-1][1]["cwd"],
            f"/tmp/touming-runs/{self.run_id}/work",
        )
        record = self.repository.get(handle.sandbox_id)
        self.assertEqual(record.status, STATUS_TERMINATED)
        self.assertIsNotNone(record.terminated_at)
        self.assertEqual(sandbox.kill_calls, 1)

    def test_environment_rejects_backend_secret_names_before_execution(self):
        async def exercise():
            handle = await self.provider.create(self._spec())
            await self.provider.execute(
                handle.sandbox_id,
                ExecutionRequest(
                    command=("python", "-V"),
                    cwd=f"/tmp/touming-runs/{self.run_id}/work",
                    environment={"E2B_API_KEY": "must-not-enter"},
                    timeout_seconds=60,
                    input_refs=(),
                ),
            )

        with self.assertRaises(SandboxProviderError):
            asyncio.run(exercise())

    def test_terminate_is_idempotent_and_releases_company_lease(self):
        async def exercise():
            handle = await self.provider.create(self._spec())
            await self.provider.terminate(handle.sandbox_id)
            await self.provider.terminate(handle.sandbox_id)
            return handle

        handle = asyncio.run(exercise())

        record = self.repository.get(handle.sandbox_id)
        self.assertEqual(record.status, STATUS_TERMINATED)
        self.assertEqual(self.limiter.active_count("company-a"), 0)
        self.assertEqual(FakeSandboxClass.instances[handle.external_id].kill_calls, 1)

    def test_status_reconciles_stopped_cloud_instance_and_releases_lease(self):
        async def exercise():
            handle = await self.provider.create(self._spec())
            FakeSandboxClass.instances[handle.external_id].kill_calls = 1
            status = await self.provider.get_status(handle.sandbox_id)
            return handle, status

        handle, status = asyncio.run(exercise())

        self.assertFalse(status["running"])
        self.assertEqual(status["status"], STATUS_TERMINATED)
        self.assertEqual(self.repository.get(handle.sandbox_id).status, STATUS_TERMINATED)
        self.assertEqual(self.limiter.active_count("company-a"), 0)
        self.assertIn("sandbox.usage.seconds", [event[0] for event in self.metrics.events])

    def test_status_reconciles_missing_cloud_instance_after_worker_restart(self):
        async def exercise():
            handle = await self.provider.create(self._spec())
            self.provider._connected.clear()
            FakeSandboxClass.instances.pop(handle.external_id)
            status = await self.provider.get_status(handle.sandbox_id)
            return handle, status

        handle, status = asyncio.run(exercise())

        self.assertFalse(status["running"])
        self.assertEqual(status["status"], STATUS_TERMINATED)
        self.assertEqual(self.repository.get(handle.sandbox_id).status, STATUS_TERMINATED)
        self.assertEqual(self.limiter.active_count("company-a"), 0)

    def test_upload_and_download_reject_expired_sandbox(self):
        handle = asyncio.run(self.provider.create(self._spec()))
        self.repository.records[handle.sandbox_id] = replace(
            self.repository.get(handle.sandbox_id),
            expires_at=NOW - timedelta(seconds=1),
        )

        with self.assertRaises(SandboxTimeoutError):
            asyncio.run(
                self.provider.upload(
                    handle.sandbox_id,
                    (f"input:company-a:{self.run_id}",),
                )
            )
        with self.assertRaises(SandboxTimeoutError):
            asyncio.run(self.provider.download(handle.sandbox_id))

    @override_settings(AGENT_RUNTIME_SANDBOX_MAX_TOTAL_ARTIFACT_BYTES=5)
    def test_upload_validates_entire_input_set_before_writing(self):
        handle = asyncio.run(self.provider.create(self._spec()))
        sandbox = FakeSandboxClass.instances[handle.external_id]
        input_ref = f"input:company-a:{self.run_id}"

        with self.assertRaises(ArtifactLimitExceeded):
            asyncio.run(self.provider.upload(handle.sandbox_id, (input_ref, input_ref)))

        self.assertEqual(sandbox.files.content, {})

    @override_settings(AGENT_RUNTIME_SANDBOX_MAX_ARTIFACT_COUNT=1)
    def test_upload_rejects_too_many_input_artifacts(self):
        handle = asyncio.run(self.provider.create(self._spec()))
        sandbox = FakeSandboxClass.instances[handle.external_id]
        input_ref = f"input:company-a:{self.run_id}"

        with self.assertRaises(ArtifactLimitExceeded):
            asyncio.run(self.provider.upload(handle.sandbox_id, (input_ref, input_ref)))

        self.assertEqual(sandbox.files.content, {})

    def test_upload_download_and_status_use_safe_run_scoped_views(self):
        async def exercise():
            handle = await self.provider.create(self._spec())
            uploaded = await self.provider.upload(
                handle.sandbox_id,
                (f"input:company-a:{self.run_id}",),
            )
            sandbox = FakeSandboxClass.instances[handle.external_id]
            output_path = f"/tmp/touming-runs/{self.run_id}/output/report.txt"
            sandbox.files.content[output_path] = b"report"
            sandbox.files.output_entries = [SimpleNamespace(
                type=SimpleNamespace(value="file"),
                symlink_target=None,
                path=output_path,
                name="report.txt",
                size=6,
            )]
            downloaded = await self.provider.download(handle.sandbox_id)
            status = await self.provider.get_status(handle.sandbox_id)
            return handle, uploaded, downloaded, status

        handle, uploaded, downloaded, status = asyncio.run(exercise())

        self.assertEqual(
            uploaded,
            (f"/tmp/touming-runs/{self.run_id}/input/000-input.txt",),
        )
        self.assertEqual(downloaded, ("artifact:output:report.txt",))
        self.assertEqual(status["sandbox_execution_id"], handle.sandbox_id)
        self.assertTrue(status["running"])
        self.assertNotIn("external_id", status)

    @override_settings(AGENT_RUNTIME_SANDBOX_MAX_TOTAL_ARTIFACT_BYTES=5)
    def test_download_checks_actual_total_before_uploading_any_output(self):
        async def exercise():
            handle = await self.provider.create(self._spec())
            sandbox = FakeSandboxClass.instances[handle.external_id]
            output_path = f"/tmp/touming-runs/{self.run_id}/output/result.txt"
            sandbox.files.content[output_path] = b"123456"
            sandbox.files.output_entries = [SimpleNamespace(
                type=SimpleNamespace(value="file"),
                symlink_target=None,
                path=output_path,
                name="result.txt",
                size=1,
            )]
            await self.provider.download(handle.sandbox_id)

        with self.assertRaises(ArtifactLimitExceeded):
            asyncio.run(exercise())
        self.assertEqual(self.artifacts.put_calls, [])

    def test_download_rejects_parent_traversal_in_listed_output_path(self):
        async def exercise():
            handle = await self.provider.create(self._spec())
            sandbox = FakeSandboxClass.instances[handle.external_id]
            output_dir = f"/tmp/touming-runs/{self.run_id}/output"
            sandbox.files.output_entries = [SimpleNamespace(
                type=SimpleNamespace(value="file"),
                symlink_target=None,
                path=f"{output_dir}/../secret.txt",
                name="secret.txt",
                size=1,
            )]
            await self.provider.download(handle.sandbox_id)

        with self.assertRaises(SandboxProviderError):
            asyncio.run(exercise())
        self.assertEqual(self.artifacts.put_calls, [])

    def test_lifecycle_emits_create_execute_and_usage_metrics(self):
        async def exercise():
            handle = await self.provider.create(self._spec())
            await self.provider.execute(
                handle.sandbox_id,
                ExecutionRequest(
                    command=("python", "-V"),
                    cwd=f"/tmp/touming-runs/{self.run_id}/work",
                    environment={},
                    timeout_seconds=60,
                    input_refs=(),
                ),
            )
            await self.provider.terminate(handle.sandbox_id)

        asyncio.run(exercise())

        names = [event[0] for event in self.metrics.events]
        self.assertIn("sandbox.create.duration_ms", names)
        self.assertIn("sandbox.execute.duration_ms", names)
        self.assertIn("sandbox.usage.seconds", names)
        self.assertIn("sandbox.terminate.duration_ms", names)

    @staticmethod
    def _sandbox_info(*, allow_internet_access):
        return SimpleNamespace(
            cpu_count=2,
            memory_mb=1000,
            allow_internet_access=allow_internet_access,
            end_at=NOW + timedelta(seconds=300),
        )

    def _spec(self):
        return SandboxSpec(
            run_id=self.run_id,
            company_id="company-a",
            template="untrusted-template",
            cpu_cores=99,
            memory_mib=99999,
            ttl_seconds=600,
            network_policy="public",
            labels={"schemaVersion": "1", "secret": "ignored"},
        )


class SandboxConcurrencyTests(SimpleTestCase):
    def test_company_limits_are_independent_and_same_run_is_idempotent(self):
        limiter = InMemoryCompanySandboxLimiter()
        limiter.acquire("a", "run-1", limit=1, ttl_seconds=60)
        limiter.acquire("a", "run-1", limit=1, ttl_seconds=60)
        with self.assertRaises(SandboxConcurrencyLimitExceeded):
            limiter.acquire("a", "run-2", limit=1, ttl_seconds=60)
        limiter.acquire("b", "run-2", limit=1, ttl_seconds=60)

        self.assertEqual(limiter.active_count("a"), 1)
        self.assertEqual(limiter.active_count("b"), 1)

    def test_global_limit_is_shared_and_release_restores_capacity(self):
        limiter = InMemoryCompanySandboxLimiter(global_limit=2)
        limiter.acquire("a", "run-1", limit=2, ttl_seconds=60)
        limiter.acquire("b", "run-2", limit=2, ttl_seconds=60)

        with self.assertRaisesRegex(
            SandboxConcurrencyLimitExceeded,
            "global sandbox concurrency limit exceeded",
        ):
            limiter.acquire("c", "run-3", limit=2, ttl_seconds=60)

        limiter.release("a", "run-1")
        limiter.acquire("c", "run-3", limit=2, ttl_seconds=60)

        self.assertEqual(limiter.global_active_count(), 2)
        self.assertEqual(limiter.active_count("a"), 0)
        self.assertEqual(limiter.active_count("c"), 1)

    def test_same_run_identifier_in_different_companies_uses_distinct_global_slots(self):
        limiter = InMemoryCompanySandboxLimiter(global_limit=2)

        limiter.acquire("a", "same-run", limit=1, ttl_seconds=60)
        limiter.acquire("b", "same-run", limit=1, ttl_seconds=60)

        self.assertEqual(limiter.global_active_count(), 2)


class SandboxReaperTests(SimpleTestCase):
    def test_reaper_continues_after_one_cleanup_failure(self):
        records = [
            SimpleNamespace(id="ok", provider="tencent_agsx", run_id="r1"),
            SimpleNamespace(id="bad", provider="tencent_agsx", run_id="r2"),
        ]
        repository = SimpleNamespace(find_reapable=lambda **kwargs: records)

        class Provider:
            async def terminate(self, record_id):
                if record_id == "bad":
                    raise RuntimeError("provider down")

        result = asyncio.run(
            SandboxReaper(
                Provider(), repository, metrics=NullSandboxMetrics(), now=lambda: NOW
            ).run(limit=10)
        )

        self.assertEqual(result.scanned, 2)
        self.assertEqual(result.terminated, 1)
        self.assertEqual(result.failed, 1)


class SandboxMetricsTests(SimpleTestCase):
    def test_json_formatter_preserves_values_and_filters_high_cardinality_labels(self):
        with patch("apps.agent_runtime.providers.sandbox.metrics.logger.info") as info:
            SandboxMetrics().observe(
                "sandbox.execute.duration_ms",
                12.5,
                {"provider": "tencent_agsx", "outcome": "success", "run_id": "secret-run"},
            )

        extra = info.call_args.kwargs["extra"]
        record = logging.LogRecord(
            "agent_runtime.sandbox.metrics",
            logging.INFO,
            __file__,
            1,
            "sandbox_metric",
            (),
            None,
        )
        for key, value in extra.items():
            setattr(record, key, value)
        payload = json.loads(SandboxMetricJsonFormatter().format(record))

        self.assertEqual(payload["event"], "sandbox_metric")
        self.assertEqual(payload["metric_name"], "sandbox.execute.duration_ms")
        self.assertEqual(payload["metric_value"], 12.5)
        self.assertEqual(
            payload["metric_labels"],
            {"provider": "tencent_agsx", "outcome": "success"},
        )

    def test_django_logging_routes_metrics_to_jsonl_file(self):
        logger_config = settings.LOGGING["loggers"]["agent_runtime.sandbox.metrics"]
        handler = settings.LOGGING["handlers"]["sandbox_metrics_file"]

        self.assertEqual(logger_config["handlers"], ["sandbox_metrics_file"])
        self.assertFalse(logger_config["propagate"])
        self.assertEqual(handler["formatter"], "sandbox_metric_json")
        self.assertTrue(str(handler["filename"]).endswith("sandbox_metrics.jsonl"))


class AgsxSystemCheckTests(SimpleTestCase):
    def test_celery_beat_schedules_memory_retry_and_sandbox_reaper(self):
        schedule = settings.CELERY_BEAT_SCHEDULE

        self.assertEqual(schedule["agent-runtime-memory-retry"]["schedule"], 300.0)
        self.assertEqual(schedule["agent-runtime-sandbox-reaper"]["schedule"], 60.0)

    @override_settings(AGSX_ENABLED=False)
    def test_reaper_task_is_side_effect_free_when_agsx_is_disabled(self):
        self.assertEqual(
            reap_expired_sandboxes.run(),
            {"status": "disabled", "scanned": 0, "terminated": 0, "failed": 0},
        )

    @override_settings(
        AGSX_ENABLED=True,
        E2B_DOMAIN="https://invalid.example/path",
        E2B_API_KEY="",
        AGSX_TEMPLATE="",
        AGSX_NETWORK_POLICY="public",
        AGENT_RUNTIME_ARTIFACT_COS_BUCKET="",
        AGENT_RUNTIME_ARTIFACT_COS_REGION="",
        AGENT_RUNTIME_ARTIFACT_COS_SECRET_ID="",
        AGENT_RUNTIME_ARTIFACT_COS_SECRET_KEY="",
    )
    def test_enabled_configuration_fails_closed_without_credentials(self):
        errors = check_agsx_configuration(None)
        ids = {error.id for error in errors}

        self.assertTrue(
            {"agent_runtime.E001", "agent_runtime.E002", "agent_runtime.E003",
             "agent_runtime.E004", "agent_runtime.E006", "agent_runtime.E009"}.issubset(ids)
        )
        self.assertNotIn("agent_runtime.E005", ids)

    @override_settings(
        AGSX_ENABLED=True,
        AGSX_GLOBAL_MAX_CONCURRENT_SANDBOXES=0,
        AGSX_FAIL_CLOSED_ON_QUOTA_BACKEND_ERROR=False,
        AGSX_ESTIMATED_COST_MICRO_UNITS_PER_SECOND_2C2G=1,
    )
    def test_enabled_configuration_rejects_unsafe_quota_controls(self):
        ids = {error.id for error in check_agsx_configuration(None)}

        self.assertIn("agent_runtime.E007", ids)
        self.assertIn("agent_runtime.E008", ids)

    @override_settings(AGSX_ENABLED=True)
    def test_enabled_configuration_fails_closed_when_schema_is_unavailable(self):
        with patch("apps.agent_runtime.checks.apps.get_model", side_effect=LookupError):
            errors = check_agsx_configuration(None)

        self.assertIn("agent_runtime.E005", {error.id for error in errors})
