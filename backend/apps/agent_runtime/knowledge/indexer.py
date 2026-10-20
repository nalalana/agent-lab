"""Move one COS-backed knowledge file into OpenViking."""

from __future__ import annotations

from common.utils.Tengcentcos_utils import get_presigned_url

from apps.agent_runtime.memory.client import OpenVikingClient
from apps.runtime.models import WorkspaceFile

from .identity import company_knowledge_namespace, knowledge_target_uri, normalize_knowledge_path


class KnowledgeIndexError(ValueError):
    """A non-retryable source-file validation error."""

    code = "KNOWLEDGE_SOURCE_INVALID"


class KnowledgeIndexer:
    """Indexes only trusted WorkspaceFile metadata; callers cannot supply URLs."""

    PRESIGNED_URL_TTL_SECONDS = 900

    def __init__(self, client=None, url_factory=None):
        self.client = client or OpenVikingClient.from_settings()
        self.url_factory = url_factory or self._presigned_url

    def index(self, workspace_file: WorkspaceFile) -> dict:
        self._validate_source(workspace_file)
        namespace = company_knowledge_namespace(workspace_file.company)
        target_uri = knowledge_target_uri(workspace_file, namespace)
        source_url = self.url_factory(workspace_file.storage_key)
        result = self.client.add_resource(
            namespace,
            source_url=source_url,
            target_uri=target_uri,
        )
        return {
            "resource_uri": str(result.get("root_uri") or target_uri),
            "openviking_task_id": str(result.get("task_id") or ""),
        }

    @staticmethod
    def _validate_source(workspace_file: WorkspaceFile) -> None:
        normalize_knowledge_path(workspace_file.logical_path)
        if workspace_file.storage_backend != WorkspaceFile.STORAGE_TENCENT_COS:
            raise KnowledgeIndexError("Knowledge source must be stored in Tencent COS.")
        if not str(workspace_file.storage_key or "").strip():
            raise KnowledgeIndexError("Knowledge source is missing its COS storage key.")

    @classmethod
    def _presigned_url(cls, storage_key: str) -> str:
        return get_presigned_url(storage_key, expires_in=cls.PRESIGNED_URL_TTL_SECONDS)
