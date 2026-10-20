from datetime import datetime, timezone
from unittest.mock import patch

from django.test import TestCase

from apps.company.models import (
    TransparentCompany,
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentTestMessage,
    TransparentCompanyAgentTestSession,
    TransparentCompanySkill,
)
from apps.opengate.models import Gates, GatesTable
from apps.runtime.models import OpenClawAuditLog, OpenClawConfigVersion
from apps.user.models import User


EXPECTED_STATS_FIELDS = {
    "immutable_records",
    "verified_records",
    "verification_rate",
    "last_verification",
    "transparent_companies",
    "total_agents",
    "public_prompts",
    "prompt_versions",
    "total_projects",
    "total_tables",
    "total_data_rows",
    "daily_verifications",
    "daily_agent_responses",
    "daily_data_writes",
}

NUMERIC_STATS_FIELDS = EXPECTED_STATS_FIELDS - {"last_verification"}


class TransparencyStatsApiTests(TestCase):
    def test_empty_deployment_returns_complete_zero_payload(self):
        response = self.client.get("/api/backend/transparency/stats")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["result"], 1)
        self.assertEqual(payload["message"], "success")
        self.assertEqual(
            payload["data"],
            {
                "immutable_records": 0,
                "verified_records": 0,
                "verification_rate": 0,
                "last_verification": None,
                "transparent_companies": 0,
                "total_agents": 0,
                "public_prompts": 0,
                "prompt_versions": 0,
                "total_projects": 0,
                "total_tables": 0,
                "total_data_rows": 0,
                "daily_verifications": 0,
                "daily_agent_responses": 0,
                "daily_data_writes": 0,
            },
        )

    @patch("apps.company.transparency_stats.ImmudbService")
    def test_stats_are_aggregated_from_existing_records(self, mock_immudb_service):
        mock_immudb_service.return_value.get_table_integrity.return_value = (
            True,
            {"total_rows": 3},
        )
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        user = User.objects.create(uid="u1", username="Owner", created_at=now, updated_at=now)
        company = user.company_set.create(
            cid="c1",
            company_name="Demo",
            description="",
            status="1",
            created_at=now,
            updated_at=now,
        )
        transparent_company = TransparentCompany.objects.create(
            company=company,
            avatar="",
            prompts="Company prompt",
        )
        agent = TransparentCompanyAgentDefinition.objects.create(
            transparentCompany=transparent_company,
            agentId="agent-1",
            name="Agent",
            systemPrompt="Public prompt",
            visibility=TransparentCompanyAgentDefinition.VISIBILITY_PUBLIC,
            exposedToClient=True,
            enabled=True,
            status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
            created_at=now,
            updated_at=now,
        )
        session = TransparentCompanyAgentTestSession.objects.create(
            transparentCompany=transparent_company,
            agent=agent,
            operator=user,
            conversationId="conversation-1",
            created_at=now,
            updated_at=now,
        )
        TransparentCompanyAgentTestMessage.objects.create(
            session=session,
            role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT,
            content="Assistant response",
            created_at=now,
            updated_at=now,
        )
        TransparentCompanySkill.objects.create(
            transparentCompany=transparent_company,
            skillId="skill-1",
            name="Skill",
            content="Prompt content",
            updated_at=now,
            lastPublishedAt=now,
        )
        gate = Gates.objects.create(
            gid="g1",
            owner=user,
            gatename="Gate",
            created_at=now,
            updated_at=now,
        )
        GatesTable.objects.create(
            tid="t1",
            gate=gate,
            name="Table",
            created_at=now,
            updated_at=now,
        )
        OpenClawConfigVersion.objects.create(
            company=company,
            version="v1",
            status=OpenClawConfigVersion.STATUS_PUBLISHED,
            config_payload="{}",
            published_at=now,
            created_at=now,
            updated_at=now,
        )
        OpenClawAuditLog.objects.create(
            company=company,
            actor_type=OpenClawAuditLog.ACTOR_SYSTEM,
            action="workspace.write",
            target_type=OpenClawAuditLog.TARGET_AGENT,
            created_at=now,
        )

        response = self.client.get("/api/backend/transparency/stats")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(set(data.keys()), EXPECTED_STATS_FIELDS)
        for field in NUMERIC_STATS_FIELDS:
            self.assertIsInstance(data[field], int)
        self.assertEqual(data["transparent_companies"], 1)
        self.assertEqual(data["total_agents"], 1)
        self.assertEqual(data["public_prompts"], 1)
        self.assertEqual(data["prompt_versions"], 1)
        self.assertEqual(data["total_projects"], 1)
        self.assertEqual(data["total_tables"], 1)
        self.assertEqual(data["total_data_rows"], 3)
        self.assertEqual(data["immutable_records"], 5)
        self.assertEqual(data["verified_records"], 5)
        self.assertEqual(data["verification_rate"], 100)
        self.assertEqual(data["last_verification"], now.replace(" ", "T") + "Z")
        self.assertEqual(data["daily_verifications"], 2)
        self.assertEqual(data["daily_agent_responses"], 1)
        self.assertEqual(data["daily_data_writes"], 1)
