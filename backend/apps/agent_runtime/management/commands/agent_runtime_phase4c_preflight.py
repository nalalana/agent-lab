"""Read-only release gate for the Phase 4-C infrastructure retirement."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.db.models import Count

from apps.agent_runtime.baseline import build_migration_inventory
from apps.agent_runtime.models import AgentRun, CompanyAgentRuntimePolicy, SandboxExecution
from apps.company.models import Company
from apps.runtime.legacy_archive import LegacyArchiveError, LegacyRuntimeArchive


ACTIVE_RUN_STATUSES = (
    AgentRun.STATUS_PENDING,
    AgentRun.STATUS_RUNNING,
    AgentRun.STATUS_WAITING_TOOL,
    AgentRun.STATUS_CANCELLING,
)
ACTIVE_SANDBOX_STATUSES = (
    SandboxExecution.STATUS_CREATING,
    SandboxExecution.STATUS_READY,
    SandboxExecution.STATUS_RUNNING,
)
LEGACY_MODEL_SPECS = (
    ("instances", "OpenClawInstance"),
    ("provisionTasks", "OpenClawProvisionTask"),
    ("runtimeBindings", "CompanyRuntimeBinding"),
    ("runtimeSnapshots", "OpenClawRuntimeSnapshot"),
    ("configVersions", "OpenClawConfigVersion"),
)
LEGACY_RUNTIME_ENV_NAMES = (
    "RUNTIME_DOCKER_SHARED_HOST_API_BASE",
    "RUNTIME_DOCKER_SHARED_HOST_API_TOKEN",
    "RUNTIME_DOCKER_SHARED_HOST_TIMEOUT",
    "RUNTIME_DOCKER_SHARED_HOST_BOOTSTRAP_TIMEOUT",
    "TENCENT_LIGHTHOUSE_API_ENABLED",
    "TENCENT_LIGHTHOUSE_REGION",
    "TENCENT_LIGHTHOUSE_ZONE",
    "TENCENT_LIGHTHOUSE_BUNDLE_ID",
    "TENCENT_LIGHTHOUSE_BLUEPRINT_ID",
    "TENCENT_LIGHTHOUSE_PERIOD",
    "TENCENT_LIGHTHOUSE_RENEW_FLAG",
    "TENCENT_LIGHTHOUSE_SECURITY_GROUP_ID",
    "TENCENT_LIGHTHOUSE_FIREWALL_TEMPLATE_ID",
    "TENCENT_LIGHTHOUSE_KEY_ID",
    "TENCENT_LIGHTHOUSE_API_POLL_INTERVAL",
    "TENCENT_LIGHTHOUSE_API_WAIT_TIMEOUT",
    "TENCENT_LIGHTHOUSE_INSTANCE_ID",
    "TENCENT_LIGHTHOUSE_PUBLIC_IP",
    "TENCENT_LIGHTHOUSE_PRIVATE_IP",
    "TENCENT_LIGHTHOUSE_SSH_USER",
    "TENCENT_LIGHTHOUSE_SSH_PORT",
    "TENCENT_LIGHTHOUSE_SSH_PRIVATE_KEY_PATH",
    "TENCENT_LIGHTHOUSE_SSH_STRICT_HOST_KEY",
    "TENCENT_LIGHTHOUSE_SSH_CONNECT_TIMEOUT",
    "TENCENT_LIGHTHOUSE_BOOTSTRAP_TIMEOUT",
    "TENCENT_LIGHTHOUSE_BRIDGE_PORT",
    "TENCENT_LIGHTHOUSE_RUNTIME_ROOT",
    "TSDS_OPENCLAW_IMAGE",
    "TSDS_WORKSPACE_AGENT_IMAGE",
    "TSDS_DEBIAN_MIRROR",
    "TSDS_DEBIAN_SECURITY_MIRROR",
    "TSDS_NPM_REGISTRY",
)


def build_phase4c_preflight(
    *,
    using: str = "default",
    require_bridge_disabled: bool = False,
    require_legacy_archive: bool = False,
    legacy_archive_manifest: str = "",
) -> dict[str, Any]:
    """Build a secret-free report without changing database or infrastructure state."""

    connection = connections[using]
    checks: list[dict[str, Any]] = []

    def add_check(check_id: str, status: str, message: str, observed: Any) -> None:
        checks.append(
            {
                "id": check_id,
                "status": status,
                "message": message,
                "observed": observed,
            }
        )

    migrations = build_migration_inventory(using=using)
    migration_blocked = bool(migrations["pending"] or migrations["conflicts"])
    add_check(
        "migrations",
        "block" if migration_blocked else "pass",
        "数据库必须不存在待应用或冲突 Migration",
        {
            "pending": migrations["pending"],
            "conflicts": migrations["conflicts"],
        },
    )

    legacy_write_mode = str(
        getattr(settings, "OPENCLAW_LEGACY_WRITE_MODE", "read_only")
    ).strip().lower()
    legacy_read_only = legacy_write_mode == "read_only"
    add_check(
        "legacy_runtime_read_only",
        "pass" if legacy_read_only else "block",
        "Phase4-B 旧 OpenClaw 数据面必须保持 read_only",
        {"mode": legacy_write_mode},
    )

    company_count = Company.objects.using(using).count()
    policy_rows = list(
        CompanyAgentRuntimePolicy.objects.using(using)
        .values("mode")
        .annotate(count=Count("id"))
        .order_by("mode")
    )
    policy_counts = {str(row["mode"]): int(row["count"]) for row in policy_rows}
    policy_count = sum(policy_counts.values())
    non_unified_count = policy_count - policy_counts.get(
        CompanyAgentRuntimePolicy.MODE_UNIFIED,
        0,
    )
    missing_policy_count = max(company_count - policy_count, 0)
    policies_blocked = bool(non_unified_count or missing_policy_count)
    add_check(
        "unified_runtime_policies",
        "block" if policies_blocked else "pass",
        "所有公司必须显式使用 unified 策略",
        {
            "companies": company_count,
            "policies": policy_count,
            "missingPolicies": missing_policy_count,
            "modeCounts": policy_counts,
        },
    )

    active_runs = AgentRun.objects.using(using).filter(status__in=ACTIVE_RUN_STATUSES).count()
    add_check(
        "active_agent_runs",
        "block" if active_runs else "pass",
        "停用旧基础设施前不得有进行中的 AgentRun",
        active_runs,
    )

    active_sandboxes = SandboxExecution.objects.using(using).filter(
        status__in=ACTIVE_SANDBOX_STATUSES
    ).count()
    cleanup_candidates = SandboxExecution.objects.using(using).filter(
        status=SandboxExecution.STATUS_FAILED,
        external_id__gt="",
    ).count()
    add_check(
        "active_agsx_sandboxes",
        "block" if active_sandboxes else "pass",
        "停用窗口开始前不得有活动 AGSX 沙箱",
        active_sandboxes,
    )
    add_check(
        "agsx_cleanup_candidates",
        "block" if cleanup_candidates else "pass",
        "带供应商实例 ID 的失败沙箱必须先由 Reaper 清理",
        cleanup_candidates,
    )

    agsx_observed = {
        "enabled": bool(getattr(settings, "AGSX_ENABLED", False)),
        "domainConfigured": bool(str(getattr(settings, "E2B_DOMAIN", "")).strip()),
        "credentialConfigured": bool(str(getattr(settings, "E2B_API_KEY", "")).strip()),
        "templateConfigured": bool(str(getattr(settings, "AGSX_TEMPLATE", "")).strip()),
        "globalConcurrencyLimit": int(
            getattr(settings, "AGSX_GLOBAL_MAX_CONCURRENT_SANDBOXES", 0)
        ),
        "quotaBackendFailClosed": bool(
            getattr(settings, "AGSX_FAIL_CLOSED_ON_QUOTA_BACKEND_ERROR", False)
        ),
        "costRateConfigured": int(
            getattr(settings, "AGSX_ESTIMATED_COST_MICRO_UNITS_PER_SECOND_2C2G", 0)
        )
        > 0,
        "companyCostBudgetConfigured": int(
            getattr(
                settings,
                "AGSX_DEFAULT_COMPANY_DAILY_COST_BUDGET_MICRO_UNITS",
                0,
            )
        )
        > 0,
        "globalCostBudgetConfigured": int(
            getattr(settings, "AGSX_GLOBAL_DAILY_COST_BUDGET_MICRO_UNITS", 0)
        )
        > 0,
    }
    agsx_blocked = not all(
        (
            agsx_observed["enabled"],
            agsx_observed["domainConfigured"],
            agsx_observed["credentialConfigured"],
            agsx_observed["templateConfigured"],
            agsx_observed["globalConcurrencyLimit"] > 0,
            agsx_observed["quotaBackendFailClosed"],
            agsx_observed["costRateConfigured"],
            agsx_observed["companyCostBudgetConfigured"],
            agsx_observed["globalCostBudgetConfigured"],
        )
    )
    add_check(
        "agsx_production_configuration",
        "block" if agsx_blocked else "pass",
        "AGSX、全局配额、成本估算与 fail-closed 必须完成生产配置",
        agsx_observed,
    )

    configured_legacy_env_names = sorted(
        name
        for name in LEGACY_RUNTIME_ENV_NAMES
        if str(os.environ.get(name, "")).strip()
        or str(getattr(settings, name, "")).strip()
    )
    bridge_observed = {"configuredNames": configured_legacy_env_names}
    bridge_configured = bool(configured_legacy_env_names)
    bridge_status = "block" if require_bridge_disabled and bridge_configured else (
        "warning" if bridge_configured else "pass"
    )
    add_check(
        "legacy_bridge_configuration",
        bridge_status,
        "停服前可保留旧配置用于回滚；最终部署必须清空 Bridge/Lighthouse 活动变量",
        bridge_observed,
    )

    legacy_counts = {}
    for key, model_name in LEGACY_MODEL_SPECS:
        model = apps.get_model("runtime", model_name)
        legacy_counts[key] = model.objects.using(using).count()
    provision_model = apps.get_model("runtime", "OpenClawProvisionTask")
    active_legacy_tasks = provision_model.objects.using(using).filter(
        status__in=(provision_model.STATUS_PENDING, provision_model.STATUS_RUNNING)
    ).count()
    add_check(
        "legacy_provision_tasks",
        "block" if active_legacy_tasks else "pass",
        "不得存在待执行或执行中的旧 Provision/Bootstrap/Deploy Bridge 任务",
        active_legacy_tasks,
    )

    archive_observed = {
        "configured": bool(str(legacy_archive_manifest or "").strip()),
        "valid": False,
    }
    archive_status = "warning"
    if archive_observed["configured"]:
        try:
            archive_result = LegacyRuntimeArchive().verify(legacy_archive_manifest)
        except (LegacyArchiveError, OSError, ValueError):
            archive_status = "block"
            archive_observed["errorCode"] = "legacy_archive_verification_failed"
        else:
            archive_observed.update(
                {
                    "valid": bool(archive_result.get("valid")),
                    "archiveFilesValid": bool(archive_result.get("archiveFilesValid")),
                    "databaseUnchanged": bool(archive_result.get("databaseUnchanged")),
                    "phase3IntegrityValid": bool(
                        (archive_result.get("phase3Integrity") or {})
                        .get("summary", {})
                        .get("valid")
                    ),
                    "openVikingBackfillValid": bool(
                        (archive_result.get("openVikingBackfill") or {}).get("countMatches")
                        and (archive_result.get("openVikingBackfill") or {}).get("unchanged")
                    ),
                }
            )
            archive_status = "pass" if archive_observed["valid"] else "block"
    elif require_legacy_archive:
        archive_status = "block"
    add_check(
        "legacy_archive_integrity",
        archive_status,
        "正式停用前必须验证 Phase4-B 归档、数据库不可变性和 OpenViking 回填",
        archive_observed,
    )

    blocker_count = sum(item["status"] == "block" for item in checks)
    warning_count = sum(item["status"] == "warning" for item in checks)
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "database": {"alias": using, "vendor": connection.vendor},
        "summary": {
            "ready": blocker_count == 0,
            "blockers": blocker_count,
            "warnings": warning_count,
        },
        "checks": checks,
        "legacyDataCounts": legacy_counts,
    }


def _write_json_atomic(target: Path, payload: dict[str, Any]) -> None:
    target = target.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


class Command(BaseCommand):
    help = "Run the read-only Phase4-C release gate and emit a secret-free JSON report."

    def add_arguments(self, parser):
        parser.add_argument("--database", default="default")
        parser.add_argument("--output")
        parser.add_argument("--require-bridge-disabled", action="store_true")
        parser.add_argument("--require-legacy-archive", action="store_true")
        parser.add_argument("--legacy-archive-manifest", default="")
        parser.add_argument("--fail-on-blockers", action="store_true")

    def handle(self, *args, **options):
        report = build_phase4c_preflight(
            using=options["database"],
            require_bridge_disabled=options["require_bridge_disabled"],
            require_legacy_archive=options["require_legacy_archive"],
            legacy_archive_manifest=options["legacy_archive_manifest"],
        )
        output = options.get("output")
        if output:
            target = Path(output)
            _write_json_atomic(target, report)
            self.stdout.write(self.style.SUCCESS(f"Phase4-C preflight written to {target}"))
        else:
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        if options["fail_on_blockers"] and report["summary"]["blockers"]:
            raise CommandError(
                f"Phase4-C preflight has {report['summary']['blockers']} blocker(s)"
            )
