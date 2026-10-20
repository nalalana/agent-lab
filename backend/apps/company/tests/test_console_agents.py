"""Contract tests for the frontend-facing Agent console endpoint."""

import time

from django.test import TestCase

from apps.company.models import (
    Company,
    TransparentCompany,
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentMemoryBinding,
    TransparentCompanyAgentPermissionBinding,
    TransparentCompanyAgentSkillBinding,
    TransparentCompanySkill,
)
from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import get_or_create_principal, get_or_create_resource, grant_permissions
from apps.user.models import User
from common.utils.Login_utils import sign_token


class ConsoleAgentsEndpointTests(TestCase):
    def setUp(self):
        self.now = str(time.time())[:20]
        self.user = User.objects.create(
            uid="agent-console-admin",
            username="agent-console-admin",
            created_at=self.now,
            updated_at=self.now,
            role="user",
        )
        self.company = Company.objects.create(
            account=self.user,
            cid="agent-console-company",
            company_name="Agent Console Company",
            status=Company.COMPANY_STATUS_TRANSPARENT,
            verification_status=Company.VERIFICATION_STATUS_VERIFIED,
            created_at=self.now,
            updated_at=self.now,
        )
        self.transparent_company = TransparentCompany.objects.create(
            company=self.company,
            prompts="Company prompt",
        )
        principal = get_or_create_principal(Principal.TYPE_USER, self.user.uid)
        resource = get_or_create_resource(Resource.TYPE_COMPANY, self.company.cid)
        grant_permissions(principal, resource, Grant.PERM_ADMIN, granter_user=self.user)
        self.token = sign_token({"user_id": self.user.id})

    def get_agents(self):
        response = self.client.get(
            "/api/backend/console/agents",
            {"cid": self.company.cid},
            HTTP_ACCESS_TOKEN=self.token,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["result"], 1)
        return payload["data"]["list"]

    def test_returns_bootstrapped_ceo(self):
        agents = self.get_agents()

        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["agent_id"], "main-agent")
        self.assertEqual(agents[0]["type"], "core")
        self.assertEqual(agents[0]["status_label"], "运行中")

    def test_returns_agents_created_after_the_first_request(self):
        self.get_agents()
        TransparentCompanyAgentDefinition.objects.create(
            transparentCompany=self.transparent_company,
            agentId="new-support-agent",
            name="新客服 Agent",
            role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            enabled=True,
            status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
            created_at=self.now,
            updated_at=self.now,
        )

        agents = self.get_agents()

        self.assertEqual(
            {agent["agent_id"] for agent in agents},
            {"main-agent", "new-support-agent"},
        )

    def test_aggregates_public_agent_permissions_memory_and_skills(self):
        agent = TransparentCompanyAgentDefinition.objects.create(
            transparentCompany=self.transparent_company,
            agentId="public-support-agent",
            name="公开客服",
            role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            visibility=TransparentCompanyAgentDefinition.VISIBILITY_PUBLIC,
            exposedToClient=True,
            routeKey="support",
            enabled=True,
            status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
            created_at=self.now,
            updated_at=self.now,
        )
        TransparentCompanyAgentPermissionBinding.objects.create(
            transparentCompany=self.transparent_company,
            agent=agent,
            skillsInclude=["support-skill"],
            knowledgeRead=["knowledge/support"],
            knowledgeWrite=["generated/support"],
            knowledgeDelete=["generated/support/tmp"],
            enabled=True,
            status=TransparentCompanyAgentPermissionBinding.STATUS_ACTIVE,
            created_at=self.now,
            updated_at=self.now,
        )
        TransparentCompanyAgentMemoryBinding.objects.create(
            transparentCompany=self.transparent_company,
            agent=agent,
            memoryScope=TransparentCompanyAgentMemoryBinding.SCOPE_SESSION,
            enabled=True,
            inheritCompanyPolicy=True,
            status=TransparentCompanyAgentMemoryBinding.STATUS_ACTIVE,
            created_at=self.now,
            updated_at=self.now,
        )
        skill = TransparentCompanySkill.objects.create(
            transparentCompany=self.transparent_company,
            skillId="support-skill",
            name="客服技能",
            enabled=True,
            status=TransparentCompanySkill.STATUS_ACTIVE,
            updated_at=self.now,
        )
        TransparentCompanyAgentSkillBinding.objects.create(
            transparentCompany=self.transparent_company,
            agent=agent,
            skill=skill,
            enabled=True,
            created_at=self.now,
            updated_at=self.now,
        )

        item = next(
            item for item in self.get_agents() if item["agent_id"] == agent.agentId
        )

        self.assertEqual(item["type"], "public")
        self.assertTrue(item["is_public"])
        self.assertEqual(item["customer_portal"], "support")
        self.assertEqual(item["knowledge_permissions"], ["read", "write", "delete"])
        self.assertEqual(item["memory_policy"], "继承公司会话策略")
        self.assertEqual(item["skills"], ["客服技能"])
