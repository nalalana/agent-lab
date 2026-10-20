"""Read-only validation of migrated runtime configuration and knowledge data."""

from __future__ import annotations

import hashlib

from apps.agent_runtime.config import ConfigResolutionError, ConfigResolver
from apps.company.models import (
    Company,
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentVikingSpace,
)
from apps.runtime.models import WorkspaceFile

from .identity import normalize_knowledge_path


class MigrationIntegrityReporter:
    """Produces a secret-free report suitable for migration acceptance."""

    def build(self, *, company=None) -> dict:
        companies = Company.objects.all()
        if company is not None:
            companies = companies.filter(pk=company.pk)
        reports = [self._company_report(item) for item in companies.order_by("id")]
        return {
            "schemaVersion": 1,
            "summary": self._summary(reports),
            "companies": reports,
        }

    def _company_report(self, company) -> dict:
        agents = TransparentCompanyAgentDefinition.objects.filter(
            transparentCompany__company_id=company.pk,
        ).order_by("id")
        files = WorkspaceFile.objects.filter(
            company=company,
            logical_path__startswith="knowledge/",
        ).order_by("id")
        agent_reports = [self._agent_report(company, agent) for agent in agents]
        file_reports = [self._file_report(item) for item in files]
        issues = [issue for item in agent_reports + file_reports for issue in item["issues"]]
        return {
            "company_id": company.pk,
            "cid": company.cid,
            "agent_count": len(agent_reports),
            "knowledge_file_count": len(file_reports),
            "agents": agent_reports,
            "knowledge_files": file_reports,
            "issues": issues,
        }

    def _agent_report(self, company, agent) -> dict:
        issues = self._viking_space_issues(company, agent)
        if agent.enabled and agent.status == TransparentCompanyAgentDefinition.STATUS_ACTIVE:
            try:
                config = ConfigResolver().resolve(company, agent.pk)
            except ConfigResolutionError as exc:
                issues.append(f"config:{exc}")
            else:
                issues.extend(self._policy_issues(config.tool_policy))
        return {
            "agent_pk": agent.pk,
            "agent_id": agent.agentId,
            "enabled": agent.enabled,
            "status": agent.status,
            "issues": issues,
        }

    @staticmethod
    def _viking_space_issues(company, agent) -> list[str]:
        digest = lambda value: hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:24]
        expected_account = f"tm-{digest(company.cid)}"
        expected_user = f"agent-{digest(f'{company.cid}:{agent.agentId}')}"
        expected = {
            "vikingSpaceId": f"{expected_account}:{expected_user}",
            "accountId": expected_account,
            "userId": expected_user,
            "rootUri": f"viking://user/{expected_user}/memories/",
        }
        space = TransparentCompanyAgentVikingSpace.objects.filter(agent=agent).first()
        if space is None:
            return ["viking_space:missing"]
        return [f"viking_space:{field}" for field, value in expected.items() if getattr(space, field) != value]

    @staticmethod
    def _policy_issues(tool_policy) -> list[str]:
        issues = []
        for section in ("skills", "tools", "knowledge"):
            policy = tool_policy.get(section) or {}
            denied = set(policy.get("deny") or ())
            action_names = ("read", "write", "delete") if section == "knowledge" else ("allow",)
            for action in action_names:
                values = policy.get(action)
                if values is None and action == "read":
                    values = policy.get("allow") or ()
                overlap = sorted(set(values or ()) & denied)
                if overlap:
                    issues.append(f"policy:{section}:{action}_deny_overlap:{','.join(overlap)}")
        return issues

    @staticmethod
    def _file_report(workspace_file) -> dict:
        issues = []
        try:
            normalize_knowledge_path(workspace_file.logical_path)
        except ValueError as exc:
            issues.append(f"knowledge_file:{exc}")
        if workspace_file.storage_backend != WorkspaceFile.STORAGE_TENCENT_COS:
            issues.append("knowledge_file:not_cos_backed")
        return {
            "file_id": workspace_file.pk,
            "logical_path": workspace_file.logical_path,
            "index_status": workspace_file.resource_sync_status,
            "issues": issues,
        }

    @staticmethod
    def _summary(reports) -> dict:
        issues = sum(len(item["issues"]) for item in reports)
        return {
            "company_count": len(reports),
            "agent_count": sum(item["agent_count"] for item in reports),
            "knowledge_file_count": sum(item["knowledge_file_count"] for item in reports),
            "issue_count": issues,
            "valid": issues == 0,
        }
