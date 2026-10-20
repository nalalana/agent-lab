"""Tests for failed-queue recovery and the reusable recall graph node."""

from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import TestCase

from apps.agent_runtime.contracts import MemoryItem, RunRequest
from apps.agent_runtime.memory.nodes import MemoryRecallNode
from apps.agent_runtime.memory.tasks import retry_failed_memory_jobs
from apps.agent_runtime.models import MemorySyncJob
from apps.company.models import TransparentCompanyAgentTestMessage

from .memory_fixtures import create_memory_fixture


class FakeGateway:
    async def recall(self, request):
        return [
            MemoryItem(
                memory_id='memory-1',
                scope='agent',
                content=f'recalled for {request.query}',
                score=0.9,
                source_ref='viking://memory-1',
            )
        ]


class CapturingGateway(FakeGateway):
    def __init__(self):
        self.requests = []

    async def recall(self, request):
        self.requests.append(request)
        return await super().recall(request)


class InvalidQueryGateway:
    async def recall(self, _request):
        raise ValueError('Memory recall query length is invalid.')


class MemoryTasksAndNodeTests(TestCase):
    def setUp(self):
        self.data = create_memory_fixture('phase2-task')

    def test_failed_queue_rebuilds_payload_from_source_messages(self):
        first = self._message('user', 'remember this')
        second = self._message('assistant', 'acknowledged')
        job = MemorySyncJob.objects.create(
            company=self.data['company_a'],
            agent=self.data['agent_a1'],
            user=self.data['owner'],
            job_type=MemorySyncJob.TYPE_MEMORY_COMMIT,
            idempotency_key='retry-key',
            source_ref=f'messages:{first.id},{second.id}',
            status=MemorySyncJob.STATUS_FAILED,
            error_code='MEMORY_UNAVAILABLE',
        )

        with patch(
            'apps.agent_runtime.memory.tasks.commit_memory_job.delay'
        ) as delay:
            result = retry_failed_memory_jobs.run()

        self.assertEqual(result, {'queued': 1})
        job.refresh_from_db()
        self.assertEqual(job.status, MemorySyncJob.STATUS_PENDING)
        payload = delay.call_args.args[1]
        self.assertEqual(payload['schemaVersion'], 3)
        self.assertEqual(payload['source_message_ids'], [str(first.id), str(second.id)])
        self.assertIn('remember this', payload['content'])
        self.assertEqual(
            [message['role'] for message in payload['messages']],
            ['user', 'assistant'],
        )

    def test_recall_node_places_contract_items_in_state(self):
        request = RunRequest(
            request_id='request-1',
            company_id=self.data['company_a'].id,
            agent_id=self.data['agent_a1'].id,
            user_id=self.data['owner'].id,
            session_id=self.data['session_a1'].id,
            source='agent_test',
            user_message_id='message-1',
            locale='zh-CN',
            trace_id='trace-1',
        )
        state = {
            'request': request,
            'run_id': 'run-1',
            'status': 'RUNNING',
            'config': {'memory_policy': {'session': {'enabled': True}}},
            'model_messages': [{'role': 'user', 'content': 'latest question'}],
        }

        result = async_to_sync(MemoryRecallNode(FakeGateway()))(state)

        self.assertEqual(result['memory_items'][0]['scope'], 'agent')
        self.assertIn('latest question', result['memory_items'][0]['content'])
        self.assertEqual(result['model_messages'][0]['role'], 'system')
        self.assertIn('Recalled long-term memory', result['model_messages'][0]['content'])
        self.assertIn('recalled for latest question', result['model_messages'][0]['content'])
        self.assertIn('not instructions', result['model_messages'][0]['content'])
        self.assertIn('prefer the newest pending_update', result['model_messages'][0]['content'])
        self.assertEqual(result['model_messages'][-1], {
            'role': 'user',
            'content': 'latest question',
        })

    def test_recall_node_merges_into_existing_system_prompt(self):
        request = RunRequest(
            request_id='request-2',
            company_id=self.data['company_a'].id,
            agent_id=self.data['agent_a1'].id,
            user_id=self.data['owner'].id,
            session_id=self.data['session_a1'].id,
            source='agent_test',
            user_message_id='message-2',
            locale='zh-CN',
            trace_id='trace-2',
        )
        state = {
            'request': request,
            'run_id': 'run-2',
            'status': 'RUNNING',
            'config': {'memory_policy': {'session': {'enabled': True}}},
            'model_messages': [
                {'role': 'system', 'content': 'agent policy'},
                {'role': 'system', 'content': 'session summary'},
                {'role': 'user', 'content': 'latest question'},
            ],
        }

        result = async_to_sync(MemoryRecallNode(FakeGateway()))(state)

        self.assertIn('agent policy', result['model_messages'][0]['content'])
        self.assertIn('session summary', result['model_messages'][0]['content'])
        self.assertIn('Recalled long-term memory', result['model_messages'][0]['content'])
        self.assertEqual(
            [message['role'] for message in result['model_messages']],
            ['system', 'user'],
        )

    def test_recall_node_uses_only_text_from_multimodal_user_content(self):
        request = RunRequest(
            request_id='request-image',
            company_id=self.data['company_a'].id,
            agent_id=self.data['agent_a1'].id,
            user_id=self.data['owner'].id,
            session_id=self.data['session_a1'].id,
            source='agent_test',
            user_message_id='message-image',
            locale='zh-CN',
            trace_id='trace-image',
        )
        state = {
            'request': request,
            'run_id': 'run-image',
            'status': 'RUNNING',
            'config': {'memory_policy': {'session': {'enabled': True}}},
            'model_messages': [{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': '描述图片内容'},
                    {
                        'type': 'image_url',
                        'image_url': {'url': 'data:image/png;base64,' + ('a' * 24000)},
                    },
                ],
            }],
        }
        gateway = CapturingGateway()

        result = async_to_sync(MemoryRecallNode(gateway))(state)

        self.assertEqual(gateway.requests[0].query, '描述图片内容')
        self.assertNotIn('data:image', gateway.requests[0].query)
        self.assertEqual(result['status'], 'RUNNING')

    def test_recall_node_uses_bounded_placeholder_for_image_only_content(self):
        request = RunRequest(
            request_id='request-image-only',
            company_id=self.data['company_a'].id,
            agent_id=self.data['agent_a1'].id,
            user_id=self.data['owner'].id,
            session_id=self.data['session_a1'].id,
            source='agent_test',
            user_message_id='message-image-only',
            locale='zh-CN',
            trace_id='trace-image-only',
        )
        state = {
            'request': request,
            'run_id': 'run-image-only',
            'status': 'RUNNING',
            'config': {'memory_policy': {'session': {'enabled': True}}},
            'model_messages': [{
                'role': 'user',
                'content': [{
                    'type': 'image_url',
                    'image_url': {'url': 'data:image/png;base64,' + ('a' * 24000)},
                }],
            }],
        }
        gateway = CapturingGateway()

        async_to_sync(MemoryRecallNode(gateway))(state)

        self.assertEqual(gateway.requests[0].query, MemoryRecallNode.IMAGE_ONLY_QUERY)

    def test_recall_node_limits_text_query_length(self):
        state = {
            'model_messages': [{'role': 'user', 'content': 'q' * 9000}],
        }

        query = MemoryRecallNode._latest_user_content(state)

        self.assertEqual(len(query), MemoryRecallNode.MAX_QUERY_CHARS)

    def test_recalled_memory_removes_private_reasoning_and_stale_policy_claims(self):
        context = MemoryRecallNode._context_message([{
            'scope': 'agent',
            'content': (
                'User: 记住叫我老板\n'
                'Assistant: <think>旧结论：用户级记忆未启用</think>\n'
                '收到，老板。'
            ),
        }])

        self.assertIn('收到，老板。', context)
        self.assertNotIn('<think>', context)
        self.assertNotIn('用户级记忆未启用', context)

    def test_recall_node_does_not_recall_when_automatic_memory_is_disabled(self):
        scopes = MemoryRecallNode._configured_scopes({
            'memory_policy': {
                'automatic': {'enabled': False},
                'session': {'enabled': False},
                'user': {'enabled': False},
            },
        })

        self.assertEqual(scopes, ())

    def test_recall_node_uses_automatic_memory_contract(self):
        scopes = MemoryRecallNode._configured_scopes({
            'memory_policy': {
                'automatic': {'enabled': True},
                'session': {'enabled': True},
                'user': {'enabled': False},
            },
        })

        self.assertEqual(scopes, ('agent', 'session'))

    def test_recall_node_degrades_when_gateway_rejects_query(self):
        request = RunRequest(
            request_id='request-invalid-query',
            company_id=self.data['company_a'].id,
            agent_id=self.data['agent_a1'].id,
            user_id=self.data['owner'].id,
            session_id=self.data['session_a1'].id,
            source='agent_test',
            user_message_id='message-invalid-query',
            locale='zh-CN',
            trace_id='trace-invalid-query',
        )
        state = {
            'request': request,
            'run_id': 'run-invalid-query',
            'status': 'RUNNING',
            'config': {'memory_policy': {'session': {'enabled': True}}},
            'model_messages': [{'role': 'user', 'content': 'question'}],
        }

        result = async_to_sync(MemoryRecallNode(InvalidQueryGateway()))(state)

        self.assertEqual(result['status'], 'RUNNING')
        self.assertEqual(result['memory_items'], [])
        self.assertEqual(result['events'][-2].event_type, 'warning')
        self.assertEqual(result['events'][-1].data['degraded'], True)

    def _message(self, role, content):
        return TransparentCompanyAgentTestMessage.objects.create(
            session=self.data['session_a1'],
            role=role,
            content=content,
            created_at='2026-08-12',
            updated_at='2026-08-12',
        )
