"""Read-only Phase 0 inventory helpers for the unified agent runtime.

The baseline intentionally records counts, migration state, and logical
OpenViking namespace mappings without exporting credentials or raw memory
content.  It is used before and after database backup/restore operations.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from django.apps import apps
from django.db import connections, transaction
from django.db.migrations.executor import MigrationExecutor


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class BaselineModelSpec:
    key: str
    app_label: str
    model_name: str


BASELINE_MODEL_SPECS: tuple[BaselineModelSpec, ...] = (
    BaselineModelSpec("companies", "company", "Company"),
    BaselineModelSpec("transparentCompanies", "company", "TransparentCompany"),
    BaselineModelSpec("agents", "company", "TransparentCompanyAgentDefinition"),
    BaselineModelSpec("agentPromptVersions", "company", "TransparentCompanyAgentPromptVersion"),
    BaselineModelSpec("skills", "company", "TransparentCompanySkill"),
    BaselineModelSpec("sessions", "company", "TransparentCompanyAgentTestSession"),
    BaselineModelSpec("messages", "company", "TransparentCompanyAgentTestMessage"),
    BaselineModelSpec("workspaceFiles", "runtime", "WorkspaceFile"),
    BaselineModelSpec("vikingSpaces", "company", "TransparentCompanyAgentVikingSpace"),
)


def _migration_key(migration: Any) -> str:
    return f"{migration.app_label}.{migration.name}"


def build_migration_inventory(*, using: str = "default") -> dict[str, Any]:
    """Return applied, pending, leaf, and conflicting migration information."""

    executor = MigrationExecutor(connections[using])
    loader = executor.loader
    targets = loader.graph.leaf_nodes()
    plan = executor.migration_plan(targets)
    pending = sorted(
        _migration_key(migration)
        for migration, backwards in plan
        if not backwards
    )
    conflicts = {
        app_label: sorted(names)
        for app_label, names in sorted(loader.detect_conflicts().items())
    }
    return {
        "applied": sorted(f"{app_label}.{name}" for app_label, name in loader.applied_migrations),
        "onDisk": sorted(f"{app_label}.{name}" for app_label, name in loader.disk_migrations),
        "leafNodes": sorted(f"{app_label}.{name}" for app_label, name in targets),
        "pending": pending,
        "conflicts": conflicts,
    }


def _count_models(*, using: str) -> dict[str, dict[str, Any]]:
    counts: dict[str, dict[str, Any]] = {}
    for spec in BASELINE_MODEL_SPECS:
        model = apps.get_model(spec.app_label, spec.model_name)
        counts[spec.key] = {
            "model": f"{spec.app_label}.{spec.model_name}",
            "table": model._meta.db_table,
            "count": model.objects.using(using).count(),
        }
    return counts


def _namespace_inventory(*, using: str) -> list[dict[str, Any]]:
    viking_space_model = apps.get_model("company", "TransparentCompanyAgentVikingSpace")
    queryset = viking_space_model.objects.using(using).select_related(
        "transparentCompany__company",
        "agent",
    ).order_by("transparentCompany__company__cid", "agent__agentId")
    return [
        {
            "companyCid": str(space.transparentCompany.company.cid),
            "agentId": str(space.agent.agentId),
            "vikingSpaceId": str(space.vikingSpaceId),
            "accountId": str(space.accountId),
            "userId": str(space.userId),
            "rootUri": str(space.rootUri),
            "status": str(space.status),
        }
        for space in queryset.iterator()
    ]


def _memory_samples(
    *,
    using: str,
    company_cids: Iterable[str],
    include_content: bool,
    limit: int,
) -> list[dict[str, Any]]:
    memory_model = apps.get_model("company", "CompanyUserMemoryItem")
    selected_cids = tuple(sorted({str(cid).strip() for cid in company_cids if str(cid).strip()}))
    if not selected_cids:
        return []
    queryset = memory_model.objects.using(using).select_related("company", "agent", "user").filter(
        company__cid__in=selected_cids
    ).order_by("company__cid", "agent__agentId", "id")[:limit]
    result: list[dict[str, Any]] = []
    for memory in queryset:
        content = str(memory.content or "")
        item = {
            "companyCid": str(memory.company.cid),
            "agentId": str(memory.agent.agentId) if memory.agent_id else "",
            "userId": str(memory.user.uid),
            "scope": str(memory.scope),
            "memoryKey": str(memory.memoryKey),
            "memoryType": str(memory.memoryType),
            "status": str(memory.status),
            "contentLength": len(content),
            "contentSha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
        if include_content:
            item["content"] = content
        result.append(item)
    return result


def _counts_checksum(counts: dict[str, dict[str, Any]]) -> str:
    canonical = {
        key: {
            "table": value["table"],
            "count": value["count"],
        }
        for key, value in sorted(counts.items())
    }
    serialized = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_data_baseline(
    *,
    using: str = "default",
    memory_company_cids: Iterable[str] = (),
    include_memory_content: bool = False,
    memory_sample_limit: int = 20,
) -> dict[str, Any]:
    """Build a consistent, credential-free database baseline report."""

    connection = connections[using]
    with transaction.atomic(using=using):
        counts = _count_models(using=using)
        namespaces = _namespace_inventory(using=using)
        samples = _memory_samples(
            using=using,
            company_cids=memory_company_cids,
            include_content=include_memory_content,
            limit=max(0, memory_sample_limit),
        )

    database_name = str(connection.settings_dict.get("NAME") or "")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "database": {
            "alias": using,
            "vendor": connection.vendor,
            "name": database_name,
        },
        "migrations": build_migration_inventory(using=using),
        "counts": counts,
        "countsChecksum": _counts_checksum(counts),
        "openViking": {
            "namespaceCount": len(namespaces),
            "namespaces": namespaces,
            "memorySamples": samples,
            "rawMemoryContentIncluded": bool(include_memory_content),
        },
    }


def compare_baseline_counts(
    current: dict[str, Any],
    expected: dict[str, Any],
) -> list[str]:
    """Compare logical table counts and return human-readable mismatches."""

    current_counts = current.get("counts") or {}
    expected_counts = expected.get("counts") or {}
    mismatches: list[str] = []
    for key in sorted(set(current_counts) | set(expected_counts)):
        current_item = current_counts.get(key)
        expected_item = expected_counts.get(key)
        if current_item is None:
            mismatches.append(f"{key}: current report is missing the count")
            continue
        if expected_item is None:
            mismatches.append(f"{key}: expected report is missing the count")
            continue
        if current_item.get("table") != expected_item.get("table"):
            mismatches.append(
                f"{key}: table changed from {expected_item.get('table')} "
                f"to {current_item.get('table')}"
            )
        if int(current_item.get("count", -1)) != int(expected_item.get("count", -1)):
            mismatches.append(
                f"{key}: expected {expected_item.get('count')}, "
                f"got {current_item.get('count')}"
            )
    return mismatches
