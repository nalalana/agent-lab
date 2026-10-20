"""Authorized OpenViking implementation of the frozen MemoryGateway contract."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from difflib import SequenceMatcher
import re
from typing import Iterable
from urllib.parse import unquote

from asgiref.sync import sync_to_async
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.agent_runtime.contracts import CommitRequest, MemoryItem, RecallRequest
from apps.agent_runtime.models import AgentRun, MemorySyncJob
from apps.company.models import TransparentCompanyAgentTestMessage

from .access import EffectiveMemoryAccess, MemoryAccessResolver
from .client import OpenVikingClient
from .errors import (
    MemoryGatewayError,
    MemoryPermissionDenied,
    OpenVikingExtractionFailed,
    OpenVikingTaskPending,
    OpenVikingUnavailable,
)
from .namespaces import MemoryNamespace, NamespaceResolver, TrustedMemoryContext
from .quality import assess_memory_candidate, is_multistep_task, is_smoke_test


class OpenVikingMemoryGateway:
    """Owns namespace calculation, authorization, recall, and commit queuing."""

    PAYLOAD_SCHEMA_VERSION = 3
    SUPPORTED_PAYLOAD_SCHEMA_VERSIONS = frozenset({1, 2, 3})
    MAX_QUERY_LENGTH = 8000
    MAX_COMMIT_LENGTH = 100000
    EXPERIENCE_REQUIRED_PRIOR_TASKS = 2
    PENDING_OVERLAY_LIMIT = 3
    PENDING_OVERLAY_MAX_AGE = timedelta(hours=1)
    PENDING_OVERLAY_TYPES = frozenset({
        'profile',
        'preferences',
        'entities',
        'events',
    })
    ADMISSION_MEMORY_TYPES = frozenset({
        'profile',
        'preferences',
        'entities',
        'events',
        'cases',
    })

    def __init__(
        self,
        client=None,
        namespace_resolver=None,
        access_resolver=None,
        dispatcher=None,
    ):
        self.client = client
        self.namespace_resolver = namespace_resolver or NamespaceResolver()
        self.access_resolver = access_resolver or MemoryAccessResolver()
        self.dispatcher = dispatcher or self._dispatch_commit

    def _client(self):
        # Queue creation must remain available even when the external memory
        # service is temporarily unconfigured or offline. Workers resolve the
        # client only when they actually process a durable job.
        if self.client is None:
            self.client = OpenVikingClient.from_settings()
        return self.client

    async def recall(self, request: RecallRequest) -> list[MemoryItem]:
        query, top_k = self._validate_recall(request)
        context, scopes, access = await sync_to_async(
            self._prepare_recall,
            thread_sensitive=True,
        )(request)
        if not scopes:
            return []
        try:
            items = await self._search_scopes(context, access, scopes, query, top_k)
        except MemoryPermissionDenied:
            raise
        except MemoryGatewayError:
            # Provider/configuration failures degrade to an answer without
            # committed memory; authorization failures remain fail-closed.
            items = []
        pending_items = await sync_to_async(
            self._pending_overlay_items,
            thread_sensitive=True,
        )(context)
        items.extend(pending_items)
        return self._rank_and_deduplicate(items, top_k)

    async def commit(self, request: CommitRequest) -> None:
        self._validate_commit(request)
        context, existing_job = await sync_to_async(
            self._authorize_commit,
            thread_sensitive=True,
        )(request)
        if existing_job is not None:
            return
        if not self._base_memory_types_for_commit(request):
            return
        job, created = await sync_to_async(
            self._get_or_create_job,
            thread_sensitive=True,
        )(request, context)
        if not created:
            self._assert_job_owner(job, context)
            return
        payload = self.serialize_commit_request(request)
        try:
            await sync_to_async(self.dispatcher, thread_sensitive=False)(str(job.id), payload)
        except Exception as exc:
            await sync_to_async(self._mark_queue_failed, thread_sensitive=True)(job.id)
            raise OpenVikingUnavailable('Memory commit queue is unavailable.') from exc

    def process_commit_job(self, job: MemorySyncJob, payload: dict) -> tuple[str, ...]:
        """Submit or poll one native OpenViking session commit."""
        request = self.deserialize_commit_request(payload)
        self._validate_commit(request)
        context = self._resolve_context(request)
        self._validate_source_messages(context, request.source_message_ids)
        self._assert_job_owner(job, context)
        scopes = self.access_resolver.commit_scopes(context)
        if 'agent' not in scopes:
            raise MemoryPermissionDenied('Native memory extraction requires Agent memory write access.')
        namespace = self.namespace_resolver.for_scope(context, 'agent')
        peer_id = (
            self.namespace_resolver.public_peer_id(context)
            if self.namespace_resolver.is_public_client_context(context)
            else ''
        )
        if not job.external_session_id:
            job.external_session_id = f'tsds-memory-{str(job.id).replace("-", "")}'
            MemorySyncJob.objects.filter(pk=job.id).update(
                external_session_id=job.external_session_id,
            )
        if not job.external_task_id:
            self._submit_native_commit(job, request, namespace, peer_id=peer_id)

        task = self._client().get_native_task(namespace, job.external_task_id)
        status = str(task.get('status') or '').lower()
        if status in {'pending', 'running', 'cancelling'}:
            raise OpenVikingTaskPending('OpenViking memory extraction is still running.')
        if status != 'completed':
            raise OpenVikingExtractionFailed('OpenViking native memory extraction failed.')

        result = task.get('result') if isinstance(task.get('result'), dict) else {}
        archive_uri = str(
            job.external_archive_uri
            or result.get('archive_uri')
            or task.get('archive_uri')
            or ''
        )
        invalidate_tree = getattr(self._client(), 'invalidate_memory_tree', None)
        if callable(invalidate_tree):
            try:
                invalidate_tree(namespace)
            except Exception:
                # Cache invalidation must not turn a completed durable commit
                # into a failed job. The short TTL remains the fallback.
                pass
        MemorySyncJob.objects.filter(pk=job.id).update(
            status=MemorySyncJob.STATUS_COMPLETED,
            error_code='',
            next_retry_at=None,
            external_archive_uri=archive_uri,
            completed_at=timezone.now(),
        )
        return (archive_uri,) if archive_uri else ()

    def _submit_native_commit(
        self,
        job: MemorySyncJob,
        request: CommitRequest,
        namespace: MemoryNamespace,
        *,
        peer_id: str = '',
    ) -> None:
        client = self._client()
        memory_types = self._memory_types_for_commit(request)
        created = client.create_native_session(
            namespace,
            job.external_session_id,
            peer_memory_only=bool(peer_id),
            memory_types=memory_types,
        )
        commit_result = None
        if not created:
            existing_tasks = client.list_native_session_tasks(
                namespace,
                job.external_session_id,
            )
            if existing_tasks:
                existing_tasks.sort(
                    key=lambda item: float(item.get('created_at') or 0),
                    reverse=True,
                )
                task = existing_tasks[0]
                commit_result = {
                    'task_id': task.get('task_id'),
                    'archive_uri': (
                        task.get('result', {}).get('archive_uri')
                        if isinstance(task.get('result'), dict)
                        else ''
                    ),
                }
            else:
                # A previous attempt stopped before commit. Recreate the
                # job-scoped session so replay cannot duplicate messages.
                client.delete_native_session(namespace, job.external_session_id)
                if not client.create_native_session(
                    namespace,
                    job.external_session_id,
                    peer_memory_only=bool(peer_id),
                    memory_types=memory_types,
                ):
                    raise OpenVikingUnavailable('OpenViking session could not be recreated.')
        if commit_result is None:
            client.add_native_session_messages(
                namespace,
                job.external_session_id,
                messages=self._native_messages(request),
                turn_id=request.idempotency_key,
                source_message_ids=request.source_message_ids,
                peer_id=peer_id,
            )
            commit_result = client.commit_native_session(namespace, job.external_session_id)
        task_id = str(commit_result.get('task_id') or '')
        if not task_id:
            raise OpenVikingUnavailable('OpenViking session commit task is unavailable.')
        archive_uri = str(commit_result.get('archive_uri') or '')
        MemorySyncJob.objects.filter(pk=job.id).update(
            external_task_id=task_id,
            external_archive_uri=archive_uri,
        )
        job.external_task_id = task_id
        job.external_archive_uri = archive_uri

    @staticmethod
    def _native_messages(request: CommitRequest) -> tuple[dict[str, str], ...]:
        if request.messages:
            return request.messages
        return ({'role': 'user', 'content': request.content},)

    async def _search_scopes(
        self,
        context: TrustedMemoryContext,
        access: EffectiveMemoryAccess,
        scopes: Iterable[str],
        query: str,
        top_k: int,
    ) -> list[MemoryItem]:
        if self.namespace_resolver.is_public_client_context(context):
            namespace = self.namespace_resolver.for_public_peer(context)
            client = self._client()
            search = getattr(client, 'search_memory_tree', client.search)
            raw_items = await asyncio.to_thread(
                search,
                namespace,
                query,
                top_k,
            )
            return self._convert_items(raw_items, namespace)

        items = []
        for scope in scopes:
            namespace = self._namespace_for_scope(context, access, scope)
            limit = min(top_k, access.max_user_items) if scope == 'user' else top_k
            if limit <= 0:
                continue
            client = self._client()
            search = getattr(client, 'search_memory_tree', client.search)
            raw_items = await asyncio.to_thread(search, namespace, query, limit)
            items.extend(self._convert_items(raw_items, namespace))
        return items

    def _namespace_for_scope(
        self,
        context: TrustedMemoryContext,
        access: EffectiveMemoryAccess,
        scope: str,
    ) -> MemoryNamespace:
        if scope == 'user' and access.user_scope == 'agent_user':
            return self.namespace_resolver.agent_user_namespace(context)
        return self.namespace_resolver.for_scope(context, scope)

    def _pending_overlay_items(
        self,
        context: TrustedMemoryContext,
    ) -> list[MemoryItem]:
        """Expose recent state updates while native extraction is still pending."""
        jobs = MemorySyncJob.objects.filter(
            company_id=context.company.id,
            agent_id=context.agent.id,
            job_type=MemorySyncJob.TYPE_MEMORY_COMMIT,
            status__in=(
                MemorySyncJob.STATUS_PENDING,
                MemorySyncJob.STATUS_RUNNING,
            ),
            created_at__gte=timezone.now() - self.PENDING_OVERLAY_MAX_AGE,
        )
        if self.namespace_resolver.is_public_client_context(context):
            jobs = jobs.filter(user_id=context.user.id)
        jobs = list(jobs.order_by('-created_at')[: self.PENDING_OVERLAY_LIMIT * 2])
        if not jobs:
            return []

        message_ids = {
            message_id
            for job in jobs
            for message_id in _message_ids(job.source_ref)
        }
        messages = {
            str(message.id): message
            for message in TransparentCompanyAgentTestMessage.objects.filter(
                id__in=message_ids,
                session__agent_id=context.agent.id,
                session__transparentCompany__company_id=context.company.id,
            )
        }
        result = []
        for job in jobs:
            job_messages = [
                messages.get(message_id)
                for message_id in _message_ids(job.source_ref)
            ]
            user_content = '\n'.join(
                str(message.content or '').strip()
                for message in job_messages
                if message is not None
                and message.role == TransparentCompanyAgentTestMessage.ROLE_USER
                and str(message.content or '').strip()
            )
            assistant_content = '\n'.join(
                str(message.content or '').strip()
                for message in job_messages
                if message is not None
                and message.role == TransparentCompanyAgentTestMessage.ROLE_ASSISTANT
                and str(message.content or '').strip()
            )
            assessment = assess_memory_candidate(user_content, assistant_content)
            immediate_types = assessment.memory_types & self.PENDING_OVERLAY_TYPES
            if not assessment.eligible or not immediate_types:
                continue
            created_at = job.created_at.isoformat(timespec='seconds')
            result.append(MemoryItem(
                memory_id=f'pending:{job.id}',
                scope='pending_update',
                content=(
                    '【待同步的最新长期记忆更新】\n'
                    f'更新时间：{created_at}\n'
                    f'类型：{", ".join(sorted(immediate_types))}\n'
                    f'用户最新陈述：{user_content[:1800]}\n'
                    '若与已提交记忆冲突，以本条更新为准。'
                ),
                score=2.0 - (len(result) * 0.001),
                source_ref=job.source_ref,
            ))
            if len(result) >= self.PENDING_OVERLAY_LIMIT:
                break
        return result

    @staticmethod
    def _convert_items(raw_items, namespace: MemoryNamespace) -> list[MemoryItem]:
        converted = []
        for item in raw_items:
            uri = str(item.get('uri') or '')
            if not uri.startswith(namespace.root_uri):
                raise MemoryPermissionDenied('OpenViking returned memory outside the authorized namespace.')
            content = str(item.get('content') or item.get('abstract') or item.get('overview') or '')
            converted.append(
                MemoryItem(
                    memory_id=uri,
                    scope=namespace.scope,
                    content=content,
                    score=float(item.get('score') or 0.0),
                    source_ref=uri,
                )
            )
        return converted

    @staticmethod
    def _rank_and_deduplicate(items: Iterable[MemoryItem], top_k: int) -> list[MemoryItem]:
        by_id = {}
        for item in items:
            deduplication_key = _recall_family_key(item)
            current = by_id.get(deduplication_key)
            if current is None or item.score > current.score:
                by_id[deduplication_key] = item
        return sorted(by_id.values(), key=lambda item: item.score, reverse=True)[:top_k]

    def _memory_types_for_commit(self, request: CommitRequest) -> tuple[str, ...]:
        """Apply the admission whitelist, then enable native task training."""
        memory_types = self._base_memory_types_for_commit(request)
        if 'cases' in memory_types and self._is_complex_task(request):
            memory_types.add('trajectories')
        if 'cases' in memory_types and self._has_repeated_task_evidence(request):
            memory_types.add('experiences')
        return tuple(sorted(memory_types))

    @classmethod
    def _base_memory_types_for_commit(cls, request: CommitRequest) -> set[str]:
        if request.memory_types:
            return set(request.memory_types)
        assessment = assess_memory_candidate(
            _request_user_content(request),
            _request_assistant_content(request),
        )
        return set(assessment.memory_types) if assessment.eligible else set()

    @staticmethod
    def _is_complex_task(request: CommitRequest) -> bool:
        run = None
        if str(request.run_id or '').strip():
            try:
                run = AgentRun.objects.filter(
                    pk=request.run_id,
                    company_id=request.company_id,
                    agent_id=request.agent_id,
                ).first()
            except (TypeError, ValueError, ValidationError):
                run = None
        if (
            run is not None
            and not is_smoke_test(_request_user_content(request))
            and run.tool_calls.filter(status='completed').exists()
        ):
            return True
        return is_multistep_task(_request_user_content(request))

    def _has_repeated_task_evidence(self, request: CommitRequest) -> bool:
        current_signatures = {
            signature
            for signature in (
                _task_signature(_request_user_content(request)),
                _task_signature(self._session_title(request.session_id)),
            )
            if signature
        }
        if not current_signatures:
            return False

        jobs = list(
            MemorySyncJob.objects.filter(
                company_id=request.company_id,
                agent_id=request.agent_id,
                job_type=MemorySyncJob.TYPE_MEMORY_COMMIT,
                status=MemorySyncJob.STATUS_COMPLETED,
            )
            .exclude(external_archive_uri='')
            .order_by('-completed_at')[:100]
        )
        message_ids = {
            message_id
            for job in jobs
            for message_id in _message_ids(job.source_ref)
        }
        messages = {
            str(message.id): message
            for message in TransparentCompanyAgentTestMessage.objects.filter(
                id__in=message_ids,
            ).select_related('session')
        }
        matching_sessions = set()
        for job in jobs:
            message = next(
                (messages.get(message_id) for message_id in _message_ids(job.source_ref)),
                None,
            )
            if message is None:
                continue
            prior_signatures = {
                signature
                for signature in (
                    _task_signature(message.content),
                    _task_signature(message.session.title),
                )
                if signature
            }
            if any(
                _same_task_family(current, previous)
                for current in current_signatures
                for previous in prior_signatures
            ):
                matching_sessions.add(str(message.session_id))
        return len(matching_sessions) >= self.EXPERIENCE_REQUIRED_PRIOR_TASKS

    @staticmethod
    def _session_title(session_id: object) -> str:
        return str(
            TransparentCompanyAgentTestMessage.objects.filter(session_id=session_id)
            .select_related('session')
            .values_list('session__title', flat=True)
            .first()
            or ''
        )

    def _resolve_context(self, request):
        return self.namespace_resolver.resolve_context(
            company_id=request.company_id,
            agent_id=request.agent_id,
            user_id=request.user_id,
            session_id=request.session_id,
        )

    def _prepare_recall(self, request):
        context = self._resolve_context(request)
        access = self.access_resolver.resolve(context)
        if self.namespace_resolver.is_public_client_context(context):
            if not access.can_read:
                raise MemoryPermissionDenied('Agent memory read permission is disabled.')
            return context, ('user',), access
        scopes = self.access_resolver.authorize_recall(context, request.scopes)
        return context, scopes, access

    def _authorize_commit(self, request):
        context = self._resolve_context(request)
        self._validate_source_messages(context, request.source_message_ids)
        self.access_resolver.commit_scopes(context)
        existing_job = MemorySyncJob.objects.filter(
            idempotency_key=request.idempotency_key,
        ).first()
        if existing_job is not None:
            self._assert_job_owner(existing_job, context)
        return context, existing_job

    @staticmethod
    def _mark_queue_failed(job_id) -> None:
        MemorySyncJob.objects.filter(pk=job_id).update(
            status=MemorySyncJob.STATUS_FAILED,
            error_code='MEMORY_QUEUE_UNAVAILABLE',
        )

    @staticmethod
    def _validate_source_messages(
        context: TrustedMemoryContext,
        source_message_ids: tuple[object, ...],
    ) -> None:
        expected_ids = {str(value) for value in source_message_ids}
        actual_ids = {
            str(value)
            for value in TransparentCompanyAgentTestMessage.objects.filter(
                pk__in=source_message_ids,
                session_id=context.session.id,
            ).values_list('pk', flat=True)
        }
        if actual_ids != expected_ids:
            raise MemoryPermissionDenied('Memory source messages do not belong to the session.')

    @classmethod
    def _validate_recall(cls, request: RecallRequest) -> tuple[str, int]:
        query = str(request.query or '').strip()
        if not query or len(query) > cls.MAX_QUERY_LENGTH:
            raise ValueError('Memory recall query length is invalid.')
        top_k = int(request.top_k)
        if top_k < 1 or top_k > 50:
            raise ValueError('Memory recall top_k must be between 1 and 50.')
        return query, top_k

    @classmethod
    def _validate_commit(cls, request: CommitRequest) -> None:
        if not request.idempotency_key or len(request.idempotency_key) > 255:
            raise ValueError('Memory commit idempotency key is invalid.')
        if not request.content.strip() or len(request.content) > cls.MAX_COMMIT_LENGTH:
            raise ValueError('Memory commit content length is invalid.')
        if request.messages:
            total_length = 0
            for message in request.messages:
                role = str(message.get('role') or '').strip().lower()
                content = str(message.get('content') or '').strip()
                if role not in {'user', 'assistant'} or not content:
                    raise ValueError('Memory commit messages are invalid.')
                total_length += len(content)
            if total_length > cls.MAX_COMMIT_LENGTH:
                raise ValueError('Memory commit messages are too long.')
        if (
            len(set(request.memory_types)) != len(request.memory_types)
            or not set(request.memory_types).issubset(cls.ADMISSION_MEMORY_TYPES)
        ):
            raise ValueError('Memory commit memory types are invalid.')
        if not request.source_message_ids:
            raise ValueError('Memory commit requires source message IDs for durable retry.')
        if len(cls._source_ref(request.source_message_ids)) > 1024:
            raise ValueError('Memory commit source references are too long.')

    @transaction.atomic
    def _get_or_create_job(self, request, context):
        return MemorySyncJob.objects.get_or_create(
            idempotency_key=request.idempotency_key,
            defaults={
                'company': context.company,
                'agent': context.agent,
                'user': context.user,
                'job_type': MemorySyncJob.TYPE_MEMORY_COMMIT,
                'source_ref': self._source_ref(request.source_message_ids),
            },
        )

    @staticmethod
    def _assert_job_owner(job: MemorySyncJob, context: TrustedMemoryContext) -> None:
        if (
            job.company_id != context.company.id
            or job.agent_id != context.agent.id
            or job.user_id != (context.user.id if context.user else None)
        ):
            raise MemoryPermissionDenied('Memory idempotency key belongs to another context.')

    @staticmethod
    def _source_ref(message_ids: tuple[object, ...]) -> str:
        return 'messages:' + ','.join(str(value) for value in message_ids)

    @staticmethod
    def serialize_commit_request(request: CommitRequest) -> dict:
        return {
            'schemaVersion': OpenVikingMemoryGateway.PAYLOAD_SCHEMA_VERSION,
            'run_id': str(request.run_id),
            'company_id': str(request.company_id),
            'agent_id': str(request.agent_id),
            'user_id': str(request.user_id) if request.user_id is not None else None,
            'session_id': str(request.session_id),
            'source_message_ids': [str(value) for value in request.source_message_ids],
            'content': request.content,
            'idempotency_key': request.idempotency_key,
            'messages': [dict(message) for message in request.messages],
            'memory_types': list(request.memory_types),
        }

    @staticmethod
    def deserialize_commit_request(payload: dict) -> CommitRequest:
        if payload.get('schemaVersion') not in OpenVikingMemoryGateway.SUPPORTED_PAYLOAD_SCHEMA_VERSIONS:
            raise ValueError('Unsupported memory commit payload schemaVersion.')
        return CommitRequest(
            run_id=str(payload.get('run_id') or ''),
            company_id=payload.get('company_id'),
            agent_id=payload.get('agent_id'),
            user_id=payload.get('user_id'),
            session_id=payload.get('session_id'),
            source_message_ids=tuple(payload.get('source_message_ids') or ()),
            content=str(payload.get('content') or ''),
            idempotency_key=str(payload.get('idempotency_key') or ''),
            messages=tuple(
                {
                    'role': str(item.get('role') or ''),
                    'content': str(item.get('content') or ''),
                }
                for item in (payload.get('messages') or ())
                if isinstance(item, dict)
            ),
            memory_types=tuple(str(value) for value in (payload.get('memory_types') or ())),
        )

    @staticmethod
    def _dispatch_commit(job_id: str, payload: dict) -> None:
        from .tasks import commit_memory_job

        commit_memory_job.delay(job_id, payload)


def _message_ids(source_ref: str) -> tuple[str, ...]:
    if not str(source_ref or '').startswith('messages:'):
        return ()
    return tuple(
        value
        for value in str(source_ref).removeprefix('messages:').split(',')
        if value
    )


def _request_user_content(request: CommitRequest) -> str:
    for message in request.messages:
        if str(message.get('role') or '').lower() == 'user':
            return str(message.get('content') or '').strip()
    return str(request.content or '').strip()


def _request_assistant_content(request: CommitRequest) -> str:
    for message in reversed(request.messages):
        if str(message.get('role') or '').lower() == 'assistant':
            return str(message.get('content') or '').strip()
    return str(request.content or '').strip()


def _task_signature(value: object) -> str:
    normalized = re.sub(r'[^\w\u4e00-\u9fff]+', '', str(value or '').casefold())
    for token in (
        '请帮我', '帮我', '请', '一个', '一份', '一封', '一下',
        '起草', '撰写', '编写', '生成', '制作', '创建', '完成', '处理', '执行',
    ):
        normalized = normalized.replace(token, '')
    return normalized[:160]


def _same_task_family(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if min(len(left), len(right)) >= 4 and (left in right or right in left):
        return True
    return SequenceMatcher(None, left, right).ratio() >= 0.72


def _recall_family_key(item: MemoryItem) -> str:
    uri = unquote(str(item.memory_id or ''))
    match = re.search(r'/memories/(cases|trajectories|experiences)/([^/]+?)(?:\.md)?$', uri)
    if not match:
        return item.memory_id
    stem = re.sub(r'_?\d{8,14}$', '', match.group(2).removesuffix('.md'))
    signature = _task_signature(stem)
    return f'{item.scope}:task:{signature}' if signature else item.memory_id
