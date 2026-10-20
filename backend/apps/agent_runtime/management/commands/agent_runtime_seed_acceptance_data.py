"""Seed deterministic, isolated Company A/B acceptance data."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.company.models import (
    Company,
    CompanySessionMemoryPolicy,
    CompanyUserMemoryItem,
    CompanyUserMemoryPolicy,
    TransparentCompany,
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentMemoryBinding,
    TransparentCompanyAgentPermissionBinding,
    TransparentCompanyAgentPromptVersion,
    TransparentCompanyAgentSkillBinding,
    TransparentCompanyAgentTestMessage,
    TransparentCompanyAgentTestSession,
    TransparentCompanyAgentUserMemoryBinding,
    TransparentCompanyAgentVikingSpace,
    TransparentCompanySkill,
)
from apps.runtime.models import WorkspaceFile
from apps.user.models import User


DEFAULT_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "data_baseline_acceptance.json"
)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _viking_identity(cid: str, agent_id: str) -> tuple[str, str, str, str]:
    account_id = f"tm-{hashlib.sha256(cid.encode('utf-8')).hexdigest()[:24]}"
    user_digest = hashlib.sha256(f"{cid}:{agent_id}".encode("utf-8")).hexdigest()[:24]
    user_id = f"agent-{user_digest}"
    return (
        f"{account_id}:{user_id}",
        account_id,
        user_id,
        f"viking://user/{user_id}/memories/",
    )


class Command(BaseCommand):
    help = "Seed deterministic Company A/B data for manual acceptance tests."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
        parser.add_argument(
            "--allow-non-test-database",
            action="store_true",
            help="Allow seeding a non-production development database",
        )

    def handle(self, *args, **options):
        environment = str(getattr(settings, "ENVIRONMENT", "")).strip().lower()
        database_name = str(settings.DATABASES["default"].get("NAME") or "")
        is_test_database = environment in {"test", "testing"} or database_name.startswith("test_")
        if environment in {"prod", "production"}:
            raise CommandError("Acceptance fixtures are forbidden in production")
        if not is_test_database and not options["allow_non_test_database"]:
            raise CommandError(
                "Refusing to seed a non-test database; use --allow-non-test-database "
                "only for an isolated development database"
            )

        fixture_path = Path(options["fixture"]).expanduser().resolve()
        try:
            payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f"Cannot read fixture {fixture_path}: {exc}") from exc
        if payload.get("schemaVersion") != 1:
            raise CommandError("Unsupported acceptance fixture schemaVersion")

        with transaction.atomic():
            summary = [self._seed_company(item) for item in payload.get("companies", [])]

        self.stdout.write(
            self.style.SUCCESS(
                json.dumps(
                    {
                        "fixtureId": payload.get("fixtureId"),
                        "companies": summary,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        )

    def _seed_company(self, item):
        now = _timestamp()
        owner_data = item["owner"]
        owner, _ = User.objects.update_or_create(
            uid=owner_data["uid"],
            defaults={
                "username": owner_data["username"],
                "role": "user",
                "created_at": now,
                "updated_at": now,
            },
        )
        company, _ = Company.objects.update_or_create(
            cid=item["cid"],
            defaults={
                "account": owner,
                "company_name": item["name"],
                "status": Company.COMPANY_STATUS_TRANSPARENT,
                "verification_status": Company.VERIFICATION_STATUS_VERIFIED,
                "verification_provider": Company.VERIFICATION_PROVIDER_TENCENT_ESS,
                "verified_at": datetime.now(timezone.utc),
                "description": "Deterministic Phase 0 acceptance fixture",
                "created_at": now,
                "updated_at": now,
            },
        )
        transparent_company = TransparentCompany.objects.filter(company=company).order_by("id").first()
        if transparent_company is None:
            transparent_company = TransparentCompany.objects.create(
                company=company,
                prompts=item["companyPrompt"],
            )
        elif transparent_company.prompts != item["companyPrompt"]:
            transparent_company.prompts = item["companyPrompt"]
            transparent_company.save(update_fields=["prompts"])

        skill_data = item["skill"]
        skill, _ = TransparentCompanySkill.objects.update_or_create(
            transparentCompany=transparent_company,
            skillId=skill_data["skillId"],
            defaults={
                "name": skill_data["name"],
                "version": "1.0.0",
                "category": "acceptance",
                "enabled": True,
                "status": TransparentCompanySkill.STATUS_ACTIVE,
                "runtimeStatus": TransparentCompanySkill.RUNTIME_STATUS_ACTIVE,
                "sourceType": TransparentCompanySkill.SOURCE_SYSTEM,
                "content": skill_data["content"],
                "updated_at": now,
            },
        )

        CompanySessionMemoryPolicy.objects.update_or_create(
            company=company,
            defaults={
                "enabled": True,
                "status": CompanySessionMemoryPolicy.STATUS_ACTIVE,
                "mode": CompanySessionMemoryPolicy.MODE_SESSION,
                "created_at": now,
                "updated_at": now,
            },
        )
        CompanyUserMemoryPolicy.objects.update_or_create(
            company=company,
            defaults={
                "enabled": True,
                "status": CompanyUserMemoryPolicy.STATUS_ACTIVE,
                "writebackEnabled": True,
                "writebackMode": CompanyUserMemoryPolicy.WRITEBACK_MODE_SHADOW,
                "created_at": now,
                "updated_at": now,
            },
        )

        agents = {}
        for index, agent_data in enumerate(item["agents"]):
            is_public = bool(agent_data["public"])
            agent, _ = TransparentCompanyAgentDefinition.objects.update_or_create(
                transparentCompany=transparent_company,
                agentId=agent_data["agentId"],
                defaults={
                    "name": agent_data["name"],
                    "role": TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
                    "description": "Deterministic Phase 0 acceptance Agent",
                    "systemPrompt": agent_data["prompt"],
                    "visibility": (
                        TransparentCompanyAgentDefinition.VISIBILITY_PUBLIC
                        if is_public
                        else TransparentCompanyAgentDefinition.VISIBILITY_INTERNAL
                    ),
                    "clientVisibility": (
                        TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_VISIBLE
                        if is_public
                        else TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_HIDDEN
                    ),
                    "exposedToClient": is_public,
                    "isDefaultEntry": is_public,
                    "routeKey": "" if is_public else f"internal-{index}",
                    "exposureOrder": 100 + index,
                    "enabled": True,
                    "isCoreAgent": False,
                    "status": TransparentCompanyAgentDefinition.STATUS_ACTIVE,
                    "sortOrder": 100 + index,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            agents[agent.agentId] = agent
            TransparentCompanyAgentPromptVersion.objects.update_or_create(
                agent=agent,
                versionLabel="phase0-v1",
                defaults={
                    "content": agent_data["prompt"],
                    "changeReason": "Phase 0 acceptance baseline",
                    "changeType": TransparentCompanyAgentPromptVersion.CHANGE_TYPE_INITIAL,
                    "createdBy": owner.uid,
                    "isCurrent": True,
                    "created_at": now,
                },
            )
            TransparentCompanyAgentSkillBinding.objects.update_or_create(
                transparentCompany=transparent_company,
                agent=agent,
                skill=skill,
                defaults={
                    "enabled": True,
                    "sortOrder": 100,
                    "note": "Phase 0 acceptance fixture",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            python_allowed = bool(agent_data["pythonAllowed"])
            memory_enabled = bool(agent_data["memoryEnabled"])
            TransparentCompanyAgentPermissionBinding.objects.update_or_create(
                transparentCompany=transparent_company,
                agent=agent,
                defaults={
                    "memoryRead": memory_enabled,
                    "memoryWrite": memory_enabled,
                    "memoryUpdate": memory_enabled,
                    "memoryDelete": memory_enabled,
                    "enabled": True,
                    "status": TransparentCompanyAgentPermissionBinding.STATUS_ACTIVE,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            TransparentCompanyAgentMemoryBinding.objects.update_or_create(
                transparentCompany=transparent_company,
                agent=agent,
                defaults={
                    "memoryScope": (
                        TransparentCompanyAgentMemoryBinding.SCOPE_SESSION
                        if memory_enabled
                        else TransparentCompanyAgentMemoryBinding.SCOPE_DISABLED
                    ),
                    "enabled": memory_enabled,
                    "status": (
                        TransparentCompanyAgentMemoryBinding.STATUS_ACTIVE
                        if memory_enabled
                        else TransparentCompanyAgentMemoryBinding.STATUS_INACTIVE
                    ),
                    "created_at": now,
                    "updated_at": now,
                },
            )
            TransparentCompanyAgentUserMemoryBinding.objects.update_or_create(
                transparentCompany=transparent_company,
                agent=agent,
                defaults={
                    "memoryScope": (
                        TransparentCompanyAgentUserMemoryBinding.SCOPE_COMPANY_USER
                        if memory_enabled
                        else TransparentCompanyAgentUserMemoryBinding.SCOPE_DISABLED
                    ),
                    "enabled": memory_enabled,
                    "status": (
                        TransparentCompanyAgentUserMemoryBinding.STATUS_ACTIVE
                        if memory_enabled
                        else TransparentCompanyAgentUserMemoryBinding.STATUS_INACTIVE
                    ),
                    "created_at": now,
                    "updated_at": now,
                },
            )
            viking_space_id, account_id, user_id, root_uri = _viking_identity(
                company.cid,
                agent.agentId,
            )
            TransparentCompanyAgentVikingSpace.objects.update_or_create(
                agent=agent,
                defaults={
                    "transparentCompany": transparent_company,
                    "vikingSpaceId": viking_space_id,
                    "accountId": account_id,
                    "userId": user_id,
                    "rootUri": root_uri,
                    "status": TransparentCompanyAgentVikingSpace.STATUS_ACTIVE,
                    "lastError": "",
                    "created_at": now,
                    "updated_at": now,
                },
            )

        memory_data = item["memorySample"]
        memory_agent = agents[memory_data["agentId"]]
        CompanyUserMemoryItem.objects.update_or_create(
            company=company,
            user=owner,
            agent=memory_agent,
            memoryKey=memory_data["memoryKey"],
            defaults={
                "scope": CompanyUserMemoryItem.SCOPE_AGENT,
                "memoryType": memory_data["memoryType"],
                "content": memory_data["content"],
                "confidence": 1.0,
                "status": CompanyUserMemoryItem.STATUS_ACTIVE,
                "created_at": now,
                "updated_at": now,
            },
        )

        public_agent = next(agent for agent in agents.values() if agent.exposedToClient)
        session, _ = TransparentCompanyAgentTestSession.objects.update_or_create(
            conversationId=f"phase0-{company.cid}-conversation",
            defaults={
                "transparentCompany": transparent_company,
                "agent": public_agent,
                "operator": owner,
                "title": "Phase 0 acceptance conversation",
                "status": TransparentCompanyAgentTestSession.STATUS_ACTIVE,
                "messageCount": 2,
                "lastMessage": "Acceptance fixture ready",
                "sessionKey": f"phase0-{company.cid}",
                "source": TransparentCompanyAgentTestSession.SOURCE_AGENT_TEST,
                "created_at": now,
                "updated_at": now,
            },
        )
        TransparentCompanyAgentTestMessage.objects.get_or_create(
            session=session,
            role=TransparentCompanyAgentTestMessage.ROLE_USER,
            content="What is my isolated project code?",
            defaults={"created_at": now, "updated_at": now},
        )
        TransparentCompanyAgentTestMessage.objects.get_or_create(
            session=session,
            role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT,
            content="Acceptance fixture ready",
            defaults={"created_at": now, "updated_at": now},
        )

        workspace_data = item["workspaceFile"]
        WorkspaceFile.objects.update_or_create(
            company=company,
            logical_path=workspace_data["logicalPath"],
            defaults={
                "instance": None,
                "file_name": workspace_data["fileName"],
                "storage_backend": WorkspaceFile.STORAGE_TENCENT_COS,
                "storage_key": workspace_data["storageKey"],
                "size": 0,
                "sync_status": WorkspaceFile.SYNC_PENDING,
                "resource_sync_status": WorkspaceFile.RESOURCE_SYNC_PENDING,
                "created_by": owner.uid,
                "created_at": now,
                "updated_at": now,
            },
        )
        return {
            "cid": company.cid,
            "agentIds": sorted(agents),
            "memorySample": memory_data["content"],
        }
