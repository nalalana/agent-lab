from types import SimpleNamespace
from unittest import TestCase

from apps.agent_runtime.memory.bank import (
    _agent_summary,
    _aggregate_task_memories,
    _memory_summary,
    _memory_title,
    _memory_type,
    _normalize_memory_uri,
)
from apps.agent_runtime.memory.namespaces import MemoryNamespace


class MemoryBankHelpersTests(TestCase):
    def setUp(self):
        self.namespace = MemoryNamespace(
            scope='agent',
            account_id='company-1',
            user_id='agent-1',
            root_uri='viking://user/agent-1/memories/',
            credential_ref='secret-ref',
        )

    def test_maps_memory_types_from_root_files_and_directories(self):
        self.assertEqual(
            _memory_type(self.namespace, f'{self.namespace.root_uri}profile.md'),
            'profile',
        )
        self.assertEqual(
            _memory_type(self.namespace, f'{self.namespace.root_uri}events/launch.md'),
            'events',
        )

    def test_normalizes_memory_diff_uri_to_agent_root(self):
        self.assertEqual(
            _normalize_memory_uri(
                self.namespace,
                'memory/user/agent-1/events/launch.md',
                'events',
            ),
            f'{self.namespace.root_uri}events/launch.md',
        )

    def test_uses_markdown_heading_as_title(self):
        self.assertEqual(
            _memory_title('# PostgreSQL 迁移\n\n迁移已经完成。', 'events', 'event.md'),
            'PostgreSQL 迁移',
        )

    def test_task_title_prefers_filename_over_generic_heading(self):
        self.assertEqual(
            _memory_title('## Situation\n\n退款已经完成。', 'experiences', '退款致歉邮件撰写.md'),
            '退款致歉邮件撰写',
        )

    def test_rejects_foreign_filename_when_case_content_is_chinese(self):
        self.assertEqual(
            _memory_title(
                '# 图片内容描述\n\n## Task Signature\n分析用户发送的图片',
                'cases',
                'Descrição de imagem.md',
                source_title='描述图片内容',
            ),
            '图片内容描述',
        )

    def test_uses_chinese_source_title_when_heading_is_also_foreign(self):
        self.assertEqual(
            _memory_title(
                '# Descrição de imagem\n\n分析用户发送的图片',
                'cases',
                'Descrição de imagem.md',
                source_title='描述图片内容',
            ),
            '描述图片内容',
        )

    def test_builds_plain_text_summary_from_memory_markdown(self):
        self.assertEqual(
            _memory_summary(
                '# 用户偏好\n\n- 默认使用 TypeScript。\n'
                '<!-- MEMORY_FIELDS {"private": true} -->\n'
            ),
            '默认使用 TypeScript。',
        )

    def test_hides_structured_task_memory_fields_and_values(self):
        self.assertEqual(
            _memory_summary(
                '{"summary":"用户要求起草退款致歉邮件。",'
                '"precondition":"客户已提交退款申请。"}'
            ),
            '',
        )

    def test_builds_agent_summary_with_group_counts_and_recent_memories(self):
        entries = [
            {
                'id': 'older',
                'title': '用户偏好',
                'content': '# 用户偏好\n\n偏好中文回复。',
                'summary': '偏好中文回复。',
                'memory_type': 'preferences',
                'memory_type_label': '用户偏好',
                'group': 'user',
                'updated_at': '2026-08-20T08:00:00+08:00',
            },
            {
                'id': 'newer',
                'title': '供应商事项',
                'content': '# 供应商事项\n\n供应商 A 已完成复核。',
                'summary': '供应商 A 已完成复核。',
                'memory_type': 'events',
                'memory_type_label': '事项与事件',
                'group': 'people_events',
                'updated_at': '2026-08-21T09:00:00+08:00',
            },
        ]

        summary = _agent_summary(entries)

        self.assertEqual(summary['memory_count'], 2)
        self.assertEqual(summary['group_counts']['user'], 1)
        self.assertEqual(summary['group_counts']['people_events'], 1)
        self.assertEqual(summary['group_counts']['task_experience'], 0)
        self.assertEqual(summary['last_updated_at'], '2026-08-21T09:00:00+08:00')
        self.assertEqual(
            [item['id'] for item in summary['recent_memories']],
            ['newer', 'older'],
        )

    def test_collapses_related_task_types_into_one_entry(self):
        source = {'task_id': 'job-1', 'title': '起草退款致歉邮件'}
        common = {
            'group': 'task_experience',
            'owner_agent_id': 'mira',
            'updated_at': '2026-08-24T08:24:00',
            'source': source,
        }
        entries = [
            {
                **common,
                'id': 'case',
                'uri': f'{self.namespace.root_uri}cases/退款致歉邮件起草.md',
                'title': '退款致歉邮件起草',
                'content': 'case content',
                'memory_type': 'cases',
                'memory_type_label': '任务案例',
            },
            {
                **common,
                'id': 'trajectory',
                'uri': f'{self.namespace.root_uri}trajectories/退款致歉邮件撰写_20260824082349.md',
                'title': '退款致歉邮件撰写',
                'content': 'trajectory content',
                'memory_type': 'trajectories',
                'memory_type_label': '执行轨迹',
            },
        ]
        result = _aggregate_task_memories(entries)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['title'], '退款致歉邮件起草')
        self.assertEqual(result[0]['memory_types'], ['cases', 'trajectories'])
        self.assertEqual(len(result[0]['components']), 2)
