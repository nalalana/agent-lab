"""Focused security regressions for Agent runtime receipts and retries."""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from apps.agent_runtime.config.resolver import ConfigResolver
from apps.agent_runtime.knowledge.tasks import (
    MAX_KNOWLEDGE_INDEX_ATTEMPTS,
    retry_failed_knowledge_jobs,
)
from apps.agent_runtime.models import MemorySyncJob
from apps.agent_runtime.orchestration.nodes import (
    MEMORY_COMMIT_PENDING_MESSAGE,
    UNVERIFIED_TOOL_CLAIM_MESSAGE,
    _enforce_runtime_truthfulness,
    _tool_call_receipts,
)
from apps.agent_runtime.runtime import UnifiedRuntimeService
from apps.agent_runtime.tests.memory_fixtures import create_memory_fixture
from apps.agent_runtime.tools.registry import default_tool_registry
from apps.runtime.security import sanitize_tool_calls


class RuntimeTruthfulnessTests(SimpleTestCase):
    @staticmethod
    def _state(tool_results=None):
        return {
            "run_id": "run-1",
            "sequence": 0,
            "events": [],
            "tool_results": list(tool_results or []),
        }

    def test_unverified_tool_success_claim_is_replaced(self):
        state = self._state()

        content = _enforce_runtime_truthfulness(
            state,
            "已执行 python.execute，结果为 28907。",
        )

        self.assertEqual(content, UNVERIFIED_TOOL_CLAIM_MESSAGE)
        self.assertEqual(len(state["events"]), 1)

    def test_memory_success_claim_is_replaced_with_pending_language(self):
        state = self._state()

        content = _enforce_runtime_truthfulness(
            state,
            "你的偏好已写入长期记忆。",
        )

        self.assertEqual(content, MEMORY_COMMIT_PENDING_MESSAGE)
        self.assertEqual(len(state["events"]), 1)

    def test_successful_tool_receipt_allows_execution_claim(self):
        state = self._state(
            [
                {
                    "tool_call_id": "tool-1",
                    "model_call_id": "model-1",
                    "name": "python.execute",
                    "success": True,
                    "content": "sensitive result",
                }
            ]
        )

        content = _enforce_runtime_truthfulness(
            state,
            "已执行 python.execute。",
        )

        self.assertEqual(content, "已执行 python.execute。")

    def test_public_receipts_include_ids_but_not_arguments_or_results(self):
        state = self._state(
            [
                {
                    "tool_call_id": "tool-1",
                    "model_call_id": "model-1",
                    "name": "workspace.read",
                    "success": True,
                    "content": "secret workspace content",
                    "arguments": {"logical_path": "knowledge/private.txt"},
                }
            ]
        )

        receipts = _tool_call_receipts(state)
        sanitized = sanitize_tool_calls(receipts)

        self.assertEqual(
            sanitized,
            [
                {
                    "name": "workspace.read",
                    "status": "completed",
                    "tool_call_id": "tool-1",
                    "model_call_id": "model-1",
                }
            ],
        )
        self.assertNotIn("content", sanitized[0])
        self.assertNotIn("arguments", sanitized[0])

    def test_exact_denied_write_scope_does_not_expose_publish_tool(self):
        service = UnifiedRuntimeService.__new__(UnifiedRuntimeService)
        service.tool_registry = default_tool_registry()

        permissions = service._tool_permissions(
            {
                "knowledge": {
                    "read": ["knowledge/test1"],
                    "write": ["knowledge/test1"],
                    "delete": [],
                    "deny": ["knowledge/test1"],
                }
            }
        )

        self.assertNotIn("tool.workspace.write", permissions)
        self.assertNotIn("tool.workspace.read", permissions)


class RuntimePrivacyDefaultsTests(SimpleTestCase):
    def test_session_memory_pii_redaction_cannot_be_disabled(self):
        binding = SimpleNamespace(
            enabled=True,
            status="active",
            memoryScope="session",
            maxTokensOverride=None,
            summaryStyleOverride="",
        )

        policy = ConfigResolver._resolve_session_memory(binding)

        self.assertTrue(policy["redactPii"])


class KnowledgeRetryLimitTests(TestCase):
    def test_exhausted_failed_job_is_not_automatically_requeued(self):
        fixture = create_memory_fixture("security-retry-limit")
        job = MemorySyncJob.objects.create(
            company=fixture["company_a"],
            job_type=MemorySyncJob.TYPE_KNOWLEDGE_INDEX,
            idempotency_key="security-retry-limit",
            source_ref="workspace_file:999999",
            status=MemorySyncJob.STATUS_FAILED,
            attempts=MAX_KNOWLEDGE_INDEX_ATTEMPTS,
        )

        with patch(
            "apps.agent_runtime.knowledge.tasks.index_knowledge_file.delay"
        ) as delay:
            result = retry_failed_knowledge_jobs.run(limit=100)

        self.assertEqual(result["queued"], 0)
        delay.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, MemorySyncJob.STATUS_FAILED)
