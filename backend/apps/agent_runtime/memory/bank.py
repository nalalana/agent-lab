"""Read the durable memories produced by native OpenViking session commits."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePosixPath
from threading import BoundedSemaphore
from typing import Any

from django.conf import settings

from apps.agent_runtime.models import MemorySyncJob
from apps.company.models import (
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentTestMessage,
)

from .client import OpenVikingClient
from .errors import OpenVikingUnavailable
from .namespaces import MemoryNamespace, NamespaceResolver


MEMORY_GROUPS: tuple[dict[str, Any], ...] = (
    {
        'key': 'user',
        'label': '用户信息与偏好',
        'types': ('profile', 'preferences'),
    },
    {
        'key': 'people_events',
        'label': '人物与事项',
        'types': ('entities', 'events'),
    },
    {
        'key': 'task_experience',
        'label': '任务经验',
        'types': ('cases', 'trajectories', 'experiences'),
    },
)
DISPLAY_MEMORY_TYPES = frozenset(
    memory_type
    for group in MEMORY_GROUPS
    for memory_type in group['types']
)
TYPE_TO_GROUP = {
    memory_type: group['key']
    for group in MEMORY_GROUPS
    for memory_type in group['types']
}
TYPE_LABELS = {
    'profile': '用户信息',
    'preferences': '用户偏好',
    'entities': '人物与实体',
    'events': '事项与事件',
    'cases': '任务案例',
    'trajectories': '执行轨迹',
    'experiences': '任务经验',
}
HIDDEN_FILENAMES = frozenset({'.abstract.md', '.overview.md', '.done', '.meta.json'})
TASK_MEMORY_TYPES = ('cases', 'trajectories', 'experiences')
MEMORY_SUMMARY_LIMIT = 140
RECENT_MEMORY_LIMIT = 3
AGENT_READ_WORKERS = 2
FILE_READ_WORKERS = 3
TENANT_CONCURRENCY_RETRIES = 20
TENANT_CONCURRENCY_RETRY_SECONDS = 0.1
MEMORY_READ_CONCURRENCY = 3
MEMORY_READ_SEMAPHORE = BoundedSemaphore(MEMORY_READ_CONCURRENCY)
GENERIC_TITLES = frozenset({
    'situation',
    'task signature',
    'input',
    'rubric',
    'evidence',
    'linked experiences',
    'summary',
    'description',
    'details',
    'context',
    'case',
    'cases',
    'trajectory',
    'trajectories',
    'experience',
    'experiences',
})


class MemoryBankService:
    """Build the frontend memory bank from current OpenViking memory files."""

    def __init__(self, *, client=None, namespace_resolver=None):
        self.client = client or OpenVikingClient.from_settings()
        self.namespace_resolver = namespace_resolver or NamespaceResolver()

    def _openviking_read(self, operation, *args, **kwargs):
        """Wait briefly for a tenant slot instead of returning a partial snapshot."""

        with MEMORY_READ_SEMAPHORE:
            for attempt in range(TENANT_CONCURRENCY_RETRIES + 1):
                try:
                    return operation(*args, **kwargs)
                except OpenVikingUnavailable as exc:
                    is_concurrency_limit = (
                        'tenant concurrency limit exceeded' in str(exc).casefold()
                    )
                    if not is_concurrency_limit or attempt >= TENANT_CONCURRENCY_RETRIES:
                        raise
                    time.sleep(
                        min(
                            TENANT_CONCURRENCY_RETRY_SECONDS * (attempt + 1),
                            0.5,
                        )
                    )

    def list_company(self, company, transparent_company) -> dict[str, Any]:
        agents = list(
            TransparentCompanyAgentDefinition.objects.filter(
                transparentCompany=transparent_company,
                enabled=True,
                status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
            ).select_related('transparentCompany').order_by('role', 'id')
        )
        jobs_by_agent = self._completed_jobs(company, agents)
        source_context = self._source_context(jobs_by_agent)
        entries: list[dict[str, Any]] = []
        warnings: list[str] = []
        agent_rows: list[dict[str, Any]] = []

        def load_agent(agent):
            agent_entries: list[dict[str, Any]] = []
            agent_warnings: list[str] = []
            try:
                namespace = self.namespace_resolver.for_agent(company, agent)
                provenance = self._provenance(
                    namespace,
                    jobs_by_agent.get(agent.id, ()),
                    source_context,
                    agent_warnings,
                )
                agent_entries = _aggregate_task_memories(
                    self._read_agent_memories(
                        agent,
                        namespace,
                        provenance,
                        agent_warnings,
                    )
                )
            except Exception as exc:
                agent_warnings.append(
                    f'Agent {agent.name or agent.agentId} 的长期记忆读取失败：{exc}'
                )
            return (
                agent_entries,
                {
                    'id': str(agent.id),
                    'agent_id': agent.agentId,
                    'name': agent.name or agent.agentId,
                    'memory_count': len(agent_entries),
                    'summary': _agent_summary(agent_entries),
                },
                agent_warnings,
            )

        worker_count = min(len(agents), AGENT_READ_WORKERS)
        if worker_count:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                for agent_entries, agent_row, agent_warnings in executor.map(
                    load_agent,
                    agents,
                ):
                    entries.extend(agent_entries)
                    agent_rows.append(agent_row)
                    warnings.extend(agent_warnings)

        entries.sort(key=lambda item: item.get('updated_at') or '', reverse=True)
        return {
            'agents': agent_rows,
            'list': entries,
            'groups': [
                {**group, 'types': list(group['types'])}
                for group in MEMORY_GROUPS
            ],
            'type_counts': dict(Counter(item['memory_type'] for item in entries)),
            'warnings': warnings,
            'source': 'native_openviking_commit',
            'openviking_version': str(getattr(settings, 'OPENVIKING_VERSION', '') or ''),
        }

    @staticmethod
    def _completed_jobs(company, agents):
        result = defaultdict(list)
        queryset = (
            MemorySyncJob.objects.filter(
                company=company,
                agent__in=agents,
                job_type=MemorySyncJob.TYPE_MEMORY_COMMIT,
                status=MemorySyncJob.STATUS_COMPLETED,
            )
            .exclude(external_archive_uri='')
            .order_by('completed_at', 'created_at')
        )
        for job in queryset:
            result[job.agent_id].append(job)
        return result

    @staticmethod
    def _source_context(jobs_by_agent):
        jobs = [job for values in jobs_by_agent.values() for job in values]
        message_ids = {
            message_id
            for job in jobs
            for message_id in _message_ids(job.source_ref)
        }
        messages = {
            str(message.id): message
            for message in TransparentCompanyAgentTestMessage.objects.filter(
                id__in=message_ids
            ).select_related('session')
        }
        result = {}
        for job in jobs:
            message = next(
                (messages.get(message_id) for message_id in _message_ids(job.source_ref)),
                None,
            )
            session = message.session if message is not None else None
            result[job.id] = {
                'type': 'conversation' if session is not None else 'task',
                'task_id': str(job.id),
                'session_id': str(session.id) if session is not None else '',
                'conversation_id': session.conversationId if session is not None else '',
                'title': (
                    session.title
                    if session is not None and session.title
                    else f'记忆沉淀任务 {str(job.id)[:8]}'
                ),
            }
        return result

    def _provenance(self, namespace, jobs, source_context, warnings):
        provenance = {}

        def read_diff(job):
            diff_uri = f"{job.external_archive_uri.rstrip('/')}/memory_diff.json"
            try:
                return job, json.loads(
                    self._openviking_read(
                        self.client.read_content,
                        namespace,
                        diff_uri,
                    ) or '{}'
                ), None
            except Exception as exc:
                return job, None, exc

        job_list = list(jobs)
        if not job_list:
            return provenance
        with ThreadPoolExecutor(
            max_workers=min(len(job_list), FILE_READ_WORKERS)
        ) as executor:
            diff_results = executor.map(read_diff, job_list)
        for job, diff, error in diff_results:
            if error is not None:
                warnings.append(
                    f'记忆任务 {str(job.id)[:8]} 的来源记录读取失败：{error}'
                )
                continue
            if not isinstance(diff, dict):
                continue
            extracted_at = str(diff.get('extracted_at') or job.completed_at or '')
            operations = diff.get('operations')
            if not isinstance(operations, dict):
                continue
            for operation_name in ('adds', 'updates'):
                for operation in operations.get(operation_name, ()) or ():
                    if not isinstance(operation, dict):
                        continue
                    memory_type = str(operation.get('memory_type') or '').strip()
                    uri = _normalize_memory_uri(
                        namespace,
                        str(operation.get('uri') or ''),
                        memory_type,
                    )
                    if uri:
                        provenance[uri] = {
                            **source_context.get(job.id, {}),
                            'extracted_at': extracted_at,
                        }
        return provenance

    def _read_agent_memories(self, agent, namespace, provenance, warnings=None):
        result = []
        warnings = warnings if warnings is not None else []
        listing = self._openviking_read(
            self.client.list_directory,
            namespace,
            namespace.root_uri,
            recursive=True,
            node_limit=2000,
        )
        candidates = []
        for item in listing:
            uri = str(item.get('uri') or item.get('path') or '').strip()
            name = str(item.get('name') or PurePosixPath(uri).name).strip()
            if (
                not uri.startswith(namespace.root_uri)
                or bool(item.get('isDir') or item.get('is_dir'))
                or name in HIDDEN_FILENAMES
                or name.startswith('.')
                or not name.endswith('.md')
            ):
                continue
            memory_type = _memory_type(namespace, uri)
            if memory_type not in DISPLAY_MEMORY_TYPES:
                continue
            candidates.append((item, uri, name, memory_type))

        def read_memory(candidate):
            item, uri, name, memory_type = candidate
            try:
                content = self._openviking_read(
                    self.client.read_content,
                    namespace,
                    uri,
                ).strip()
                return item, uri, name, memory_type, content, None
            except Exception as exc:
                return item, uri, name, memory_type, '', exc

        if not candidates:
            return result
        with ThreadPoolExecutor(
            max_workers=min(len(candidates), FILE_READ_WORKERS)
        ) as executor:
            memory_results = executor.map(read_memory, candidates)
        for item, uri, name, memory_type, content, error in memory_results:
            if error is not None:
                warnings.append(f'记忆文件 {name} 读取失败：{error}')
                continue
            if not content:
                continue
            source = provenance.get(uri, {})
            updated_at = str(
                source.get('extracted_at')
                or item.get('modTime')
                or item.get('updated_at')
                or item.get('modified_at')
                or ''
            )
            digest = hashlib.sha256(
                f'{agent.id}:{uri}'.encode('utf-8')
            ).hexdigest()[:24]
            result.append(
                {
                    'id': f'ov-{digest}',
                    'uri': uri,
                    'title': _memory_title(
                        content,
                        memory_type,
                        name,
                        source_title=str(source.get('title') or ''),
                    ),
                    'content': content,
                    'memory_type': memory_type,
                    'memory_type_label': TYPE_LABELS[memory_type],
                    'group': TYPE_TO_GROUP[memory_type],
                    'owner_agent_id': agent.agentId,
                    'owner_agent_pk': str(agent.id),
                    'owner_agent_name': agent.name or agent.agentId,
                    'updated_at': updated_at,
                    'summary': _memory_summary(content),
                    'source': source or None,
                }
            )
        return result


def _message_ids(source_ref: str) -> tuple[str, ...]:
    if not str(source_ref or '').startswith('messages:'):
        return ()
    return tuple(
        value
        for value in str(source_ref).removeprefix('messages:').split(',')
        if value
    )


def _memory_type(namespace: MemoryNamespace, uri: str) -> str:
    relative = uri.removeprefix(namespace.root_uri).strip('/')
    first = relative.split('/', 1)[0] if relative else ''
    if '/' not in relative and first.endswith('.md'):
        first = PurePosixPath(first).stem
    return first


def _normalize_memory_uri(
    namespace: MemoryNamespace,
    raw_uri: str,
    memory_type: str,
) -> str:
    value = str(raw_uri or '').strip()
    if value.startswith(namespace.root_uri):
        return value
    marker = '/memories/'
    if marker in value:
        return namespace.root_uri + value.split(marker, 1)[1].lstrip('/')
    parts = [part for part in value.replace('\\', '/').split('/') if part]
    if memory_type in parts:
        return namespace.root_uri + '/'.join(parts[parts.index(memory_type):])
    name = PurePosixPath(value).name
    if memory_type in DISPLAY_MEMORY_TYPES and name:
        if name == f'{memory_type}.md':
            return namespace.root_uri + name
        return f'{namespace.root_uri}{memory_type}/{name}'
    return ''


def _memory_title(
    content: str,
    memory_type: str,
    filename: str,
    *,
    source_title: str = '',
) -> str:
    filename_title = _filename_title(filename)
    headings = []
    for line in content.splitlines():
        stripped = line.strip()
        match = re.match(r'^#{1,3}\s+(.+?)\s*$', stripped)
        if match and not _is_generic_title(match.group(1)):
            headings.append(match.group(1).strip())
    candidates = [*headings]
    if filename_title:
        candidates.append(filename_title)
    if source_title and not _is_generic_title(source_title):
        candidates.append(source_title.strip())
    for line in content.splitlines():
        stripped = line.strip(' -*\t')
        if (
            stripped
            and not stripped.startswith(('---', '<!--'))
            and not _is_generic_title(stripped)
        ):
            candidates.append(stripped)
            break
    if _contains_han(content) or _contains_han(source_title):
        chinese_candidate = next(
            (candidate for candidate in candidates if _contains_han(candidate)),
            '',
        )
        if chinese_candidate:
            return chinese_candidate[:120]
    if candidates:
        return candidates[0][:120]
    return TYPE_LABELS.get(memory_type, memory_type)


def _filename_title(filename: str) -> str:
    stem = PurePosixPath(filename).stem
    stem = re.sub(r'_?\d{8,14}$', '', stem)
    title = re.sub(r'[_-]+', ' ', stem).strip()
    return '' if _is_generic_title(title) else title


def _is_generic_title(value: str) -> bool:
    normalized = re.sub(r'[^\w\u4e00-\u9fff]+', ' ', str(value or '').casefold()).strip()
    return not normalized or normalized in GENERIC_TITLES


def _contains_han(value: object) -> bool:
    return bool(re.search(r'[\u3400-\u4dbf\u4e00-\u9fff]', str(value or '')))


def _memory_summary(content: str, limit: int = MEMORY_SUMMARY_LIMIT) -> str:
    """Return a compact preview without Markdown headings or private metadata."""

    value = re.sub(r'<!--\s*MEMORY_FIELDS[\s\S]*?-->', '', str(content or ''), flags=re.I)
    value = re.sub(r'```(?:json|markdown|md)?', '', value, flags=re.I)
    try:
        structured = json.loads(value.strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        structured = None
    if isinstance(structured, (dict, list)):
        return ''
    lines = []
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line or line == '---' or re.match(r'^#{1,6}\s+', line):
            continue
        line = re.sub(r'^\s*(?:[-*+]|\d+[.)])\s+', '', line)
        line = re.sub(r'[`*_~]+', '', line).strip()
        if line:
            lines.append(line)
    compact = re.sub(r'\s+', ' ', ' '.join(lines)).strip()
    if not compact:
        return ''
    sentence = re.match(r'^.*?[。！？!?](?:\s|$)', compact)
    preview = sentence.group(0).strip() if sentence else compact
    characters = list(preview)
    return preview if len(characters) <= limit else ''.join(characters[:limit]) + '…'


def _agent_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the lightweight Agent overview consumed by the canvas side panel."""

    sorted_entries = sorted(
        entries,
        key=lambda item: str(item.get('updated_at') or ''),
        reverse=True,
    )
    group_counts = {group['key']: 0 for group in MEMORY_GROUPS}
    type_counts: Counter[str] = Counter()
    for entry in entries:
        group = str(entry.get('group') or '')
        if group in group_counts:
            group_counts[group] += 1
        memory_types = entry.get('memory_types')
        if not isinstance(memory_types, list) or not memory_types:
            memory_types = [entry.get('memory_type')]
        type_counts.update(
            str(memory_type)
            for memory_type in memory_types
            if str(memory_type or '').strip()
        )

    recent_memories = []
    for entry in sorted_entries[:RECENT_MEMORY_LIMIT]:
        recent_memories.append(
            {
                'id': entry.get('id'),
                'title': entry.get('title'),
                'summary': entry.get('summary') or _memory_summary(entry.get('content', '')),
                'memory_type': entry.get('memory_type'),
                'memory_type_label': entry.get('memory_type_label'),
                'memory_types': entry.get('memory_types') or [entry.get('memory_type')],
                'memory_type_labels': entry.get('memory_type_labels')
                or [entry.get('memory_type_label')],
                'group': entry.get('group'),
                'updated_at': entry.get('updated_at'),
            }
        )
    return {
        'memory_count': len(entries),
        'group_counts': group_counts,
        'type_counts': dict(type_counts),
        'last_updated_at': str(sorted_entries[0].get('updated_at') or '')
        if sorted_entries
        else '',
        'recent_memories': recent_memories,
    }


