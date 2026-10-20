import time

from django.test import TestCase

from apps.company.models import Company, TransparentCompany, TransparentCompanyAgentDefinition
from apps.company.services import CompanyService
from apps.runtime.services import OpenClawConfigService
from apps.user.models import User


class PublicClientEntryPayloadTests(TestCase):
    def setUp(self):
        now = str(time.time())[:20]
        self.user = User.objects.create(
            uid="public-client-entry-admin",
            username="public-client-entry-admin",
            created_at=now,
            updated_at=now,
            role="user",
        )
        self.company = Company.objects.create(
            account=self.user,
            cid="company-public-client-entry",
            company_name="Public Client Entry Inc",
            status=Company.COMPANY_STATUS_TRANSPARENT,
            created_at=now,
            updated_at=now,
        )
        self.transparent_company = TransparentCompany.objects.create(
            company=self.company,
            prompts="Public client entry prompts",
        )

    def test_build_client_entry_payload_includes_listed_and_default_visibility_agents(self):
        now = str(time.time())[:20]
        listed_agent = TransparentCompanyAgentDefinition.objects.create(
            transparentCompany=self.transparent_company,
            agentId="listed-agent",
            name="Listed Agent",
            role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            visibility=TransparentCompanyAgentDefinition.VISIBILITY_PUBLIC,
            clientVisibility=TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_LISTED,
            exposedToClient=True,
            isDefaultEntry=False,
            routeKey="reception",
            exposureOrder=100,
            enabled=True,
            status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
            isCoreAgent=False,
            created_at=now,
            updated_at=now,
        )
        default_agent = TransparentCompanyAgentDefinition.objects.create(
            transparentCompany=self.transparent_company,
            agentId="default-agent",
            name="Default Agent",
            role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            visibility=TransparentCompanyAgentDefinition.VISIBILITY_PUBLIC,
            clientVisibility=TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_DEFAULT,
            exposedToClient=True,
            isDefaultEntry=True,
            routeKey="",
            exposureOrder=90,
            enabled=True,
            status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
            isCoreAgent=False,
            created_at=now,
            updated_at=now,
        )
        TransparentCompanyAgentDefinition.objects.create(
            transparentCompany=self.transparent_company,
            agentId="hidden-agent",
            name="Hidden Agent",
            role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            visibility=TransparentCompanyAgentDefinition.VISIBILITY_INTERNAL,
            clientVisibility=TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_HIDDEN,
            exposedToClient=False,
            isDefaultEntry=False,
            routeKey="hidden",
            exposureOrder=200,
            enabled=True,
            status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
            isCoreAgent=False,
            created_at=now,
            updated_at=now,
        )

        agents = CompanyService._get_client_entry_agents(self.transparent_company)
        self.assertEqual(
            {agent.agentId for agent in agents},
            {listed_agent.agentId, default_agent.agentId},
        )

        payload = CompanyService._build_client_entry_payload(self.transparent_company)
        self.assertEqual(payload["default_agent_id"], default_agent.agentId)
        self.assertEqual(payload["entry_agent_id"], default_agent.agentId)
        self.assertEqual(
            {item["agentId"] for item in payload["visible_agents"]},
            {listed_agent.agentId, default_agent.agentId},
        )
        self.assertEqual(
            {item["entry_key"] for item in payload["bindings"]},
            {"reception", "__default__"},
        )

    def test_runtime_preview_client_entry_matches_public_visibility_rules(self):
        now = str(time.time())[:20]
        listed_agent = TransparentCompanyAgentDefinition.objects.create(
            transparentCompany=self.transparent_company,
            agentId="listed-runtime-agent",
            name="Listed Runtime Agent",
            role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            visibility=TransparentCompanyAgentDefinition.VISIBILITY_PUBLIC,
            clientVisibility=TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_LISTED,
            exposedToClient=True,
            isDefaultEntry=False,
            routeKey="support",
            exposureOrder=100,
            enabled=True,
            status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
            isCoreAgent=False,
            created_at=now,
            updated_at=now,
        )
        default_agent = TransparentCompanyAgentDefinition.objects.create(
            transparentCompany=self.transparent_company,
            agentId="default-runtime-agent",
            name="Default Runtime Agent",
            role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            visibility=TransparentCompanyAgentDefinition.VISIBILITY_PUBLIC,
            clientVisibility=TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_DEFAULT,
            exposedToClient=True,
            isDefaultEntry=True,
            routeKey="",
            exposureOrder=90,
            enabled=True,
            status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
            isCoreAgent=False,
            created_at=now,
            updated_at=now,
        )

        ok, preview = OpenClawConfigService.build_company_preview(self.company)

        self.assertTrue(ok)
        assert isinstance(preview, dict)
        self.assertEqual(
            {item["agent_id"] for item in preview["client_entry"]["bindings"]},
            {listed_agent.agentId, default_agent.agentId},
        )
        self.assertEqual(
            preview["client_entry"]["default_agent_id"],
            default_agent.agentId,
        )
