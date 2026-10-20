"""Authorization, isolation, degradation, and commit tests for MemoryGateway."""

from unittest.mock import Mock, patch

from asgiref.sync import async_to_sync
from django.test import TestCase

from apps.agent_runtime.contracts import CommitRequest, MemoryItem, RecallRequest
from apps.agent_runtime.memory.errors import (
    MemoryPermissionDenied,
    OpenVikingTaskPending,
    OpenVikingUnavailable,
)
from apps.agent_runtime.memory.gateway import OpenVikingMemoryGateway
from apps.agent_runtime.memory.namespaces import NamespaceResolver
from apps.agent_runtime.models import MemorySyncJob
from apps.company.models import (
    TransparentCompanyAgentPermissionBinding,
    TransparentCompanyAgentTestMessage,
    TransparentCompanyAgentTestSession,
)

from .memory_fixtures import create_memory_fixture


class FakeOpenVikingClient:
    def __init__(self):
        self.search_calls = []
        self.write_calls = []
        self.native_session_calls = []
        self.search_error = None
        self.return_wrong_namespace = False
        self.task_status = 'completed'

    def search(self, namespace, query, limit):
        self.search_calls.append((namespace, query, limit))
        if self.search_error:
            raise self.search_error
        root = 'viking://user/other/memories/' if self.return_wrong_namespace else namespace.root_uri
        return [{
            'uri': f'{root}{namespace.scope}.md',
            'abstract': f'{namespace.scope} memory',
            'score': 0.8,
        }]

    def write_memory(self, namespace, **kwargs):
        self.write_calls.append((namespace, kwargs))
        return f'{namespace.root_uri}written.md'

    def create_native_session(self, namespace, session_id, **kwargs):
        self.native_session_calls.append(('create', namespace, session_id, kwargs))
        return True

    def list_native_session_tasks(self, namespace, session_id):
        self.native_session_calls.append(('list_tasks', namespace, session_id))
        return []

    def delete_native_session(self, namespace, session_id):
        self.native_session_calls.append(('delete', namespace, session_id))

    def add_native_session_messages(self, namespace, session_id, **kwargs):
        self.native_session_calls.append(('add_messages', namespace, session_id, kwargs))

    def commit_native_session(self, namespace, session_id):
        self.native_session_calls.append(('commit', namespace, session_id))
        return {
            'task_id': 'native-task-1',
            'archive_uri': f'viking://user/{namespace.user_id}/sessions/{session_id}/history/archive_001',
        }

    def get_native_task(self, namespace, task_id):
        self.native_session_calls.append(('get_task', namespace, task_id))
        return {
            'task_id': task_id,
            'status': self.task_status,
            'result': {},
        }


