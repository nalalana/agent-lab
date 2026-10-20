import json
from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.agent_runtime.models import AgentRun, CompanyAgentRuntimePolicy, SandboxExecution
from apps.agent_runtime.tests.memory_fixtures import create_memory_fixture
from apps.company.admin_access import agent_allowed
from apps.company.models import CompanyMember, TransparentCompanyAgentTestMessage, TransparentCompanyAgentTestSession
from apps.company.services import CompanyService
from apps.iam.models import Grant, Group, GroupMembership, Principal, Resource, Tenant, TenantMembership
from common.utils.Login_utils import sign_token
from apps.iam.services import compute_effective_permission, compute_effective_permissions


class ScopedAgentAccessTests(TestCase):
    def setUp(self):
        self.fixture = create_memory_fixture("scoped-agent")
        self.owner = self.fixture["owner"]
        self.user = self.fixture["second_user"]
        self.company = self.fixture["company_a"]
        self.agent = self.fixture["agent_a1"]
        self.other_agent = self.fixture["agent_a2"]
        self.tenant = Tenant.objects.create(type=Tenant.TYPE_ORGANIZATION,
            external_id=f"organization:{self.company.cid}", super_admin_user=self.owner)
        TenantMembership.objects.create(tenant=self.tenant, user=self.owner, role=TenantMembership.ROLE_SUPER_ADMIN)
        TenantMembership.objects.create(tenant=self.tenant, user=self.user)
        self.member = CompanyMember.objects.create(company=self.company, user=self.user,
            memberUid=self.user.uid, name="Employee", status=CompanyMember.STATUS_ACTIVE,
            created_at="1", updated_at="1")
        company_resource = Resource.objects.get(type=Resource.TYPE_COMPANY, external_id=self.company.cid)
        company_resource.tenant = self.tenant
        company_resource.save(update_fields=["tenant"])
        self.resource = Resource.objects.create(type=Resource.TYPE_AGENT,
            external_id=f"agent:{self.company.cid}:{self.agent.agentId}", parent=company_resource, tenant=self.tenant)
        Resource.objects.create(type=Resource.TYPE_AGENT,
            external_id=f"agent:{self.company.cid}:{self.other_agent.agentId}", parent=company_resource, tenant=self.tenant)
        self.principal = Principal.objects.create(type=Principal.TYPE_USER, external_id=self.user.uid)
        self.grant = Grant.objects.create(principal=self.principal, resource=self.resource, permissions=Grant.ROLE_VIEWER)
        self.visible_run = self.make_run(self.agent, self.fixture["session_a1"], "visible-run", 10)
        self.hidden_run = self.make_run(self.other_agent, self.fixture["session_a2"], "hidden-run", 1000)
        self.headers = {"HTTP_ACCESS_TOKEN": sign_token({"user_id": self.user.id})}

    def make_run(self, agent, session, request_id, tokens):
        message = TransparentCompanyAgentTestMessage.objects.create(session=session, role="user",
            content=request_id, created_at="1", updated_at="1")
        return AgentRun.objects.create(request_id=request_id, company=self.company, agent=agent,
            user=self.owner, session=session, source=AgentRun.SOURCE_AGENT_TEST,
            engine=AgentRun.ENGINE_UNIFIED, status=AgentRun.STATUS_COMPLETED,
            input_message=message, input_tokens=tokens, completed_at=timezone.now())

    def get(self, path, **params):
        return self.client.get("/api/agent-runtime/" + path, {"cid": self.company.cid, **params}, **self.headers)

    def test_viewer_can_see_authorized_agent_but_never_chat_or_configure(self):
        response = self.client.get("/api/company/transparent/agent-test/agents", {"cid": self.company.cid}, **self.headers)
        self.assertEqual(response.status_code, 200)
        agents = response.json()["data"]
        self.assertEqual([agent["agentId"] for agent in agents], [self.agent.agentId])
        self.assertEqual(agents[0]["permissions"], 1)
        self.assertFalse(agents[0]["can_chat"])
        self.assertFalse(agents[0]["can_configure"])
        self.assertNotIn("systemPrompt", agents[0])
        self.assertFalse(agent_allowed(self.company, self.user, self.agent.agentId))
        before = TransparentCompanyAgentTestSession.objects.count()
        self.assertFalse(CompanyService.create_transparent_company_test_session(self.company.cid, self.agent.agentId, self.user)[0])
        with patch("apps.company.services.CompanyService._resolve_agent_test_session_for_send") as resolve:
            for method in (CompanyService.send_transparent_company_test_message, CompanyService.stream_transparent_company_test_message):
                self.assertFalse(method(self.company.cid, "", "hello", self.user, agent_id=self.agent.agentId)[0])
            resolve.assert_not_called()
        self.assertEqual(TransparentCompanyAgentTestSession.objects.count(), before)

    def test_role_flags_separate_use_from_configuration(self):
        for permission in (2, 3, 4):
            self.grant.permissions = permission
            self.grant.save(update_fields=["permissions"])
            agent = CompanyService.list_transparent_company_test_agents(self.company.cid, self.user)[1][0]
            self.assertTrue(agent["can_chat"])
            self.assertEqual(agent["can_configure"], permission >= 3)
        self.agent.enabled = False
        self.agent.save(update_fields=["enabled"])
        self.assertFalse(CompanyService.list_transparent_company_test_agents(self.company.cid, self.user)[1][0]["can_chat"])

    def test_unified_delegate_sessions_are_own_and_authorized_not_company_wide(self):
        self.grant.permissions = 2
        self.grant.save(update_fields=["permissions"])
        ok, created = CompanyService.create_transparent_company_test_session(self.company.cid, self.agent.agentId, self.user)
        self.assertTrue(ok)
        sessions = CompanyService.list_transparent_company_test_sessions(self.company.cid, self.user)[1]
        self.assertEqual([session["conversation_id"] for session in sessions], [created["conversation_id"]])
        self.assertFalse(CompanyService.create_transparent_company_test_session(self.company.cid, self.other_agent.agentId, self.user)[0])
        self.assertFalse(CompanyService.get_transparent_company_test_messages(self.company.cid, self.fixture["session_a1"].conversationId, self.user)[0])

    def test_monitoring_lists_details_and_filters_only_readable_agents(self):
        response = self.get("runs")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual([item["id"] for item in data["items"]], [str(self.visible_run.id)])
        self.assertEqual(self.get(f"runs/{self.visible_run.id}").status_code, 200)
        self.assertEqual(self.get(f"runs/{self.hidden_run.id}").status_code, 404)
        self.assertEqual(self.get("runs", agent_id=self.other_agent.agentId).json()["data"]["total"], 0)
        self.assertEqual(self.get("runs", agent_id=str(self.other_agent.id)).json()["data"]["total"], 0)
        metrics = self.get("metrics")
        self.assertEqual(metrics.status_code, 200)
        self.assertEqual(metrics.json()["data"]["runCount"], 1)
        cursor = self.get("runs", cursor=str(self.hidden_run.id))
        self.assertEqual(cursor.json()["data"]["total"], 1)

    def test_alerts_never_include_hidden_agents_or_company_platform_usage(self):
        CompanyAgentRuntimePolicy.objects.create(company=self.company, daily_token_budget=5, daily_sandbox_seconds_budget=10)
        sandbox = SandboxExecution.objects.create(run=self.hidden_run, company=self.company,
            status=SandboxExecution.STATUS_RUNNING, template="test", cpu_cores=2,
            memory_mib=2048, network_policy="isolated", expires_at=timezone.now() + timedelta(minutes=5))
        SandboxExecution.objects.filter(pk=sandbox.pk).update(created_at=timezone.now() - timedelta(hours=1))
        response = self.get("alerts")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["metrics"]["dailyTokenUsage"], 10)
        self.assertEqual(data["metrics"]["orphanSandboxCount"], 0)
        self.assertIsNone(data["metrics"]["dailyTokenBudget"])
        self.assertIsNone(data["metrics"]["globalActiveSandboxCount"])
        self.assertIsNone(data["metrics"]["globalDailyEstimatedCostMicroUnits"])
        self.assertNotIn("token_quota", [alert["code"] for alert in data["alerts"]])

    def test_revocation_expiration_disabled_membership_and_suspension_deny_access(self):
        self.grant.expires_at = timezone.now() - timedelta(seconds=1)
        self.grant.save(update_fields=["expires_at"])
        self.assertEqual(self.get("runs").status_code, 403)
        self.grant.expires_at = None
        self.grant.save(update_fields=["expires_at"])
        self.member.status = CompanyMember.STATUS_DISABLED
        self.member.save(update_fields=["status"])
        self.assertEqual(self.get("runs").status_code, 403)
        self.member.status = CompanyMember.STATUS_ACTIVE
        self.member.save(update_fields=["status"])
        self.company.service_suspended_at = timezone.now()
        self.company.service_suspension_reason = "test suspension"
        self.company.save(update_fields=["service_suspended_at", "service_suspension_reason"])
        self.assertEqual(self.get("runs").status_code, 403)
        self.company.service_suspended_at = None
        self.company.service_suspension_reason = ""
        self.company.save(update_fields=["service_suspended_at", "service_suspension_reason"])
        self.grant.delete()
        self.assertEqual(self.get("runs").status_code, 403)

    def test_batched_permissions_match_single_checks_for_inheritance_groups_and_expiry(self):
        parent = self.resource.parent
        other = Resource.objects.get(external_id=f"agent:{self.company.cid}:{self.other_agent.agentId}")
        group = Group.objects.create(tenant=self.tenant, name="Editors")
        GroupMembership.objects.create(group=group, user=self.user)
        group_principal = Principal.objects.create(type=Principal.TYPE_GROUP, external_id=str(group.group_id))
        group_grant = Grant.objects.create(principal=group_principal, resource=parent, permissions=3)
        resources = [self.resource, other]
        for independent in (False, True):
            other.permission_mode = Resource.PERMISSION_MODE_INDEPENDENT if independent else Resource.PERMISSION_MODE_INHERIT
            other.save(update_fields=["permission_mode"])
            for inherited in (False, True):
                group_grant.inherit = inherited
                group_grant.save(update_fields=["inherit"])
                for permission in (1, 2, 3, 4):
                    self.grant.permissions = permission
                    self.grant.save(update_fields=["permissions"])
                    batch = compute_effective_permissions(self.principal, resources)
                    for resource in resources:
                        self.assertEqual(batch[resource.id].permissions, compute_effective_permission(self.principal, resource).permissions)
        self.grant.expires_at = timezone.now() - timedelta(seconds=1)
        self.grant.save(update_fields=["expires_at"])
        self.member.status = CompanyMember.STATUS_DISABLED
        self.member.save(update_fields=["status"])
        self.assertTrue(all(item.permissions == 0 for item in compute_effective_permissions(self.principal, resources).values()))

    def test_batched_permission_query_count_does_not_grow_with_agents(self):
        parent = self.resource.parent
        resources = Resource.objects.bulk_create([
            Resource(type=Resource.TYPE_AGENT, external_id=f"agent:{self.company.cid}:bulk-{index}",
                     parent=parent, tenant=self.tenant) for index in range(50)
        ])
        Grant.objects.create(principal=self.principal, resource=parent, permissions=Grant.ROLE_VIEWER)
        with CaptureQueriesContext(connection) as queries:
            result = compute_effective_permissions(self.principal, resources)
        self.assertEqual(len(result), 50)
        self.assertTrue(all(item.permissions == Grant.ROLE_VIEWER for item in result.values()))
        self.assertLessEqual(len(queries), 12)

    def test_group_read_grants_are_honored_without_escalating_to_chat(self):
        self.grant.delete()
        group = Group.objects.create(tenant=self.tenant, name="Support")
        GroupMembership.objects.create(group=group, user=self.user)
        principal = Principal.objects.create(type=Principal.TYPE_GROUP, external_id=str(group.group_id))
        Grant.objects.create(principal=principal, resource=self.resource, permissions=Grant.ROLE_VIEWER)
        self.assertEqual(self.get("runs").json()["data"]["total"], 1)
        self.assertFalse(agent_allowed(self.company, self.user, self.agent.agentId))
