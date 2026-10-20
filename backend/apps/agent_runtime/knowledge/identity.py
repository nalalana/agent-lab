"""Trusted OpenViking identity and URI helpers for company knowledge."""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath

from apps.agent_runtime.memory.namespaces import MemoryNamespace
from apps.agent_runtime.memory.errors import MemoryConfigurationError
from apps.company.models import CompanyVikingBinding


def company_knowledge_namespace(company) -> MemoryNamespace:
    """Keep resources inside the company account selected by the backend."""
    try:
        binding = CompanyVikingBinding.objects.get(
            company=company,
            status=CompanyVikingBinding.STATUS_ACTIVE,
        )
    except CompanyVikingBinding.DoesNotExist as exc:
        raise MemoryConfigurationError('Active OpenViking company binding is missing.') from exc
    user_id = binding.runtimeUserId
    return MemoryNamespace(
        scope="company_knowledge",
        account_id=binding.accountId,
        user_id=user_id,
        root_uri=f"viking://resources/{user_id}/",
        credential_ref=binding.runtimeCredentialRef,
    )


def knowledge_target_uri(workspace_file, namespace: MemoryNamespace) -> str:
    """Build a content-versioned URI so retries cannot duplicate an index."""
    logical_path = normalize_knowledge_path(workspace_file.logical_path)
    basename = _safe_basename(PurePosixPath(logical_path).name)
    version_seed = workspace_file.checksum or workspace_file.storage_key
    version = _digest(version_seed)
    return f"{namespace.root_uri}{workspace_file.pk}/{version}/{basename}"


def normalize_knowledge_path(value: str) -> str:
    path = str(value or "").strip().replace("\\", "/").lstrip("/")
    parts = PurePosixPath(path).parts
    if not parts or parts[0] != "knowledge" or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("WorkspaceFile must use a safe knowledge/ logical path.")
    return "/".join(parts)


def knowledge_idempotency_key(workspace_file) -> str:
    version_seed = workspace_file.checksum or workspace_file.storage_key
    digest = hashlib.sha256(str(version_seed).encode("utf-8")).hexdigest()
    path_digest = hashlib.sha256(
        normalize_knowledge_path(workspace_file.logical_path).encode("utf-8")
    ).hexdigest()[:16]
    return f"knowledge:{workspace_file.company_id}:{workspace_file.pk}:{path_digest}:{digest}"


def _safe_basename(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return sanitized or "resource"


def _digest(value) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:24]