def _task_family_key(entry: dict[str, Any]) -> str:
    name = PurePosixPath(str(entry.get('uri') or '')).stem
    name = re.sub(r'_?\d{8,14}$', '', name)
    normalized = re.sub(r'[^\w\u4e00-\u9fff]+', '', name.casefold())
    normalized = re.sub(
        r'(起草|撰写|编写|生成|制作|创建|处理|执行|分析|总结)$',
        '',
        normalized,
    )
    if normalized:
        return normalized
    return re.sub(r'[^\w\u4e00-\u9fff]+', '', str(entry.get('title') or '').casefold())


def _aggregate_task_memories(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse one task's case/trajectory/experience files into one UI record."""
    order = {memory_type: index for index, memory_type in enumerate(TASK_MEMORY_TYPES)}
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    result = []
    for entry in entries:
        if entry.get('memory_type') not in TASK_MEMORY_TYPES:
            entry['memory_types'] = [entry['memory_type']]
            entry['components'] = [_memory_component(entry)]
            result.append(entry)
            continue
        source = entry.get('source') if isinstance(entry.get('source'), dict) else {}
        source_key = str(source.get('task_id') or entry.get('updated_at') or '')
        buckets[(source_key, _task_family_key(entry))].append(entry)

    for bucket_entries in buckets.values():
        bucket_entries.sort(key=lambda item: order.get(item['memory_type'], 99))
        case_entry = next(
            (item for item in bucket_entries if item['memory_type'] == 'cases'),
            None,
        )
        primary = case_entry or bucket_entries[0]
        memory_types = list(dict.fromkeys(item['memory_type'] for item in bucket_entries))
        digest = hashlib.sha256(
            '|'.join(sorted(str(item['uri']) for item in bucket_entries)).encode('utf-8')
        ).hexdigest()[:24]
        combined = {
            **primary,
            'id': f'ov-bundle-{digest}',
            'title': case_entry['title'] if case_entry is not None else primary['title'],
            'memory_types': memory_types,
            'memory_type_labels': [TYPE_LABELS[value] for value in memory_types],
            'components': [_memory_component(item) for item in bucket_entries],
            'updated_at': max(str(item.get('updated_at') or '') for item in bucket_entries),
        }
        result.append(combined)
    return result


def _memory_component(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        'memory_type': entry['memory_type'],
        'memory_type_label': entry.get('memory_type_label'),
        'title': entry.get('title'),
        'content': entry.get('content'),
        'uri': entry.get('uri'),
        'updated_at': entry.get('updated_at'),
    }
