"""Minimal OpenViking HTTP client migrated into the unified runtime."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import PurePosixPath
import time
from typing import Any
from urllib.parse import quote

from django.core.cache import cache
from django.conf import settings
import requests

from .errors import (
    MemoryConfigurationError,
    OpenVikingAuthenticationError,
    OpenVikingResponseError,
    OpenVikingUnavailable,
)
from .namespaces import MemoryNamespace
from .provisioning import ProvisioningError, read_credential


class OpenVikingClient:
    """Call only the content and retrieval APIs needed by MemoryGateway."""

    MEMORY_TREE_CACHE_SECONDS = 60
    NATIVE_MEMORY_TYPES = frozenset({
        'profile',
        'preferences',
        'entities',
        'events',
        'cases',
        'trajectories',
        'experiences',
    })

    def __init__(self, base_url: str, api_key: str = '', timeout: int = 30, session=None):
        self.base_url = str(base_url or '').rstrip('/')
        self.api_key = str(api_key or '')
        self.timeout = max(int(timeout), 1)
        self.session = session or requests.Session()
        if not self.base_url:
            raise MemoryConfigurationError('OpenViking endpoint is not configured.')

    @classmethod
    def from_settings(cls):
        return cls(
            base_url=settings.OPENVIKING_BASE_URL,
            api_key='',
            timeout=settings.OPENVIKING_TIMEOUT,
        )

    def search(self, namespace: MemoryNamespace, query: str, limit: int) -> list[dict[str, Any]]:
        payload = self._request(
            'POST',
            '/api/v1/search/search',
            namespace,
            json={
                'query': query,
                'context_type': 'memory',
                'target_uri': namespace.root_uri,
                'limit': limit,
                'include_provenance': False,
            },
        )
        result = payload.get('result') if isinstance(payload, dict) else None
        memories = result.get('memories', []) if isinstance(result, dict) else []
        return [item for item in memories if isinstance(item, dict)]

    def search_memory_tree(
        self,
        namespace: MemoryNamespace,
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Search every native long-term-memory leaf, excluding legacy raw archives.

        OpenViking's ``target_uri`` search is not recursive across nested memory
        directories. Native extraction can place a preference at paths such as
        ``preferences/user/称呼偏好.md`` while a search targeting ``memories/``
        only sees direct children. Discover the current tree first, search each
        directory that owns a native memory file, and then allow only the files
        found in that authoritative tree.
        """
        cache_key = self._memory_tree_cache_key(namespace)
        listing = cache.get(cache_key)
        if not isinstance(listing, list):
            listing = self.list_directory(
                namespace,
                namespace.root_uri,
                recursive=True,
                node_limit=2000,
            )
            cache.set(cache_key, listing, timeout=self.MEMORY_TREE_CACHE_SECONDS)
        eligible_uris: set[str] = set()
        authoritative_uris: set[str] = set()
        search_target_uris: set[str] = set()
        for item in listing:
            if bool(item.get('isDir') or item.get('is_dir')):
                continue
            uri = str(item.get('uri') or item.get('path') or '').strip()
            if not uri.startswith(namespace.root_uri) or not uri.endswith('.md'):
                continue
            relative = uri.removeprefix(namespace.root_uri).strip('/')
            first = relative.split('/', 1)[0] if relative else ''
            memory_type = (
                PurePosixPath(first).stem
                if '/' not in relative and first.endswith('.md')
                else first
            )
            if memory_type not in self.NATIVE_MEMORY_TYPES:
                continue
            eligible_uris.add(uri)
            if memory_type in {'profile', 'preferences'}:
                authoritative_uris.add(uri)
            else:
                search_target_uris.add(f"{uri.rsplit('/', 1)[0]}/")

        if not eligible_uris:
            return []

        by_uri: dict[str, dict[str, Any]] = {}
        fresh_content: dict[str, str] = {}

        def search_target(target_uri: str) -> list[dict[str, Any]]:
            target_namespace = MemoryNamespace(
                scope=namespace.scope,
                account_id=namespace.account_id,
                user_id=namespace.user_id,
                root_uri=target_uri,
                credential_ref=namespace.credential_ref,
            )
            return self.search(target_namespace, query, limit)

        targets = sorted(search_target_uris)
        authoritative = sorted(authoritative_uris)
        # Read current profile/preferences and search the semantic directories
        # concurrently. This keeps current-state guarantees without adding the
        # two network phases together on every turn.
        work_count = len(targets) + len(authoritative)
        with ThreadPoolExecutor(max_workers=min(work_count, 6)) as executor:
            authoritative_futures = {
                uri: executor.submit(self.read_content, namespace, uri)
                for uri in authoritative
            }
            search_futures = [
                executor.submit(search_target, target_uri)
                for target_uri in targets
            ]
            for uri, future in authoritative_futures.items():
                content = future.result().strip()
                if not content:
                    continue
                fresh_content[uri] = content
                by_uri[uri] = {
                    'context_type': 'memory',
                    'uri': uri,
                    'score': 1.0,
                    'category': PurePosixPath(
                        uri.removeprefix(namespace.root_uri)
                    ).parts[0],
                    'match_reason': 'authoritative_profile_or_preference',
                    'abstract': content,
                    'overview': None,
                }
            for future in search_futures:
                target_items = future.result()
                for item in target_items:
                    uri = str(item.get('uri') or '').strip()
                    if uri not in eligible_uris:
                        continue
                    current = by_uri.get(uri)
                    if current is None or float(item.get('score') or 0.0) > float(
                        current.get('score') or 0.0
                    ):
                        by_uri[uri] = item
        selected = sorted(
            by_uri.values(),
            key=lambda item: float(item.get('score') or 0.0),
            reverse=True,
        )[:limit]
        if not selected:
            return []

        # Search results locate candidate URIs, but their abstracts may lag
        # behind an update. Hydrate only the final top-k in parallel from the
        # authoritative content API so latency is bounded by roughly one read
        # round trip rather than N sequential reads.
        uris = [
            str(item.get('uri') or '')
            for item in selected
            if str(item.get('uri') or '') not in fresh_content
        ]
        if uris:
            with ThreadPoolExecutor(max_workers=min(len(uris), 6)) as executor:
                fresh_content.update(dict(executor.map(
                    lambda uri: (uri, self.read_content(namespace, uri).strip()),
                    uris,
                )))
        hydrated = []
        for item in selected:
            uri = str(item.get('uri') or '')
            content = fresh_content.get(uri, '')
            if not content:
                # The file was deleted or became unavailable after discovery;
                # never inject the stale search abstract.
                continue
            hydrated.append({
                **item,
                'content': content,
                'abstract': content,
            })
        return hydrated

    @staticmethod
    def _memory_tree_cache_key(namespace: MemoryNamespace) -> str:
        digest = hashlib.sha256(
            f'{namespace.account_id}:{namespace.user_id}:{namespace.root_uri}'.encode('utf-8')
        ).hexdigest()
        return f'openviking:memory-tree:{digest}'

    def invalidate_memory_tree(self, namespace: MemoryNamespace) -> None:
        """Invalidate shared discovery data after a native commit changes the tree."""
        cache.delete(self._memory_tree_cache_key(namespace))

    def list_directory(
        self,
        namespace: MemoryNamespace,
        uri: str,
        *,
        recursive: bool = False,
        node_limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """List one authorized Agent memory tree through OpenViking's native FS API."""
        self._assert_authorized_uri(namespace, uri, require_memory_root=True)
        payload = self._request(
            'GET',
            '/api/v1/fs/ls',
            namespace,
            allow_not_found=True,
            params={
                'uri': uri,
                'recursive': str(bool(recursive)).lower(),
                'output': 'original',
                'show_all_hidden': 'false',
                'node_limit': min(max(int(node_limit), 1), 2000),
            },
        )
        result = payload.get('result') if isinstance(payload, dict) else None
        if isinstance(result, dict) and result.get('not_found'):
            return []
        if not isinstance(result, list):
            raise OpenVikingResponseError('OpenViking returned an invalid directory listing.')
        return [item for item in result if isinstance(item, dict)]

    def read_content(self, namespace: MemoryNamespace, uri: str) -> str:
        """Read visible content from one authorized memory or session archive file."""
        self._assert_authorized_uri(namespace, uri)
        payload = self._request(
            'GET',
            '/api/v1/content/read',
            namespace,
            allow_not_found=True,
            params={'uri': uri, 'raw': 'false'},
        )
        result = payload.get('result') if isinstance(payload, dict) else None
        if isinstance(result, dict) and result.get('not_found'):
            return ''
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            for key in ('content', 'text', 'body'):
                value = result.get(key)
                if isinstance(value, str):
                    return value
        raise OpenVikingResponseError('OpenViking returned invalid file content.')

    def write_memory(
        self,
        namespace: MemoryNamespace,
        *,
        content: str,
        idempotency_key: str,
        source_message_ids: tuple[object, ...],
    ) -> str:
        digest = hashlib.sha256(
            f'{namespace.scope}:{idempotency_key}'.encode('utf-8')
        ).hexdigest()[:32]
        uri = f'{namespace.root_uri}runtime-{digest}.md'
        document = self._format_document(content, source_message_ids)
        self._request(
            'POST',
            '/api/v1/content/write',
            namespace,
            allow_conflict=True,
            json={
                'uri': uri,
                'content': document,
                'mode': 'create',
                'wait': True,
                'timeout': self.timeout,
            },
        )
        return uri

    def create_native_session(
        self,
        namespace: MemoryNamespace,
        session_id: str,
        *,
        peer_memory_only: bool = False,
        memory_types: tuple[str, ...] | None = None,
    ) -> bool:
        """Create an extraction session; return False for an idempotent replay."""
        memory_policy = {
            'self': {'enabled': not peer_memory_only},
            'peer': {'enabled': peer_memory_only},
            'working_memory': {'enabled': True},
        }
        if memory_types is not None:
            memory_policy['memory_types'] = list(memory_types)
        payload = self._request(
            'POST',
            '/api/v1/sessions',
            namespace,
            allow_conflict=True,
            json={
                'session_id': session_id,
                'memory_policy': memory_policy,
            },
        )
        result = payload.get('result') if isinstance(payload, dict) else None
        return not bool(isinstance(result, dict) and result.get('idempotent_replay'))

    def delete_native_session(self, namespace: MemoryNamespace, session_id: str) -> None:
        self._request(
            'DELETE',
            f'/api/v1/sessions/{quote(session_id, safe="")}',
            namespace,
            allow_not_found=True,
        )

    def add_native_session_messages(
        self,
        namespace: MemoryNamespace,
        session_id: str,
        *,
        messages: tuple[dict[str, str], ...],
        turn_id: str,
        source_message_ids: tuple[object, ...],
        peer_id: str = '',
    ) -> None:
        source_ids = [str(value) for value in source_message_ids]
        native_messages = []
        for message in messages:
            role = str(message.get('role') or '').strip().lower()
            content = str(message.get('content') or '').strip()
            if role not in {'user', 'assistant'} or not content:
                continue
            native_message = {
                'role': role,
                'content': content,
                'turn_id': turn_id,
                'message_kind': 'user_query' if role == 'user' else 'assistant_step',
                'source_message_ids': source_ids,
            }
            if role == 'user' and peer_id:
                native_message['peer_id'] = peer_id
            native_messages.append(native_message)
        if not native_messages:
            raise OpenVikingResponseError('Native memory commit has no valid messages.')
        self._request(
            'POST',
            f'/api/v1/sessions/{quote(session_id, safe="")}/messages/batch',
            namespace,
            json={'messages': native_messages},
        )

    def commit_native_session(self, namespace: MemoryNamespace, session_id: str) -> dict[str, Any]:
        payload = self._request(
            'POST',
            f'/api/v1/sessions/{quote(session_id, safe="")}/commit',
            namespace,
            json={'keep_recent_count': 0},
        )
        result = payload.get('result') if isinstance(payload, dict) else None
        if not isinstance(result, dict) or not result.get('task_id'):
            raise OpenVikingResponseError('OpenViking did not return a session commit task.')
        return result

    def get_native_task(self, namespace: MemoryNamespace, task_id: str) -> dict[str, Any]:
        payload = self._request(
            'GET',
            f'/api/v1/tasks/{quote(task_id, safe="")}',
            namespace,
        )
        result = payload.get('result') if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            raise OpenVikingResponseError('OpenViking returned an invalid task response.')
        return result

    def list_native_session_tasks(
        self,
        namespace: MemoryNamespace,
        session_id: str,
    ) -> list[dict[str, Any]]:
        payload = self._request(
            'GET',
            '/api/v1/tasks',
            namespace,
            params={
                'task_type': 'session_commit',
                'resource_id': session_id,
                'limit': 20,
            },
        )
        result = payload.get('result') if isinstance(payload, dict) else None
        if not isinstance(result, list):
            raise OpenVikingResponseError('OpenViking returned an invalid task list.')
        return [item for item in result if isinstance(item, dict)]

    def add_resource(
        self,
        namespace: MemoryNamespace,
        *,
        source_url: str,
        target_uri: str,
    ) -> dict[str, Any]:
        """Index a short-lived COS URL at a deterministic tenant resource URI."""
        payload = self._request(
            'POST',
            '/api/v1/resources',
            namespace,
            allow_conflict=True,
            json={
                'path': source_url,
                'to': target_uri,
                'wait': True,
                'timeout': self.timeout,
            },
        )
        result = payload.get('result') if isinstance(payload, dict) else None
        if isinstance(result, dict):
            return result
        # Re-adding the same checksum may return HTTP 409. The deterministic
        # target already represents the completed idempotent operation.
        if payload.get('status') == 'ok':
            return {'status': 'success', 'root_uri': target_uri}
        raise OpenVikingResponseError('OpenViking returned an invalid resource response.')

    def search_resources(
        self,
        namespace: MemoryNamespace,
        *,
        query: str,
        target_uri: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Search one backend-authorized resource tree."""
        payload = self._request(
            'POST',
            '/api/v1/search/search',
            namespace,
            json={
                'query': query,
                'context_type': 'resource',
                'target_uri': target_uri,
                'limit': limit,
                'include_provenance': False,
            },
        )
        result = payload.get('result') if isinstance(payload, dict) else None
        resources = result.get('resources', []) if isinstance(result, dict) else []
        return [item for item in resources if isinstance(item, dict)]

    def _request(
        self,
        method: str,
        path: str,
        namespace: MemoryNamespace,
        *,
        allow_conflict: bool = False,
        allow_not_found: bool = False,
        **kwargs,
    ) -> dict[str, Any]:
        try:
            api_key = self.api_key or read_credential(namespace.credential_ref)
        except ProvisioningError as exc:
            raise MemoryConfigurationError('OpenViking runtime credential is unavailable.') from exc
        headers = {
            'X-API-Key': api_key,
            'X-OpenViking-Account': namespace.account_id,
            'X-OpenViking-User': namespace.user_id,
        }
        self._check_tenant_rate(namespace.account_id)
        with self._tenant_concurrency_slot(namespace.account_id):
            try:
                response = self.session.request(
                    method,
                    f'{self.base_url}{path}',
                    headers=headers,
                    timeout=self.timeout,
                    **kwargs,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                raise OpenVikingUnavailable('OpenViking is temporarily unavailable.') from exc
        if allow_conflict and response.status_code == 409:
            return {'status': 'ok', 'result': {'idempotent_replay': True}}
        payload = self._decode_json(response)
        error = payload.get('error') if isinstance(payload, dict) else None
        if allow_not_found and (
            response.status_code == 404
            or (isinstance(error, dict) and error.get('code') == 'NOT_FOUND')
        ):
            return {'status': 'ok', 'result': {'not_found': True}}
        self._raise_for_response(response.status_code, payload)
        return payload

    @staticmethod
    def _assert_authorized_uri(
        namespace: MemoryNamespace,
        uri: str,
        *,
        require_memory_root: bool = False,
    ) -> None:
        normalized = str(uri or '').strip()
        allowed_root = namespace.root_uri if require_memory_root else f'viking://user/{namespace.user_id}/'
        if not normalized.startswith(allowed_root):
            raise MemoryConfigurationError('OpenViking URI is outside the authorized Agent namespace.')

    @staticmethod
    def _tenant_cache_prefix(account_id: str) -> str:
        digest = hashlib.sha256(str(account_id).encode('utf-8')).hexdigest()[:24]
        return f'openviking:tenant:{digest}'

    def _check_tenant_rate(self, account_id: str) -> None:
        limit = max(int(settings.OPENVIKING_TENANT_RATE_LIMIT_PER_MINUTE), 1)
        bucket = int(time.time() // 60)
        key = f'{self._tenant_cache_prefix(account_id)}:rate:{bucket}'
        try:
            if cache.add(key, 1, timeout=70):
                count = 1
            else:
                count = cache.incr(key)
        except Exception as exc:
            raise OpenVikingUnavailable('OpenViking tenant limiter is unavailable.') from exc
        if count > limit:
            raise OpenVikingUnavailable('OpenViking tenant rate limit exceeded.')

    @contextmanager
    def _tenant_concurrency_slot(self, account_id: str):
        maximum = max(int(settings.OPENVIKING_TENANT_MAX_CONCURRENT), 1)
        prefix = f'{self._tenant_cache_prefix(account_id)}:concurrency'
        token = hashlib.sha256(f'{time.time_ns()}:{id(self)}'.encode()).hexdigest()
        acquired = None
        try:
            for slot in range(maximum):
                key = f'{prefix}:{slot}'
                if cache.add(key, token, timeout=self.timeout + 10):
                    acquired = key
                    break
        except Exception as exc:
            raise OpenVikingUnavailable('OpenViking tenant limiter is unavailable.') from exc
        if acquired is None:
            raise OpenVikingUnavailable('OpenViking tenant concurrency limit exceeded.')
        try:
            yield
        finally:
            try:
                if cache.get(acquired) == token:
                    cache.delete(acquired)
            except Exception:
                pass

    @staticmethod
    def _decode_json(response) -> dict[str, Any]:
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise OpenVikingResponseError('OpenViking returned an invalid response.') from exc
        if not isinstance(payload, dict):
            raise OpenVikingResponseError('OpenViking returned an invalid response.')
        return payload

    @staticmethod
    def _raise_for_response(status_code: int, payload: dict[str, Any]) -> None:
        if status_code in {401, 403}:
            raise OpenVikingAuthenticationError('OpenViking authentication failed.')
        if status_code == 429 or status_code >= 500:
            raise OpenVikingUnavailable('OpenViking is temporarily unavailable.')
        if status_code >= 400 or payload.get('status') == 'error':
            raise OpenVikingResponseError('OpenViking rejected the memory operation.')

    @staticmethod
    def _format_document(content: str, source_message_ids: tuple[object, ...]) -> str:
        source_refs = ', '.join(str(value) for value in source_message_ids)
        return '\n'.join(
            [
                '# Agent Runtime Memory',
                '',
                content.strip(),
                '',
                f'Source messages: {source_refs}',
            ]
        )
