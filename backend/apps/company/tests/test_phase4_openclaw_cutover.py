import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from apps.agent_runtime.contracts import ModelEvent
from apps.agent_runtime.enums import ModelEventType
from apps.company.models import (
    Company,
    CompanyUserMemoryPolicy,
    TransparentCompany,
    TransparentCompanyAgentDefinition,
)
from apps.company.services import CompanyService
from apps.user.models import User


class Phase4OpenClawCutoverTests(TestCase):
    def setUp(self):
        now = str(time.time())[:20]
        self.user = User.objects.create(
            uid="phase4-cutover-user",
            username="phase4-cutover-user",
            created_at=now,
            updated_at=now,
            role="user",
        )
        self.company = Company.objects.create(
            account=self.user,
            cid="phase4-cutover-company",
            company_name="Phase4 Cutover Inc",
            status=Company.COMPANY_STATUS_TRANSPARENT,
            created_at=now,
            updated_at=now,
        )
        self.transparent_company = TransparentCompany.objects.create(
            company=self.company,
            prompts="Phase4 cutover prompts",
        )
        self.agent = TransparentCompanyAgentDefinition.objects.create(
            transparentCompany=self.transparent_company,
            agentId="support-agent",
            name="Customer Support",
            role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            visibility=TransparentCompanyAgentDefinition.VISIBILITY_PUBLIC,
            clientVisibility=TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_DEFAULT,
            exposedToClient=True,
            isDefaultEntry=True,
            routeKey="",
            exposureOrder=100,
            enabled=True,
            status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
            isCoreAgent=False,
            created_at=now,
            updated_at=now,
        )
        CompanyUserMemoryPolicy.objects.create(
            company=self.company,
            enabled=True,
            status=CompanyUserMemoryPolicy.STATUS_ACTIVE,
            maxRecallItems=5,
            maxRecallTokens=320,
            allowSensitive=False,
            writebackEnabled=True,
            writebackMode=CompanyUserMemoryPolicy.WRITEBACK_MODE_SHADOW,
            writeMinConfidence=0.9,
            created_at=now,
            updated_at=now,
        )

    def test_user_memory_extractor_uses_unified_model_gateway_without_openclaw(self):
        async def fake_stream(_gateway, request):
            self.assertEqual(request.provider, "openai")
            self.assertEqual(request.model, "gpt-test")
            self.assertEqual(request.credential_ref, "platform:openai")
            yield ModelEvent(
                str(ModelEventType.TEXT_DELTA),
                {"delta": json.dumps({"items": []})},
            )
            yield ModelEvent(str(ModelEventType.USAGE), {"total_tokens": 7})

        now = str(time.time())[:20]
        session = self.agent.testSessions.create(
            transparentCompany=self.transparent_company,
            conversationId="phase4-memory-extractor-session",
            sessionKey="phase4-memory-extractor-session",
            created_at=now,
            updated_at=now,
        )
        source_message = session.messages.create(
            role="user",
            content="Remember that I prefer concise answers",
            created_at=now,
            updated_at=now,
        )
        selection = SimpleNamespace(
            provider="openai",
            model="gpt-test",
            credential_ref="platform:openai",
            timeout_seconds=5,
        )
        with patch(
            "apps.agent_runtime.providers.model.CompanyModelResolver.resolve",
            return_value=selection,
        ) as resolve_mock, patch(
            "apps.agent_runtime.providers.model.OpenAICompatibleModelGateway.stream",
            new=fake_stream,
        ), patch(
            "apps.runtime.services.OpenClawRuntimeService.get_company_instance",
            side_effect=AssertionError("OpenClaw must not be used by memory extractor"),
        ), patch(
            "apps.runtime.services.OpenClawRuntimeService.get_provider_adapter",
            side_effect=AssertionError("OpenClaw adapter must not be used by memory extractor"),
        ):
            ok, payload = CompanyService._execute_public_client_entry_user_memory_extractor_via_runtime(
                session,
                source_message=source_message,
                config={"allowSensitive": False},
            )

        self.assertTrue(ok)
        self.assertEqual(payload["assistant_content"], json.dumps({"items": []}))
        self.assertEqual(payload["token_count"], 7)
        self.assertEqual(payload["runtime"], "unified")
        resolve_mock.assert_called_once_with(self.company)