class MemoryGatewayTests(TestCase):
    def setUp(self):
        self.data = create_memory_fixture()
        self.client = FakeOpenVikingClient()
        self.dispatched = []
        self.gateway = OpenVikingMemoryGateway(
            client=self.client,
            dispatcher=lambda job_id, payload: self.dispatched.append((job_id, payload)),
        )
        self.source_messages = (
            self._message('user', 'remember this'),
            self._message('assistant', 'acknowledged'),
        )

    def test_memory_sync_job_matches_frozen_schema(self):
        self.assertEqual(MemorySyncJob._meta.get_field('job_type').max_length, 32)
        self.assertEqual(MemorySyncJob._meta.get_field('idempotency_key').max_length, 255)
        self.assertEqual(MemorySyncJob._meta.get_field('source_ref').max_length, 1024)
        self.assertEqual(MemorySyncJob._meta.get_field('external_task_id').max_length, 255)

    def test_namespace_isolates_company_agent_and_user(self):
        resolver = NamespaceResolver()
        context_a1 = self._context('company_a', 'agent_a1', 'session_a1', 'owner')
        context_a2 = self._context('company_a', 'agent_a2', 'session_a2', 'owner')
        context_b1 = self._context('company_b', 'agent_b1', 'session_b1', 'owner')

        namespace_a = resolver.for_scope(context_a1, 'company')
        namespace_b = resolver.for_scope(context_b1, 'company')
        self.assertNotEqual(
            (namespace_a.account_id, namespace_a.root_uri),
            (namespace_b.account_id, namespace_b.root_uri),
        )
        self.assertNotEqual(
            resolver.for_scope(context_a1, 'agent').root_uri,
            resolver.for_scope(context_a2, 'agent').root_uri,
        )
        self.assertEqual(
            resolver.for_scope(context_a1, 'user').root_uri,
            resolver.for_scope(context_a2, 'user').root_uri,
        )

        second_session = TransparentCompanyAgentTestSession.objects.create(
            transparentCompany=self.data['agent_a1'].transparentCompany,
            agent=self.data['agent_a1'],
            operator=self.data['second_user'],
            conversationId='phase2-second-user-session',
            created_at='2026-08-12',
            updated_at='2026-08-12',
        )
        second_context = resolver.resolve_context(
            company_id=self.data['company_a'].id,
            agent_id=self.data['agent_a1'].id,
            user_id=self.data['second_user'].id,
            session_id=second_session.id,
        )
        self.assertNotEqual(
            resolver.for_scope(context_a1, 'user').root_uri,
            resolver.for_scope(second_context, 'user').root_uri,
        )

    def test_legacy_user_policy_does_not_enable_runtime_user_scope(self):
        context_a1 = self._context('company_a', 'agent_a1', 'session_a1', 'owner')
        access = self.gateway.access_resolver.resolve(context_a1)

        self.assertFalse(access.user_read)
        self.assertFalse(access.user_write)
        self.assertEqual(access.user_scope, 'disabled')
        with self.assertRaises(MemoryPermissionDenied):
            self.gateway.access_resolver.authorize_recall(context_a1, ('user',))

    def test_public_peer_namespace_isolates_visitors_and_agents(self):
        resolver = NamespaceResolver()
        context_a1 = self._public_context('agent_a1', self.data['owner'], 'public-owner')
        context_a2 = self._public_context('agent_a1', self.data['second_user'], 'public-second')
        context_other_agent = self._public_context(
            'agent_a2', self.data['owner'], 'public-owner-other-agent'
        )

        namespace_a1 = resolver.for_public_peer(context_a1)
        namespace_a2 = resolver.for_public_peer(context_a2)
        namespace_other_agent = resolver.for_public_peer(context_other_agent)

        self.assertEqual(namespace_a1, resolver.for_public_peer(context_a1))
        self.assertNotEqual(namespace_a1.root_uri, namespace_a2.root_uri)
        self.assertNotEqual(namespace_a1.root_uri, namespace_other_agent.root_uri)
        self.assertRegex(
            namespace_a1.root_uri,
            rf'^viking://user/{context_a1.agent_user_id}/peers/visitor-[0-9a-f]{{24}}/memories/$',
        )

    def test_cross_company_context_is_rejected_before_transport(self):
        request = self._recall_request()
        request = RecallRequest(
            **{**request.__dict__, 'company_id': self.data['company_b'].id}
        )
        with self.assertRaises(MemoryPermissionDenied):
            async_to_sync(self.gateway.recall)(request)
        self.assertEqual(self.client.search_calls, [])

    def test_recall_authorizes_each_scope_and_ranks_results(self):
        items = async_to_sync(self.gateway.recall)(self._recall_request())

        self.assertEqual({item.scope for item in items}, {'agent', 'session'})
        self.assertEqual(len(self.client.search_calls), 2)
        for namespace, _, _ in self.client.search_calls:
            self.assertTrue(namespace.root_uri.startswith(f'viking://user/{namespace.user_id}/'))

    def test_pending_preference_update_overrides_committed_recall_without_waiting(self):
        user_message = self._message('user', '以后叫我董事长')
        assistant_message = self._message('assistant', '收到，以后称呼您为董事长。')
        job = MemorySyncJob.objects.create(
            company=self.data['company_a'],
            agent=self.data['agent_a1'],
            user=self.data['owner'],
            job_type=MemorySyncJob.TYPE_MEMORY_COMMIT,
            idempotency_key='pending-chairman-preference',
            source_ref=f'messages:{user_message.id},{assistant_message.id}',
            status=MemorySyncJob.STATUS_PENDING,
        )

        items = async_to_sync(self.gateway.recall)(self._recall_request())

        self.assertEqual(items[0].scope, 'pending_update')
        self.assertIn('以后叫我董事长', items[0].content)
        self.assertIn('以本条更新为准', items[0].content)

        job.status = MemorySyncJob.STATUS_COMPLETED
        job.save(update_fields=['status'])
        items = async_to_sync(self.gateway.recall)(self._recall_request())
        self.assertNotIn('pending_update', {item.scope for item in items})

    def test_public_pending_overlay_is_isolated_to_current_visitor(self):
        owner_context = self._public_context(
            'agent_a1', self.data['owner'], 'public-pending-owner'
        )
        other_context = self._public_context(
            'agent_a1', self.data['second_user'], 'public-pending-other'
        )
        user_message = self._message_for_session(
            other_context.session, 'user', '以后叫我访客二号'
        )
        assistant_message = self._message_for_session(
            other_context.session, 'assistant', '收到。'
        )
        MemorySyncJob.objects.create(
            company=self.data['company_a'],
            agent=self.data['agent_a1'],
            user=self.data['second_user'],
            job_type=MemorySyncJob.TYPE_MEMORY_COMMIT,
            idempotency_key='pending-other-visitor-preference',
            source_ref=f'messages:{user_message.id},{assistant_message.id}',
            status=MemorySyncJob.STATUS_PENDING,
        )
        request = RecallRequest(
            run_id='public-pending-run',
            company_id=self.data['company_a'].id,
            agent_id=self.data['agent_a1'].id,
            user_id=self.data['owner'].id,
            session_id=owner_context.session.id,
            query='应该怎么称呼我？',
            scopes=('agent',),
            top_k=10,
        )

        items = async_to_sync(self.gateway.recall)(request)

        self.assertNotIn('pending_update', {item.scope for item in items})

    def test_public_recall_searches_only_current_visitor_peer_memory(self):
        context = self._public_context('agent_a1', self.data['owner'], 'public-recall')
        request = RecallRequest(
            run_id='public-run',
            company_id=self.data['company_a'].id,
            agent_id=self.data['agent_a1'].id,
            user_id=self.data['owner'].id,
            session_id=context.session.id,
            query='what did this visitor say?',
            scopes=('agent', 'session', 'user'),
            top_k=10,
        )

        items = async_to_sync(self.gateway.recall)(request)

        self.assertEqual(len(items), 1)
        self.assertEqual(len(self.client.search_calls), 1)
        namespace, _, limit = self.client.search_calls[0]
        self.assertEqual(namespace, NamespaceResolver.for_public_peer(context))
        self.assertEqual(limit, 10)
        self.assertNotEqual(
            namespace.root_uri,
            NamespaceResolver().for_scope(context, 'agent').root_uri,
        )

    def test_network_failure_degrades_to_empty_memory(self):
        self.client.search_error = OpenVikingUnavailable('network down')

        self.assertEqual(async_to_sync(self.gateway.recall)(self._recall_request()), [])

    def test_permission_failure_does_not_degrade(self):
        TransparentCompanyAgentPermissionBinding.objects.filter(
            agent=self.data['agent_a1']
        ).update(memoryRead=False)

        with self.assertRaises(MemoryPermissionDenied):
            async_to_sync(self.gateway.recall)(self._recall_request())
        self.assertEqual(self.client.search_calls, [])

    def test_provider_result_is_revalidated_against_namespace(self):
        self.client.return_wrong_namespace = True

        with self.assertRaises(MemoryPermissionDenied):
            async_to_sync(self.gateway.recall)(self._recall_request())

    def test_commit_is_queued_once_and_processed_idempotently(self):
        request = self._commit_request('phase2-idempotent-key')
        async_to_sync(self.gateway.commit)(request)
        async_to_sync(self.gateway.commit)(request)

        self.assertEqual(MemorySyncJob.objects.count(), 1)
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self.dispatched[0][1]['schemaVersion'], 3)
        job = MemorySyncJob.objects.get()
        self.gateway.process_commit_job(job, self.dispatched[0][1])
        job.refresh_from_db()
        self.assertEqual(job.status, MemorySyncJob.STATUS_COMPLETED)
        self.assertEqual(job.external_task_id, 'native-task-1')
        self.assertTrue(job.external_session_id.startswith('tsds-memory-'))
        self.assertEqual(self.client.write_calls, [])
        self.assertEqual(
            [call[0] for call in self.client.native_session_calls],
            ['create', 'add_messages', 'commit', 'get_task'],
        )
        added = self.client.native_session_calls[1][3]['messages']
        self.assertEqual([message['role'] for message in added], ['user', 'assistant'])
        create_kwargs = self.client.native_session_calls[0][3]
        self.assertFalse(create_kwargs['peer_memory_only'])
        self.assertIn('cases', create_kwargs['memory_types'])
        self.assertNotIn('trajectories', create_kwargs['memory_types'])
        self.assertNotIn('experiences', create_kwargs['memory_types'])
        self.assertTrue(
            {'identity', 'soul', 'skills', 'tools'}.isdisjoint(create_kwargs['memory_types'])
        )
        self.assertEqual(self.client.native_session_calls[1][3]['peer_id'], '')

    def test_public_commit_extracts_only_current_visitor_peer_memory(self):
        context = self._public_context('agent_a1', self.data['owner'], 'public-commit')
        messages = (
            self._message_for_session(context.session, 'user', 'call me boss'),
            self._message_for_session(context.session, 'assistant', 'understood'),
        )
        request = CommitRequest(
            run_id='public-run',
            company_id=self.data['company_a'].id,
            agent_id=self.data['agent_a1'].id,
            user_id=self.data['owner'].id,
            session_id=context.session.id,
            source_message_ids=tuple(message.id for message in messages),
            content='visitor preference',
            idempotency_key='public-peer-commit',
            messages=(
                {'role': 'user', 'content': 'call me boss'},
                {'role': 'assistant', 'content': 'understood'},
            ),
        )

        async_to_sync(self.gateway.commit)(request)
        job = MemorySyncJob.objects.get(idempotency_key='public-peer-commit')
        self.gateway.process_commit_job(job, self.dispatched[-1][1])

        peer_id = NamespaceResolver.public_peer_id(context)
        create_kwargs = self.client.native_session_calls[0][3]
        self.assertTrue(create_kwargs['peer_memory_only'])
        self.assertIn('preferences', create_kwargs['memory_types'])
        added_kwargs = self.client.native_session_calls[1][3]
        self.assertEqual(added_kwargs['peer_id'], peer_id)
        self.assertEqual(
            [message.get('peer_id') for message in added_kwargs['messages']],
            [None, None],
        )

    def test_native_commit_stays_pending_until_extraction_completes(self):
        request = self._commit_request('phase2-native-pending')
        async_to_sync(self.gateway.commit)(request)
        job = MemorySyncJob.objects.get(idempotency_key='phase2-native-pending')
        self.client.task_status = 'running'

        with self.assertRaises(OpenVikingTaskPending):
            self.gateway.process_commit_job(job, self.dispatched[-1][1])

        job.refresh_from_db()
        self.assertNotEqual(job.status, MemorySyncJob.STATUS_COMPLETED)
        self.assertEqual(job.external_task_id, 'native-task-1')

    def test_multistep_prompt_enables_trajectory_extraction(self):
        request = self._commit_request('multistep-memory-policy')
        request = CommitRequest(
            **{
                **request.__dict__,
                'messages': (
                    {'role': 'user', 'content': '1. 查询资料\n2. 生成报告'},
                    {'role': 'assistant', 'content': '报告已生成'},
                ),
            }
        )
        memory_types = self.gateway._memory_types_for_commit(request)
        self.assertIn('cases', memory_types)
        self.assertIn('trajectories', memory_types)

    def test_greeting_is_rejected_before_a_sync_job_is_created(self):
        request = CommitRequest(
            **{
                **self._commit_request('greeting-is-not-memory').__dict__,
                'content': 'User: 你好\nAssistant: 你好，我是公司的客服。',
                'messages': (
                    {'role': 'user', 'content': '你好'},
                    {'role': 'assistant', 'content': '你好，我是公司的客服。'},
                ),
            }
        )

        async_to_sync(self.gateway.commit)(request)

        self.assertFalse(
            MemorySyncJob.objects.filter(idempotency_key='greeting-is-not-memory').exists()
        )
        self.assertEqual(self.dispatched, [])

    def test_preference_whitelist_does_not_open_unrelated_schemas(self):
        request = CommitRequest(
            **{
                **self._commit_request('preference-types-only').__dict__,
                'messages': (
                    {'role': 'user', 'content': '以后请称呼我为老板'},
                    {'role': 'assistant', 'content': '好的，以后我会称呼您为老板。'},
                ),
                'memory_types': ('preferences', 'profile'),
            }
        )

        self.assertEqual(
            self.gateway._memory_types_for_commit(request),
            ('preferences', 'profile'),
        )

    def test_only_completed_tool_calls_enable_trajectory_extraction(self):
        request = CommitRequest(
            **{
                **self._commit_request('successful-tools-only').__dict__,
                'messages': (
                    {'role': 'user', 'content': '帮我查询客户订单并整理退款情况'},
                    {'role': 'assistant', 'content': '已经整理完成退款订单和客户通知情况。'},
                ),
                'memory_types': ('cases',),
            }
        )
        run = Mock()
        completed_calls = Mock()
        run.tool_calls.filter.return_value = completed_calls

        with patch(
            'apps.agent_runtime.memory.gateway.AgentRun.objects.filter'
        ) as run_filter:
            run_filter.return_value.first.return_value = run
            completed_calls.exists.return_value = False
            self.assertNotIn('trajectories', self.gateway._memory_types_for_commit(request))

            completed_calls.exists.return_value = True
            self.assertIn('trajectories', self.gateway._memory_types_for_commit(request))

    def test_recall_collapses_task_memory_variants(self):
        items = [
            MemoryItem(
                memory_id='viking://user/a/memories/cases/退款致歉邮件起草.md',
                scope='agent',
                content='case',
                score=0.7,
                source_ref=None,
            ),
            MemoryItem(
                memory_id='viking://user/a/memories/trajectories/退款致歉邮件撰写_20260824082349.md',
                scope='agent',
                content='trajectory',
                score=0.9,
                source_ref=None,
            ),
        ]
        result = self.gateway._rank_and_deduplicate(items, 10)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].content, 'trajectory')

    def test_commit_payload_rejects_missing_or_unknown_schema_version(self):
        payload = self.gateway.serialize_commit_request(self._commit_request('schema-key'))
        payload.pop('schemaVersion')
        with self.assertRaises(ValueError):
            self.gateway.deserialize_commit_request(payload)

        payload['schemaVersion'] = 4
        with self.assertRaises(ValueError):
            self.gateway.deserialize_commit_request(payload)

    def test_idempotency_key_cannot_cross_company_boundary(self):
        async_to_sync(self.gateway.commit)(self._commit_request('shared-key'))
        other_message = TransparentCompanyAgentTestMessage.objects.create(
            session=self.data['session_b1'],
            role='user',
            content='company B source',
            created_at='2026-08-12',
            updated_at='2026-08-12',
        )
        other = CommitRequest(
            run_id='run-b',
            company_id=self.data['company_b'].id,
            agent_id=self.data['agent_b1'].id,
            user_id=self.data['owner'].id,
            session_id=self.data['session_b1'].id,
            source_message_ids=(other_message.id,),
            content='company B memory',
            idempotency_key='shared-key',
        )

        with self.assertRaises(MemoryPermissionDenied):
            async_to_sync(self.gateway.commit)(other)

    def _context(self, company, agent, session, user):
        return NamespaceResolver().resolve_context(
            company_id=self.data[company].id,
            agent_id=self.data[agent].id,
            user_id=self.data[user].id,
            session_id=self.data[session].id,
        )

    def _public_context(self, agent_key, user, conversation_id):
        agent = self.data[agent_key]
        session = TransparentCompanyAgentTestSession.objects.create(
            transparentCompany=agent.transparentCompany,
            agent=agent,
            operator=user,
            conversationId=conversation_id,
            source=TransparentCompanyAgentTestSession.SOURCE_PUBLIC_CLIENT_ENTRY,
            created_at='2026-08-12',
            updated_at='2026-08-12',
        )
        return NamespaceResolver().resolve_context(
            company_id=agent.transparentCompany.company_id,
            agent_id=agent.id,
            user_id=user.id,
            session_id=session.id,
        )

    def _recall_request(self):
        return RecallRequest(
            run_id='run-a',
            company_id=self.data['company_a'].id,
            agent_id=self.data['agent_a1'].id,
            user_id=self.data['owner'].id,
            session_id=self.data['session_a1'].id,
            query='what should be remembered?',
            scopes=('agent', 'session'),
            top_k=10,
        )

    def _commit_request(self, idempotency_key):
        return CommitRequest(
            run_id='run-a',
            company_id=self.data['company_a'].id,
            agent_id=self.data['agent_a1'].id,
            user_id=self.data['owner'].id,
            session_id=self.data['session_a1'].id,
            source_message_ids=tuple(message.id for message in self.source_messages),
            content='durable memory',
            idempotency_key=idempotency_key,
            messages=(
                {'role': 'user', 'content': 'remember this'},
                {'role': 'assistant', 'content': 'acknowledged'},
            ),
        )

    def _message(self, role, content):
        return self._message_for_session(self.data['session_a1'], role, content)

    @staticmethod
    def _message_for_session(session, role, content):
        return TransparentCompanyAgentTestMessage.objects.create(
            session=session,
            role=role,
            content=content,
            created_at='2026-08-12',
            updated_at='2026-08-12',
        )
