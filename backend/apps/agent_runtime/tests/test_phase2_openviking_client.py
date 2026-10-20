"""Wire-level tests for the migrated OpenViking HTTP client."""

import requests
from unittest.mock import Mock
from django.core.cache import cache
from django.test import SimpleTestCase

from apps.agent_runtime.memory.client import OpenVikingClient
from apps.agent_runtime.memory.errors import (
    OpenVikingAuthenticationError,
    OpenVikingUnavailable,
)
from apps.agent_runtime.memory.namespaces import MemoryNamespace


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload or {'status': 'ok', 'result': {}}

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error:
            raise self.error
        return self.response


class OpenVikingClientTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.namespace = MemoryNamespace(
            scope='agent',
            account_id='tm-account',
            user_id='agent-user',
            root_uri='viking://user/agent-user/memories/',
        )

    def test_search_sends_server_calculated_identity_and_target(self):
        response = FakeResponse(payload={
            'status': 'ok',
            'result': {'memories': [{'uri': f'{self.namespace.root_uri}one.md'}]},
        })
        session = FakeSession(response=response)
        client = OpenVikingClient('http://openviking:1933', 'server-secret', session=session)

        result = client.search(self.namespace, 'query', 5)

        self.assertEqual(len(result), 1)
        _, url, kwargs = session.calls[0]
        self.assertEqual(url, 'http://openviking:1933/api/v1/search/search')
        self.assertEqual(kwargs['headers']['X-OpenViking-Account'], 'tm-account')
        self.assertEqual(kwargs['headers']['X-OpenViking-User'], 'agent-user')
        self.assertEqual(kwargs['json']['target_uri'], self.namespace.root_uri)

    def test_memory_tree_search_reaches_nested_native_files_and_excludes_legacy_archives(self):
        client = OpenVikingClient('http://openviking:1933', 'server-secret')
        preference_uri = f'{self.namespace.root_uri}preferences/user/称呼偏好.md'
        legacy_uri = f'{self.namespace.root_uri}runtime-old.md'
        client.list_directory = Mock(return_value=[
            {'uri': preference_uri, 'name': '称呼偏好.md', 'isDir': False},
            {'uri': legacy_uri, 'name': 'runtime-old.md', 'isDir': False},
        ])
        client.read_content = Mock(return_value='希望被称呼为“董事长”。')
        client.search = Mock(return_value=[
            {'uri': legacy_uri, 'abstract': '称呼为老板', 'score': 1.0},
        ])

        result = client.search_memory_tree(self.namespace, '应该怎么称呼我', 5)

        self.assertEqual([item['uri'] for item in result], [preference_uri])
        self.assertEqual(result[0]['abstract'], '希望被称呼为“董事长”。')
        client.search.assert_not_called()

    def test_memory_tree_search_hydrates_latest_file_instead_of_stale_abstract(self):
        client = OpenVikingClient('http://openviking:1933', 'server-secret')
        case_uri = f'{self.namespace.root_uri}cases/report.md'
        client.list_directory = Mock(return_value=[
            {'uri': case_uri, 'name': 'report.md', 'isDir': False},
        ])
        client.search = Mock(return_value=[{
            'uri': case_uri,
            'abstract': '旧版摘要',
            'score': 0.9,
        }])
        client.read_content = Mock(return_value='OpenViking 文件中的最新正文')

        result = client.search_memory_tree(self.namespace, '报告', 5)

        self.assertEqual(result[0]['content'], 'OpenViking 文件中的最新正文')
        self.assertEqual(result[0]['abstract'], 'OpenViking 文件中的最新正文')
        client.read_content.assert_called_once_with(self.namespace, case_uri)

    def test_memory_tree_search_drops_file_deleted_after_search(self):
        client = OpenVikingClient('http://openviking:1933', 'server-secret')
        event_uri = f'{self.namespace.root_uri}events/launch.md'
        client.list_directory = Mock(return_value=[
            {'uri': event_uri, 'name': 'launch.md', 'isDir': False},
        ])
        client.search = Mock(return_value=[{
            'uri': event_uri,
            'abstract': '已经删除的旧摘要',
            'score': 0.9,
        }])
        client.read_content = Mock(return_value='')

        self.assertEqual(client.search_memory_tree(self.namespace, '发布', 5), [])

    def test_memory_tree_discovery_cache_is_shared_and_explicitly_invalidated(self):
        client = OpenVikingClient('http://openviking:1933', 'server-secret')
        preference_uri = f'{self.namespace.root_uri}preferences/user/language.md'
        client.list_directory = Mock(return_value=[
            {'uri': preference_uri, 'name': 'language.md', 'isDir': False},
        ])
        client.search = Mock(return_value=[])
        client.read_content = Mock(return_value='优先使用中文。')

        client.search_memory_tree(self.namespace, '回复语言', 5)
        client.search_memory_tree(self.namespace, '回复语言', 5)
        self.assertEqual(client.list_directory.call_count, 1)

        client.invalidate_memory_tree(self.namespace)
        client.search_memory_tree(self.namespace, '回复语言', 5)
        self.assertEqual(client.list_directory.call_count, 2)

    def test_write_uri_is_deterministic_for_retry(self):
        session = FakeSession()
        client = OpenVikingClient('http://openviking:1933', 'server-secret', session=session)

        first = client.write_memory(
            self.namespace,
            content='remember me',
            idempotency_key='same-key',
            source_message_ids=(1,),
        )
        second = client.write_memory(
            self.namespace,
            content='remember me',
            idempotency_key='same-key',
            source_message_ids=(1,),
        )

        self.assertEqual(first, second)
        self.assertEqual(session.calls[0][2]['json']['mode'], 'create')

    def test_conflict_is_treated_as_idempotent_replay(self):
        session = FakeSession(response=FakeResponse(status_code=409))
        client = OpenVikingClient('http://openviking:1933', 'server-secret', session=session)

        uri = client.write_memory(
            self.namespace,
            content='remember me',
            idempotency_key='same-key',
            source_message_ids=(1,),
        )
        self.assertTrue(uri.startswith(self.namespace.root_uri))

    def test_native_session_creation_uses_self_memory_without_peer_memory(self):
        session = FakeSession(response=FakeResponse(payload={
            'status': 'ok',
            'result': {'session_id': 'session-1'},
        }))
        client = OpenVikingClient('http://openviking:1933', 'server-secret', session=session)

        self.assertTrue(client.create_native_session(self.namespace, 'session-1'))

        method, url, kwargs = session.calls[0]
        self.assertEqual(method, 'POST')
        self.assertEqual(url, 'http://openviking:1933/api/v1/sessions')
        self.assertEqual(kwargs['json']['memory_policy']['self']['enabled'], True)
        self.assertEqual(kwargs['json']['memory_policy']['peer']['enabled'], False)

    def test_public_native_session_uses_peer_memory_without_self_memory(self):
        session = FakeSession(response=FakeResponse(payload={
            'status': 'ok',
            'result': {'session_id': 'session-1'},
        }))
        client = OpenVikingClient('http://openviking:1933', 'server-secret', session=session)

        self.assertTrue(client.create_native_session(
            self.namespace,
            'session-1',
            peer_memory_only=True,
        ))

        policy = session.calls[0][2]['json']['memory_policy']
        self.assertEqual(policy['self']['enabled'], False)
        self.assertEqual(policy['peer']['enabled'], True)

    def test_native_session_batch_preserves_roles_and_source_ids(self):
        session = FakeSession()
        client = OpenVikingClient('http://openviking:1933', 'server-secret', session=session)

        client.add_native_session_messages(
            self.namespace,
            'session-1',
            messages=(
                {'role': 'user', 'content': 'remember blue'},
                {'role': 'assistant', 'content': 'noted'},
            ),
            turn_id='turn-1',
            source_message_ids=(11, 12),
        )

        _, url, kwargs = session.calls[0]
        self.assertTrue(url.endswith('/api/v1/sessions/session-1/messages/batch'))
        messages = kwargs['json']['messages']
        self.assertEqual([item['role'] for item in messages], ['user', 'assistant'])
        self.assertEqual(messages[0]['source_message_ids'], ['11', '12'])
        self.assertEqual(messages[1]['turn_id'], 'turn-1')

    def test_public_native_batch_assigns_peer_only_to_user_messages(self):
        session = FakeSession()
        client = OpenVikingClient('http://openviking:1933', 'server-secret', session=session)

        client.add_native_session_messages(
            self.namespace,
            'session-1',
            messages=(
                {'role': 'user', 'content': 'call me boss'},
                {'role': 'assistant', 'content': 'understood'},
            ),
            turn_id='turn-1',
            source_message_ids=(11, 12),
            peer_id='visitor-0123456789abcdef01234567',
        )

        messages = session.calls[0][2]['json']['messages']
        self.assertEqual(messages[0]['peer_id'], 'visitor-0123456789abcdef01234567')
        self.assertNotIn('peer_id', messages[1])

    def test_native_commit_requires_provider_task_id(self):
        session = FakeSession(response=FakeResponse(payload={
            'status': 'ok',
            'result': {
                'task_id': 'task-1',
                'archive_uri': 'viking://user/agent-user/sessions/session-1/history/archive_001',
            },
        }))
        client = OpenVikingClient('http://openviking:1933', 'server-secret', session=session)

        result = client.commit_native_session(self.namespace, 'session-1')

        self.assertEqual(result['task_id'], 'task-1')
        self.assertEqual(session.calls[0][2]['json'], {'keep_recent_count': 0})

    def test_network_error_is_retryable_unavailable(self):
        session = FakeSession(error=requests.ConnectionError('connection refused'))
        client = OpenVikingClient('http://openviking:1933', 'server-secret', session=session)

        with self.assertRaises(OpenVikingUnavailable):
            client.search(self.namespace, 'query', 5)

    def test_authentication_error_is_not_network_degradation(self):
        session = FakeSession(response=FakeResponse(status_code=403))
        client = OpenVikingClient('http://openviking:1933', 'server-secret', session=session)

        with self.assertRaises(OpenVikingAuthenticationError):
            client.search(self.namespace, 'query', 5)
