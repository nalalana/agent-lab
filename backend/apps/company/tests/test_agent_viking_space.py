"""Integration tests for one-Agent-one-OpenViking-space lifecycle rules."""

import time
from unittest.mock import patch

from django.test import TestCase

from apps.company.models import (
    Company,
    TransparentCompany,
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentPermissionBinding,
    TransparentCompanyAgentVikingSpace,
)
from apps.company.services import CompanyService
from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import get_or_create_principal, get_or_create_resource, grant_permissions
from apps.user.models import User


class AgentVikingSpaceTests(TestCase):
    """Verify space provisioning, ownership permissions, and archival behavior."""

    def setUp(self):
        now = str(time.time())[:20]
        self.user = User.objects.create(
            uid="memory-gateway-admin",
            username="memory-gateway-admin",
            created_at=now,
            updated_at=now,
            role="user",
        )
        self.company = Company.objects.create(
            account=self.user,
            cid="memory-gateway-company",
            company_name="Memory Gateway Company",
            status=Company.COMPANY_STATUS_TRANSPARENT,
            verification_status=Company.VERIFICATION_STATUS_VERIFIED,
            created_at=now,
            updated_at=now,
        )
        self.transparent_company = TransparentCompany.objects.create(
            company=self.company,
            prompts="Company prompt",
        )
        principal = get_or_create_principal(Principal.TYPE_USER, self.user.uid)
        resource = get_or_create_resource(Resource.TYPE_COMPANY, self.company.cid)
        grant_permissions(principal, resource, Grant.PERM_ADMIN, granter_user=self.user)

    @patch("apps.company.services.CompanyService._sync_transparent_company_runtime_config")
    def test_new_agent_gets_one_active_owned_space_and_full_memory_permissions(self, mock_sync):
        mock_sync.return_value = (True, {"version": {"version": "v-test"}})

        ok, _payload = CompanyService.upsert_transparent_company_agent(
            cid=self.company.cid,
            user=self.user,
            name="Customer Service",
            agent_id="customer-agent",
            role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
        )

        self.assertTrue(ok)
        agent = TransparentCompanyAgentDefinition.objects.get(agentId="customer-agent")
        space = TransparentCompanyAgentVikingSpace.objects.get(agent=agent)
        self.assertEqual(space.status, TransparentCompanyAgentVikingSpace.STATUS_ACTIVE)
        self.assertEqual(space.rootUri, f"viking://user/{space.userId}/memories/")

        binding = TransparentCompanyAgentPermissionBinding.objects.get(agent=agent)
        self.assertTrue(binding.memoryRead)
        self.assertTrue(binding.memoryWrite)
        self.assertTrue(binding.memoryUpdate)
        self.assertTrue(binding.memoryDelete)

    @patch("apps.company.services.CompanyService._sync_transparent_company_runtime_config")
    def test_deprecated_agent_archives_but_does_not_delete_space(self, mock_sync):
        mock_sync.return_value = (True, {"version": {"version": "v-test"}})
        CompanyService.upsert_transparent_company_agent(
            cid=self.company.cid,
            user=self.user,
            name="Purchasing",
            agent_id="purchasing-agent",
            role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
        )

        ok, _payload = CompanyService.upsert_transparent_company_agent(
            cid=self.company.cid,
            user=self.user,
            name="Purchasing",
            agent_id="purchasing-agent",
            role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            status=TransparentCompanyAgentDefinition.STATUS_DEPRECATED,
        )

        self.assertTrue(ok)
        space = TransparentCompanyAgentVikingSpace.objects.get(
            agent__agentId="purchasing-agent"
        )
        self.assertEqual(space.status, TransparentCompanyAgentVikingSpace.STATUS_ARCHIVED)

    def test_default_ceo_creation_gets_one_active_viking_space(self):
        transparent_company = self.transparent_company

        agent = CompanyService.ensure_default_transparent_agent(transparent_company)
        space = TransparentCompanyAgentVikingSpace.objects.get(agent=agent)

        self.assertEqual(agent.agentId, "main-agent")
        self.assertEqual(space.status, TransparentCompanyAgentVikingSpace.STATUS_ACTIVE)
        self.assertEqual(space.rootUri, f"viking://user/{space.userId}/memories/")
        self.assertEqual(
            TransparentCompanyAgentVikingSpace.objects.filter(agent=agent).count(),
            1,
        )

    def test_existing_default_ceo_is_backfilled_and_repaired_idempotently(self):
        transparent_company = self.transparent_company
        now = str(time.time())[:20]
        agent = TransparentCompanyAgentDefinition.objects.create(
            transparentCompany=transparent_company,
            agentId="main-agent",
            name="Existing CEO",
            role=TransparentCompanyAgentDefinition.ROLE_MAIN_AGENT,
            description="Existing default CEO",
            systemPrompt="Existing prompt",
            visibility=TransparentCompanyAgentDefinition.VISIBILITY_INTERNAL,
            clientVisibility=TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_HIDDEN,
            exposedToClient=False,
            isDefaultEntry=False,
            routeKey="",
            exposureOrder=0,
            enabled=True,
            isCoreAgent=True,
            status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
            sortOrder=0,
            created_at=now,
            updated_at=now,
        )

        first = CompanyService.ensure_default_transparent_agent(transparent_company)
        space = TransparentCompanyAgentVikingSpace.objects.get(agent=agent)
        space.rootUri = "viking://stale/path/"
        space.status = TransparentCompanyAgentVikingSpace.STATUS_FAILED
        space.save(update_fields=["rootUri", "status"])

        second = CompanyService.ensure_default_transparent_agent(transparent_company)
        space.refresh_from_db()

        self.assertEqual(first.id, second.id)
        self.assertEqual(space.status, TransparentCompanyAgentVikingSpace.STATUS_ACTIVE)
        self.assertEqual(space.rootUri, f"viking://user/{space.userId}/memories/")
        self.assertEqual(
            TransparentCompanyAgentVikingSpace.objects.filter(agent=agent).count(),
            1,
        )
