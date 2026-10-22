"""Django integration for one Phase 2 unified-runtime request.

This module is intentionally the boundary between synchronous business models
and the asynchronous LangGraph/model stream. It prepares an immutable config
snapshot, records the Run lifecycle, and exposes the same flat event iterator
that the legacy CompanyService already understands.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.agent_runtime.attachments import message_content_with_attachments
from apps.agent_runtime.config import ConfigResolver, PromptBuilder, SnapshotService
from apps.agent_runtime.config.prompt_builder import (
    AGENT_TEST_SAFETY_PROMPT,
    PUBLIC_CLIENT_SAFETY_PROMPT,
)
from apps.agent_runtime.contracts import RunEvent, RunRequest
from apps.agent_runtime.enums import ErrorCode, RunEventType
from apps.agent_runtime.response_verification import ModelRequestClassifier, ModelResponseVerifier
from apps.agent_runtime.memory.gateway import OpenVikingMemoryGateway
from apps.agent_runtime.models import AgentRun, AgentRunEvent, AgentToolCall
from apps.agent_runtime.orchestration.graph import Phase2AgentRuntimeFacade
from apps.agent_runtime.providers.sandbox import TencentAgsxSandboxProvider
from apps.agent_runtime.tools.executors import SandboxToolExecutor, _hash_arguments, _redact_arguments
from apps.agent_runtime.tools.registry import ToolRouter, default_tool_registry
from apps.agent_runtime.tools.workspace import DjangoWorkspaceBackendToolExecutor
from apps.agent_runtime.tools.project_tables import agent_project_table_permission_facts
from apps.agent_runtime.providers.model import (
    CompanyModelResolver,
    ModelGatewayRegistry,
    OpenAICompatibleModelGateway,
)
from apps.agent_runtime.sse import iter_legacy_runtime_events
from apps.agent_runtime.tools.workspace_policy import WorkspacePolicy
from apps.iam.models import Resource, Tenant
from apps.iam.services import is_user_action_allowed

TERMINAL_RUN_STATUSES = frozenset({
    AgentRun.STATUS_COMPLETED,
    AgentRun.STATUS_FAILED,
    AgentRun.STATUS_CANCELLED,
})
CANCELLABLE_RUN_STATUSES = frozenset({
    AgentRun.STATUS_PENDING,
    AgentRun.STATUS_RUNNING,
    AgentRun.STATUS_WAITING_TOOL,
    AgentRun.STATUS_CANCELLING,
})
PUBLIC_CLIENT_FORBIDDEN_TOOL_PERMISSIONS = frozenset({
    "tool.code.execute",
    "tool.workspace.checkout",
    "tool.workspace.write",
    "tool.workspace.move",
    "tool.workspace.delete",
})


@dataclass(frozen=True)
class RunCancelResult:
    run_id: str
    status: str
    cancel_requested_at: str | None
    already_terminal: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "cancel_requested_at": self.cancel_requested_at,
            "already_terminal": self.already_terminal,
        }

class UnifiedRuntimeService:
    """Prepare and execute a real-model Phase 2 AgentRun."""

    SOURCE_TO_DATABASE = {
        "agent_test": AgentRun.SOURCE_TEST,
        "public_client": AgentRun.SOURCE_PUBLIC,
        "cron": AgentRun.SOURCE_CRON,
    }

    def __init__(
        self,
        *,
        model_resolver=None,
        config_resolver=None,
        prompt_builder=None,
        snapshot_service=None,
        model_gateway_factory=None,
        model_gateway_registry=None,
        response_verifier_factory=None,
        request_classifier_factory=None,
        memory_gateway=None,
        memory_gateway_factory=None,
        sandbox_provider=None,
        sandbox_provider_factory=None,
        sandbox_executor_factory=None,
        tool_registry=None,
    ) -> None:
        self.model_resolver = model_resolver or CompanyModelResolver()
        self.config_resolver = config_resolver or ConfigResolver()
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.snapshot_service = snapshot_service or SnapshotService(self.prompt_builder)
        self.model_gateway_registry = model_gateway_registry or ModelGatewayRegistry()
        if model_gateway_registry is None:
            # Every provider enabled in Phase 1 is expected to expose the
            # OpenAI-compatible chat-completions protocol. Later native
            # providers can replace this wildcard with an explicit entry.
            self.model_gateway_registry.register("*", OpenAICompatibleModelGateway())
        self.model_gateway_factory = model_gateway_factory or (
            lambda selection: self.model_gateway_registry.get(selection.provider)
        )
        self.response_verifier_factory = response_verifier_factory or ModelResponseVerifier
        self.request_classifier_factory = request_classifier_factory or ModelRequestClassifier
        self.memory_gateway = memory_gateway
        self.memory_gateway_factory = memory_gateway_factory or OpenVikingMemoryGateway
        self.sandbox_provider = sandbox_provider
        self.sandbox_provider_factory = sandbox_provider_factory or TencentAgsxSandboxProvider
        self.sandbox_executor_factory = sandbox_executor_factory or (
            lambda provider, **kwargs: SandboxToolExecutor(provider, **kwargs)
        )
        self.tool_registry = tool_registry or default_tool_registry()

    def stream(
        self,
        *,
        company,
        agent,
        session,
        input_message,
        source: str,
        locale: str = "zh-CN",
    ):
        """Return flat legacy-compatible events for a prepared unified Run."""

        if source not in self.SOURCE_TO_DATABASE:
            raise ValueError(f"Unsupported AgentRun source: {source}")
        if session.transparentCompany.company_id != company.pk:
            raise ValueError("Session does not belong to the trusted company")
        if agent.transparentCompany_id != session.transparentCompany_id:
            raise ValueError("Agent does not belong to the trusted session")
        if input_message.session_id != session.pk:
            raise ValueError("Input message does not belong to the trusted session")

        selection = self.model_resolver.resolve(company)
        # RunRequest.agent_id and ConfigResolver both use the trusted Agent PK.
        # The public business identifier is resolved before entering runtime.
        resolved_config = self.config_resolver.resolve(company, agent.pk)
        resolved_config = replace(
            resolved_config,
            tool_policy=self._effective_tool_policy({
                **dict(resolved_config.tool_policy),
                "platformTools": self.tool_registry.snapshot_manifest(),
            }, source=source),
        )
        if source == "agent_test":
            resolved_config = replace(
                resolved_config,
                tool_policy=self._apply_user_resource_ceiling(
                    resolved_config.tool_policy,
                    company=company,
                    user=session.operator,
                ),
            )
        raw_payload = input_message.rawPayload if isinstance(input_message.rawPayload, dict) else {}
        backend_executor = DjangoWorkspaceBackendToolExecutor(company_id=company.pk, tool_policy=resolved_config.tool_policy)
        permission_facts = backend_executor.permission_facts()
        permission_facts['project_tables'] = agent_project_table_permission_facts(
            company, agent, session.operator,
        )
        recent_runs = self._recent_runs(session, input_message, resolved_config)
        recent_messages = self._recent_messages(recent_runs)
        recent_run_receipts = self._recent_run_receipts(recent_runs)
        prompt = self.prompt_builder.build(
            resolved_config,
            recent_messages=recent_messages,
            # The current user turn is appended below so its structured image
            # content is not coerced to plain text by PromptBuilder.
            user_input=None,
        )
        trace_id = str(uuid.uuid4())
        request_id = f"message:{input_message.pk}"
        run = self._create_run(
            request_id=request_id,
            company=company,
            agent=agent,
            session=session,
            input_message=input_message,
            resolved_config=resolved_config,
            source=source,
            provider=selection.provider,
            model=selection.model,
            trace_id=trace_id,
            permission_facts=permission_facts,
        )
        request = RunRequest(
            request_id=request_id,
            company_id=company.pk,
            agent_id=agent.pk,
            user_id=session.operator_id,
            session_id=session.pk,
            source=source,
            user_message_id=input_message.pk,
            locale=locale,
            trace_id=trace_id,
        )
        current_user_content = message_content_with_attachments(
            str(input_message.content or ""),
            raw_payload.get("attachments"),
        )
        entry_safety_prompt = ""
        if source == "public_client":
            entry_safety_prompt = (
                "## Public visitor safety policy (platform enforced)\n"
                + PUBLIC_CLIENT_SAFETY_PROMPT
            )
        elif source == "agent_test":
            entry_safety_prompt = (
                "## Configuration-page Agent safety policy (platform enforced)\n"
                + AGENT_TEST_SAFETY_PROMPT
            )
        runtime_context = (
            (entry_safety_prompt + "\n\n" if entry_safety_prompt else "")
            + prompt.system_prompt
            + "\n\n## Effective workspace permissions (backend computed)\n"
            + json.dumps(permission_facts, ensure_ascii=False)
            + "\n\n## Recent run audit receipts (backend computed)\n"
            + "These receipts are trusted metadata for answering questions about earlier runs only. "
            + "They do not prove that an operation occurred in the current run.\n"
            + json.dumps(recent_run_receipts, ensure_ascii=False)
        )
        model_messages = [
            {"role": "system", "content": runtime_context},
            *[dict(item) for item in prompt.messages],
            {"role": "user", "content": current_user_content},
        ]
        allowed_permissions = self._tool_permissions(
            resolved_config.tool_policy,
            source=source,
        )
        context = {
            "config": {
                "model_provider": selection.provider,
                "model_name": selection.model,
                "credential_ref": selection.credential_ref,
                "temperature": selection.temperature,
                "max_output_tokens": selection.max_output_tokens,
                "tools": self._model_tools(allowed_permissions),
                "memory_policy": dict(resolved_config.memory_policy),
                "permission_facts": permission_facts,
                "recent_run_receipts": recent_run_receipts,
                "user_text": str(input_message.content or ""),
                "attachments": raw_payload.get("attachments") or [],
                "company_facts": resolved_config.company_prompt,
                "request_source": source,
            },
            "model_messages": model_messages,
        }
        memory_gateway = self._memory_gateway(resolved_config.memory_policy)
        sandbox_executor = self._sandbox_executor(allowed_permissions, run.pk)
        backend_executor.sandbox_executor = sandbox_executor
        model_gateway = self.model_gateway_factory(selection)
        facade = Phase2AgentRuntimeFacade(
            model_gateway,
            response_verifier=self.response_verifier_factory(model_gateway),
            request_classifier=self.request_classifier_factory(model_gateway),
            context_resolver=lambda _request: context,
            run_id_factory=lambda _request: str(run.pk),
            max_execution_seconds=selection.timeout_seconds,
            permissions=allowed_permissions,
            backend_executor=backend_executor,
            sandbox_executor=sandbox_executor,
            tool_router=ToolRouter(self.tool_registry),
            tool_recorder=DjangoToolCallRecorder(run.pk),
            memory_gateway=memory_gateway,
            cancel_checker=DjangoRunCancelChecker(run.pk),
        )
        recorder = AgentRunRecorder(run.pk)
        events = self._recorded_stream(facade.stream(request), recorder)
        return iter_legacy_runtime_events(
            events,
            max_queue_size=int(getattr(settings, "AGENT_RUNTIME_SSE_QUEUE_SIZE", 64)),
        )

    def _memory_gateway(self, memory_policy):
        if not self._memory_enabled(memory_policy):
            return None
        return self.memory_gateway or self.memory_gateway_factory()

    @staticmethod
    def _memory_enabled(memory_policy) -> bool:
        policy = dict(memory_policy or {})
        automatic = policy.get("automatic")
        if isinstance(automatic, dict):
            return bool(automatic.get("enabled"))
        # Backward compatibility for immutable snapshots created before the
        # automatic-memory contract was introduced.
        return any(bool(dict(value or {}).get("enabled")) for value in policy.values())

    def _sandbox_executor(self, permissions: frozenset[str], run_id):
        if "tool.code.execute" not in permissions:
            return None
        provider = self.sandbox_provider or self.sandbox_provider_factory()
        recorder = DjangoSandboxLifecycleRecorder(run_id)
        try:
            return self.sandbox_executor_factory(provider, audit_sink=recorder.record)
        except TypeError:
            executor = self.sandbox_executor_factory(provider)
            if hasattr(executor, "audit_sink"):
                executor.audit_sink = recorder.record
            return executor

    @staticmethod
    def _effective_tool_policy(tool_policy, *, source: str) -> dict:
        """Apply the trusted entry-point ceiling before snapshot and execution."""

        policy = dict(tool_policy or {})
        if source != "public_client":
            return policy
        knowledge = dict(policy.get("knowledge") or {})
        knowledge["write"] = []
        knowledge["delete"] = []
        return {**policy, "knowledge": knowledge}

    @staticmethod
    def _apply_user_resource_ceiling(tool_policy, *, company, user) -> dict:
        """Intersect Agent workspace policy with the operator's knowledge role.

        Existing Agent policy remains the Agent side of the intersection; this
        method applies the independent human-resource ACL ceiling.  Companies
        not yet migrated to a Tenant retain the legacy path until migration.
        """

        tenant = Tenant.objects.filter(
            type=Tenant.TYPE_ORGANIZATION,
            external_id=f"organization:{company.cid}",
            status=Tenant.STATUS_ACTIVE,
        ).first()
        if tenant is None:
            return dict(tool_policy or {})
        knowledge_resource = Resource.objects.filter(
            tenant=tenant,
            type=Resource.TYPE_KNOWLEDGE_BASE,
            external_id=f"knowledge:{company.cid}",
        ).first()
        policy = dict(tool_policy or {})
        knowledge = dict(policy.get("knowledge") or {})
        if knowledge_resource is None or user is None:
            knowledge.update({"read": [], "write": [], "delete": []})
            return {**policy, "knowledge": knowledge}
        if not is_user_action_allowed(user, knowledge_resource, "query"):
            knowledge.update({"read": [], "write": [], "delete": []})
        else:
            if not is_user_action_allowed(user, knowledge_resource, "add_content"):
                knowledge["write"] = []
            if not is_user_action_allowed(user, knowledge_resource, "delete_content"):
                knowledge["delete"] = []
        return {**policy, "knowledge": knowledge}

    def _tool_permissions(self, tool_policy, *, source: str = "agent_test") -> frozenset[str]:
        """Derive dangerous Workspace capabilities from independent path scopes."""

        permissions = {"tool.code.execute", *WorkspacePolicy.from_config(tool_policy).tool_permissions()}
        if source == "public_client":
            permissions.difference_update(PUBLIC_CLIENT_FORBIDDEN_TOOL_PERMISSIONS)
        registered = {
            definition.required_permission
            for definition in self.tool_registry.definitions.values()
            if definition.required_permission
        }
        return frozenset(permissions & registered)

    def _model_tools(self, permissions: frozenset[str]) -> list[dict]:
        tools = []
        for definition in self.tool_registry.definitions.values():
            if definition.required_permission not in permissions:
                continue
            tools.append({
                "type": "function",
                "function": {
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": definition.args_schema,
                },
            })
        return tools

    @transaction.atomic
    def _create_run(
        self,
        *,
        request_id,
        company,
        agent,
        session,
        input_message,
        resolved_config,
        source,
        provider,
        model,
        trace_id,
        permission_facts,
    ) -> AgentRun:
        if AgentRun.objects.filter(company=company, request_id=request_id).exists():
            # A single input message is the server-side idempotency boundary.
            # Re-executing an existing Run could duplicate future side effects.
            raise ValueError("This input message already has an AgentRun")

        # Snapshot creation shares this transaction with Run creation. A unique
        # request race therefore rolls back both records instead of leaving an
        # orphaned immutable snapshot.
        snapshot = self.snapshot_service.create(resolved_config)
        return AgentRun.objects.create(
            company=company,
            request_id=request_id,
            agent=agent,
            user_id=session.operator_id,
            session=session,
            source=self.SOURCE_TO_DATABASE[source],
            engine=AgentRun.ENGINE_UNIFIED,
            status=AgentRun.STATUS_PENDING,
            config_snapshot=snapshot,
            input_message=input_message,
            model_provider=provider,
            model_name=model,
            metadata={"schemaVersion": 1, "trace_id": trace_id, "permission_facts": permission_facts},
        )

    @staticmethod
    def _recent_runs(session, input_message, config) -> list[AgentRun]:
        session_policy = dict(config.memory_policy.get("session") or {})
        window = max(0, int(session_policy.get("recentMessageWindow") or 12))
        # History is replayed as complete user/assistant turns.  Reading the
        # raw message stream here used to leak orphaned user messages from
        # FAILED/CANCELLED runs into the next model request.
        completed_turn_limit = window // 2
        if completed_turn_limit == 0:
            return []
        runs = list(
            AgentRun.objects.filter(
                session=session,
                agent=session.agent,
                status=AgentRun.STATUS_COMPLETED,
                input_message_id__lt=input_message.pk,
                input_message__session=session,
                output_message__session=session,
            )
            .select_related("input_message", "output_message")
            .prefetch_related("tool_calls")
            .order_by("-input_message_id")[:completed_turn_limit]
        )
        return list(reversed(runs))

    @staticmethod
    def _recent_messages(runs: list[AgentRun]) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for run in runs:
            user_content = str(run.input_message.content or "")
            assistant_content = str(run.output_message.content or "")
            if (
                run.input_message.role != "user"
                or run.output_message.role != "assistant"
                or not user_content.strip()
                or not assistant_content.strip()
            ):
                continue
            messages.extend((
                {"role": "user", "content": user_content},
                {"role": "assistant", "content": assistant_content},
            ))
        return messages

    @staticmethod
    def _recent_run_receipts(runs: list[AgentRun]) -> list[dict]:
        """Expose metadata-only receipts for questions about completed prior turns."""

        receipts = []
        for run in runs:
            calls = sorted(
                run.tool_calls.all(),
                key=lambda call: (call.created_at, str(call.pk)),
            )
            tools = [
                {
                    "executor": call.executor,
                    "name": call.tool_name,
                    "status": call.status,
                    "error_code": call.error_code or None,
                }
                for call in calls
            ]
            receipts.append({
                "run_id": str(run.pk),
                "input_message_id": run.input_message_id,
                "output_message_id": run.output_message_id,
                "status": run.status,
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                "sandbox_used": any(
                    item["executor"] == "sandbox"
                    and item["status"] == AgentToolCall.STATUS_COMPLETED
                    for item in tools
                ),
                "tools": tools,
            })
        return receipts

    @staticmethod
    async def _recorded_stream(
        source: AsyncIterator[RunEvent],
        recorder: "AgentRunRecorder",
    ) -> AsyncIterator[RunEvent]:
        terminal = False
        try:
            async for event in source:
                if await sync_to_async(recorder.cancel_requested, thread_sensitive=True)():
                    await sync_to_async(recorder.cancel, thread_sensitive=True)()
                    terminal = True
                    break
                await sync_to_async(recorder.record, thread_sensitive=True)(event)
                terminal = event.event_type in {
                    str(RunEventType.RUN_COMPLETED),
                    str(RunEventType.RUN_FAILED),
                    str(RunEventType.RUN_CANCELLED),
                }
                yield event
        except asyncio.CancelledError:
            await sync_to_async(recorder.cancel, thread_sensitive=True)()
            raise
        except Exception:
            await sync_to_async(recorder.fail, thread_sensitive=True)()
            raise
        finally:
            if not terminal:
                await sync_to_async(recorder.cancel, thread_sensitive=True)()


class DjangoToolCallRecorder:
    """Persist Phase 2 tool-call audit records without leaking raw arguments."""

    def __init__(self, run_id) -> None:
        self.run_id = run_id
        self._tool_to_model: dict[str, str] = {}

    async def started(self, *, tool_call_id: str, model_call_id: str, tool_name: str, executor: str, arguments: dict) -> None:
        self._tool_to_model[str(tool_call_id)] = str(model_call_id)
        await sync_to_async(self._started_sync, thread_sensitive=True)(
            model_call_id=str(model_call_id),
            tool_name=str(tool_name),
            executor=str(executor),
            arguments=dict(arguments or {}),
        )

    async def completed(self, *, tool_call_id: str, duration_ms: int | None = None, result_ref: str = "") -> None:
        model_call_id = self._tool_to_model.get(str(tool_call_id), "")
        if model_call_id:
            await sync_to_async(self._completed_sync, thread_sensitive=True)(
                model_call_id=model_call_id,
                duration_ms=duration_ms,
                result_ref=result_ref,
            )

    async def failed(self, *, tool_call_id: str, error_code: str, duration_ms: int | None = None) -> None:
        model_call_id = self._tool_to_model.get(str(tool_call_id), "")
        if model_call_id:
            await sync_to_async(self._failed_sync, thread_sensitive=True)(
                model_call_id=model_call_id,
                error_code=str(error_code or ""),
                duration_ms=duration_ms,
            )

    @transaction.atomic
    def _started_sync(self, *, model_call_id: str, tool_name: str, executor: str, arguments: dict) -> None:
        AgentToolCall.objects.update_or_create(
            run_id=self.run_id,
            model_call_id=model_call_id,
            defaults={
                "tool_name": tool_name,
                "executor": executor,
                "status": AgentToolCall.STATUS_RUNNING,
                "arguments_redacted": _redact_arguments(arguments),
                "arguments_hash": _hash_arguments(arguments),
                "started_at": timezone.now(),
                "completed_at": None,
                "duration_ms": None,
                "error_code": "",
                "result_ref": "",
            },
        )

    def _completed_sync(self, *, model_call_id: str, duration_ms: int | None, result_ref: str) -> None:
        AgentToolCall.objects.filter(run_id=self.run_id, model_call_id=model_call_id).update(
            status=AgentToolCall.STATUS_COMPLETED,
            duration_ms=duration_ms,
            result_ref=str(result_ref or ""),
            completed_at=timezone.now(),
            error_code="",
        )

    def _failed_sync(self, *, model_call_id: str, error_code: str, duration_ms: int | None) -> None:
        AgentToolCall.objects.filter(run_id=self.run_id, model_call_id=model_call_id).update(
            status=AgentToolCall.STATUS_FAILED,
            duration_ms=duration_ms,
            error_code=error_code,
            completed_at=timezone.now(),
        )


class DjangoRunCancelChecker:
    """Read the API-written cancel signal without exposing Django to graph nodes."""

    def __init__(self, run_id) -> None:
        self.run_id = run_id

    def __call__(self, _state) -> bool:
        return AgentRun.objects.filter(
            pk=self.run_id,
            cancel_requested_at__isnull=False,
            status__in=CANCELLABLE_RUN_STATUSES,
        ).exists()


class DjangoSandboxLifecycleRecorder:
    """Persist sandbox lifecycle audit events without leaking provider IDs."""

    def __init__(self, run_id) -> None:
        self.run_id = run_id

    def record(self, event_type: str, payload: dict) -> None:
        if event_type not in {
            str(RunEventType.SANDBOX_STARTED),
            str(RunEventType.SANDBOX_COMPLETED),
        }:
            return
        safe_payload = dict(payload or {})
        safe_payload.pop("external_id", None)
        safe_payload["schemaVersion"] = 1
        AgentRunRecorder(self.run_id)._append_audit_event(event_type, safe_payload)


class AgentRunRecorder:
    """Persist sparse events and lifecycle fields for one AgentRun."""

    def __init__(self, run_id) -> None:
        self.run_id = run_id
        self._first_token_recorded = False

    @transaction.atomic
    def record(self, event: RunEvent) -> None:
        event_type = str(event.event_type)
        if event_type != str(RunEventType.MESSAGE_DELTA):
            AgentRunEvent.objects.get_or_create(
                run_id=self.run_id,
                sequence=event.sequence,
                defaults={
                    "event_type": event_type,
                    "payload": dict(event.data or {}),
                },
            )

        updates = {}
        now = timezone.now()
        if event_type == str(RunEventType.RUN_STARTED):
            updates.update(status=AgentRun.STATUS_RUNNING, started_at=now)
        elif (
            event_type == str(RunEventType.MESSAGE_DELTA)
            and not self._first_token_recorded
        ):
            AgentRun.objects.filter(
                pk=self.run_id,
                first_token_at__isnull=True,
            ).update(first_token_at=now)
            self._first_token_recorded = True
        elif event_type == str(RunEventType.RUN_COMPLETED):
            updates.update(
                status=AgentRun.STATUS_COMPLETED,
                input_tokens=max(0, int(event.data.get("input_tokens") or 0)),
                output_tokens=max(0, int(event.data.get("output_tokens") or 0)),
                completed_at=now,
            )
        elif event_type == str(RunEventType.RUN_FAILED):
            updates.update(
                status=AgentRun.STATUS_FAILED,
                error_code=str(event.data.get("code") or ErrorCode.INTERNAL_ERROR),
                error_message=str(event.data.get("message") or "操作未完成，请稍后重试"),
                completed_at=now,
            )
            updates.update({key: max(0, int(event.data[key] or 0))
                for key in ("input_tokens", "output_tokens") if key in event.data})
        elif event_type == str(RunEventType.RUN_CANCELLED):
            updates.update(
                status=AgentRun.STATUS_CANCELLED,
                error_code=str(ErrorCode.RUN_CANCELLED),
                error_message="请求已取消",
                completed_at=now,
            )
        if updates:
            AgentRun.objects.filter(pk=self.run_id).update(**updates)

    def _append_audit_event(self, event_type: str, payload: dict) -> None:
        last_sequence = (
            AgentRunEvent.objects.filter(run_id=self.run_id)
            .order_by("-sequence")
            .values_list("sequence", flat=True)
            .first()
        )
        try:
            AgentRunEvent.objects.create(
                run_id=self.run_id,
                sequence=int(last_sequence or 0) + 1,
                event_type=event_type,
                payload=dict(payload or {}),
            )
        except IntegrityError:
            pass
    def cancel_requested(self) -> bool:
        return AgentRun.objects.filter(
            pk=self.run_id,
            cancel_requested_at__isnull=False,
            status__in=CANCELLABLE_RUN_STATUSES,
        ).exists()

    def cancel(self) -> None:
        now = timezone.now()
        AgentRun.objects.filter(
            pk=self.run_id,
            status__in=CANCELLABLE_RUN_STATUSES,
        ).update(
            status=AgentRun.STATUS_CANCELLED,
            cancel_requested_at=now,
            completed_at=now,
            error_code=str(ErrorCode.RUN_CANCELLED),
            error_message="请求已取消",
        )

    @transaction.atomic
    def fail(self) -> None:
        now = timezone.now()
        AgentToolCall.objects.filter(
            run_id=self.run_id,
            status=AgentToolCall.STATUS_RUNNING,
        ).update(
            status=AgentToolCall.STATUS_FAILED,
            completed_at=now,
            error_code=str(ErrorCode.INTERNAL_ERROR),
        )
        AgentRun.objects.filter(
            pk=self.run_id,
            status__in=CANCELLABLE_RUN_STATUSES,
        ).update(
            status=AgentRun.STATUS_FAILED,
            completed_at=now,
            error_code=str(ErrorCode.INTERNAL_ERROR),
            error_message="操作未完成，请稍后重试",
        )


def attach_output_message(run_id: str | None, assistant_message) -> None:
    """Link the single CompanyService-created assistant message to its Run."""

    if not run_id or assistant_message is None:
        return
    AgentRun.objects.filter(
        pk=run_id,
        company_id=assistant_message.session.transparentCompany.company_id,
        output_message__isnull=True,
    ).update(output_message=assistant_message)



def record_run_cancel_requested(run_id) -> None:
    last_sequence = (
        AgentRunEvent.objects.filter(run_id=run_id)
        .order_by("-sequence")
        .values_list("sequence", flat=True)
        .first()
    )
    try:
        AgentRunEvent.objects.create(
            run_id=run_id,
            sequence=int(last_sequence or 0) + 1,
            event_type=str(RunEventType.RUN_CANCELLED),
            payload={"code": str(ErrorCode.RUN_CANCELLED), "message": "请求正在取消"},
        )
    except IntegrityError:
        pass
def request_run_cancel(*, run_id, company) -> RunCancelResult:
    """Request cancellation for one company-scoped AgentRun."""

    run = AgentRun.objects.get(pk=run_id, company=company)
    if run.status in TERMINAL_RUN_STATUSES:
        return RunCancelResult(
            run_id=str(run.pk),
            status=run.status,
            cancel_requested_at=run.cancel_requested_at.isoformat() if run.cancel_requested_at else None,
            already_terminal=True,
        )

    now = timezone.now()
    updated = AgentRun.objects.filter(
        pk=run.pk,
        status__in=CANCELLABLE_RUN_STATUSES,
    ).update(
        status=AgentRun.STATUS_CANCELLING,
        cancel_requested_at=now,
        error_code=str(ErrorCode.RUN_CANCELLED),
        error_message="请求正在取消",
    )
    if updated:
        record_run_cancel_requested(run.pk)
    run.refresh_from_db(fields=["status", "cancel_requested_at"])
    return RunCancelResult(
        run_id=str(run.pk),
        status=run.status,
        cancel_requested_at=run.cancel_requested_at.isoformat() if run.cancel_requested_at else None,
        already_terminal=False,
    )
