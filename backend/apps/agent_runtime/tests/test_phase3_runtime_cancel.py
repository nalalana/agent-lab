"""Phase 3-A Run cancellation API tests."""

from __future__ import annotations

import json

from django.test import RequestFactory, TestCase

from apps.agent_runtime.enums import RunEventType
from apps.agent_runtime.models import AgentConfigSnapshot, AgentRun, AgentRunEvent
from apps.agent_runtime.runtime import request_run_cancel
from apps.agent_runtime.views import cancel_run
from apps.company.models import (
    Company,
    TransparentCompany,
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentTestMessage,
    TransparentCompanyAgentTestSession,
)
from apps.user.models import User
from common.utils.Login_utils import sign_token
from unittest.mock import patch


class RunCancelTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create(
            uid="phase3-a-user",
            username="phase3-a-user",
            created_at="2026-08-13",
            updated_at="2026-08-13",
        )
        self.company = Company.objects.create(
            account=self.user,
            cid="phase3-a-company",
            company_name="Phase 3-A Company",
            created_at="2026-08-13",
            updated_at="2026-08-13",
        )
        self.transparent_company = TransparentCompany.objects.create(company=self.company)
        self.agent = TransparentCompanyAgentDefinition.objects.create(
            transparentCompany=self.transparent_company,
            agentId="phase3-a-agent",
            name="Phase 3-A Agent",
            enabled=True,
            status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
            created_at="2026-08-13",
            updated_at="2026-08-13",
        )
        self.session = TransparentCompanyAgentTestSession.objects.create(
            transparentCompany=self.transparent_company,
            agent=self.agent,
            operator=self.user,
            conversationId="phase3-a-conversation",
            sessionKey="phase3-a-session",
            source=TransparentCompanyAgentTestSession.SOURCE_AGENT_TEST,
            created_at="2026-08-13",
            updated_at="2026-08-13",
        )
        self.input_message = TransparentCompanyAgentTestMessage.objects.create(
            session=self.session,
            role=TransparentCompanyAgentTestMessage.ROLE_USER,
            content="cancel me",
            created_at="2026-08-13",
            updated_at="2026-08-13",
        )
        self.snapshot = AgentConfigSnapshot.objects.create(
            company=self.company,
            agent=self.agent,
            model_config={},
            tool_policy={},
            memory_policy={},
            compiled_prompt_encrypted="",
            checksum="phase3-a",
        )

    def _run(self, status=AgentRun.STATUS_RUNNING):
        return AgentRun.objects.create(
            company=self.company,
            request_id=f"request-{status}",
            agent=self.agent,
            user=self.user,
            session=self.session,
            source=AgentRun.SOURCE_AGENT_TEST,
            engine=AgentRun.ENGINE_UNIFIED,
            status=status,
            config_snapshot=self.snapshot,
            input_message=self.input_message,
            model_provider="openai",
            model_name="gpt-test",
        )

    def test_request_run_cancel_marks_active_run_cancelling(self):
        run = self._run()

        result = request_run_cancel(run_id=run.pk, company=self.company)

        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.STATUS_CANCELLING)
        self.assertIsNotNone(run.cancel_requested_at)
        self.assertFalse(result.already_terminal)
        self.assertEqual(result.status, AgentRun.STATUS_CANCELLING)
        self.assertTrue(
            AgentRunEvent.objects.filter(
                run=run,
                event_type=str(RunEventType.RUN_CANCELLED),
            ).exists()
        )

    def test_cancel_completed_run_is_noop(self):
        run = self._run(status=AgentRun.STATUS_COMPLETED)

        result = request_run_cancel(run_id=run.pk, company=self.company)

        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.STATUS_COMPLETED)
        self.assertTrue(result.already_terminal)

    def test_cancel_api_requires_company_admin_and_returns_state(self):
        run = self._run()
        request = self.factory.post(
            f"/api/agent-runtime/runs/{run.pk}/cancel",
            HTTP_ACCESS_TOKEN=sign_token({"user_id": self.user.pk}),
        )

        with patch("apps.agent_runtime.views.get_user_from_request", return_value=self.user), patch(
            "apps.agent_runtime.views.CompanyService._get_company_for_admin",
            return_value=(True, self.company),
        ):
            response = cancel_run(request, run.pk)

        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["data"]["status"], AgentRun.STATUS_CANCELLING)
        self.assertFalse(payload["data"]["already_terminal"])
