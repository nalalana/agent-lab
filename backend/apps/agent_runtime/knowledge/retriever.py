"""Permission-filtered knowledge recall prepared for Phase 3-A integration."""

from __future__ import annotations

from dataclasses import dataclass

from apps.agent_runtime.config import ConfigResolver
from apps.agent_runtime.memory.client import OpenVikingClient
from apps.agent_runtime.memory.errors import MemoryPermissionDenied, OpenVikingUnavailable
from apps.company.models import Company
from apps.runtime.models import WorkspaceFile

from .identity import company_knowledge_namespace, normalize_knowledge_path


@dataclass(frozen=True)
class KnowledgeItem:
    content: str
    score: float
    source_ref: str
    logical_path: str


class KnowledgeRetriever:
    """Search only completed resources allowed by the Agent's frozen policy."""

    MAX_QUERY_LENGTH = 8000

    def __init__(self, client=None, config_resolver=None):
        self.client = client or OpenVikingClient.from_settings()
        self.config_resolver = config_resolver or ConfigResolver()

    def recall(self, *, company_id, agent_id, query: str, top_k: int = 8) -> list[KnowledgeItem]:
        query = str(query or "").strip()
        if not query or len(query) > self.MAX_QUERY_LENGTH:
            raise ValueError("Knowledge query length is invalid.")
        if top_k < 1 or top_k > 50:
            raise ValueError("Knowledge top_k must be between 1 and 50.")
        company = self._company(company_id)
        config = self.config_resolver.resolve(company, agent_id)
        allowed_files = self._allowed_files(company, config.tool_policy.get("knowledge") or {})
        try:
            items = self._search(company, allowed_files, query, top_k)
        except OpenVikingUnavailable:
            return []
        return sorted(items, key=lambda item: item.score, reverse=True)[:top_k]

    def _search(self, company, files, query, top_k):
        namespace = company_knowledge_namespace(company)
        items = []
        for workspace_file in files:
            results = self.client.search_resources(
                namespace,
                query=query,
                target_uri=workspace_file.resource_uri,
                limit=top_k,
            )
            items.extend(self._convert(results, workspace_file))
        return items

    @staticmethod
    def _convert(results, workspace_file) -> list[KnowledgeItem]:
        items = []
        for result in results:
            uri = str(result.get("uri") or "")
            target = workspace_file.resource_uri.rstrip("/")
            if uri != target and not uri.startswith(f"{target}/"):
                raise MemoryPermissionDenied("OpenViking returned knowledge outside the authorized resource.")
            items.append(
                KnowledgeItem(
                    content=str(result.get("content") or result.get("abstract") or result.get("overview") or ""),
                    score=float(result.get("score") or 0.0),
                    source_ref=uri,
                    logical_path=workspace_file.logical_path,
                )
            )
        return items

    @staticmethod
    def _allowed_files(company, policy):
        read_scopes = policy.get("read")
        if read_scopes is None:
            read_scopes = policy.get("allow") or ()
        allow = tuple(_normalize_scope(item) for item in read_scopes)
        deny = tuple(_normalize_scope(item) for item in policy.get("deny") or ())
        if not allow:
            return []
        files = WorkspaceFile.objects.filter(
            company=company,
            logical_path__startswith="knowledge/",
            resource_sync_status=WorkspaceFile.RESOURCE_SYNC_COMPLETED,
        ).exclude(resource_uri="")
        return [
            item
            for item in files
            if _matches_any(item.logical_path, allow) and not _matches_any(item.logical_path, deny)
        ]

    @staticmethod
    def _company(company_id):
        try:
            return Company.objects.get(pk=company_id)
        except (Company.DoesNotExist, TypeError, ValueError) as exc:
            raise MemoryPermissionDenied("Knowledge company context is invalid.") from exc


def _normalize_scope(value: str) -> str:
    return normalize_knowledge_path(value).rstrip("/")


def _matches_any(path: str, scopes) -> bool:
    normalized = normalize_knowledge_path(path)
    return any(normalized == scope or normalized.startswith(f"{scope}/") for scope in scopes)
