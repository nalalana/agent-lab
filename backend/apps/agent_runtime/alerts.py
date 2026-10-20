"""Operational alert evaluation and periodic notification dispatch."""

from __future__ import annotations

import logging
from datetime import timedelta, timezone as datetime_timezone
from typing import Any

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.agent_runtime.models import AgentRun, CompanyAgentRuntimePolicy, SandboxExecution
from apps.company.models import CompanyMember, TransparentCompany
from apps.notification.models import Notification
from apps.notification.services import NotificationService

logger = logging.getLogger("touming")

ACTIVE_SANDBOX_STATUSES = (
    SandboxExecution.STATUS_CREATING,
    SandboxExecution.STATUS_READY,
    SandboxExecution.STATUS_RUNNING,
)


def evaluate_runtime_alerts(company, *, now=None, agent_ids=None) -> dict[str, Any]:
    """Compute current alerts from PostgreSQL, the authoritative audit store."""

    observed_at = now or timezone.now()
    scoped = agent_ids is not None
    runs = AgentRun.objects.filter(company=company)
    sandboxes = SandboxExecution.objects.filter(company=company)
    if scoped:
        runs = runs.filter(agent__agentId__in=agent_ids)
        sandboxes = sandboxes.filter(run__agent__agentId__in=agent_ids)
    orphan_minutes = int(getattr(settings, "AGENT_RUNTIME_ALERT_ORPHAN_MINUTES", 15))
    failure_window_minutes = int(
        getattr(settings, "AGENT_RUNTIME_ALERT_FAILURE_WINDOW_MINUTES", 15)
    )
    failure_rate_threshold = float(
        getattr(settings, "AGENT_RUNTIME_ALERT_FAILURE_RATE", 0.2)
    )
    failure_min_runs = int(getattr(settings, "AGENT_RUNTIME_ALERT_FAILURE_MIN_RUNS", 10))
    quota_ratio_threshold = float(
        getattr(settings, "AGENT_RUNTIME_ALERT_QUOTA_USAGE_RATIO", 0.8)
    )
    agsx_failure_window_minutes = int(
        getattr(settings, "AGSX_ALERT_FAILURE_WINDOW_MINUTES", 15)
    )
    agsx_failure_rate_threshold = float(
        getattr(settings, "AGSX_ALERT_FAILURE_RATE", 0.2)
    )
    agsx_failure_min_creates = int(
        getattr(settings, "AGSX_ALERT_FAILURE_MIN_CREATES", 5)
    )
    global_sandbox_limit = max(
        int(getattr(settings, "AGSX_GLOBAL_MAX_CONCURRENT_SANDBOXES", 25)),
        1,
    )
    cost_per_second = max(
        int(
            getattr(
                settings,
                "AGSX_ESTIMATED_COST_MICRO_UNITS_PER_SECOND_2C2G",
                0,
            )
        ),
        0,
    )
    company_cost_budget = max(
        int(
            getattr(
                settings,
                "AGSX_DEFAULT_COMPANY_DAILY_COST_BUDGET_MICRO_UNITS",
                0,
            )
        ),
        0,
    )
    global_cost_budget = max(
        int(getattr(settings, "AGSX_GLOBAL_DAILY_COST_BUDGET_MICRO_UNITS", 0)),
        0,
    )
    cost_currency = str(getattr(settings, "AGSX_COST_CURRENCY", "CNY"))

    if scoped:
        # Company/platform budgets and global totals are outside Agent grants.
        company_cost_budget = 0
        global_cost_budget = 0
    orphan_cutoff = observed_at - timedelta(minutes=orphan_minutes)
    orphan_count = sandboxes.filter(
        status__in=ACTIVE_SANDBOX_STATUSES,
        created_at__lte=orphan_cutoff,
    ).count()
    company_active_sandboxes = sandboxes.filter(status__in=ACTIVE_SANDBOX_STATUSES).count()
    global_active_sandboxes = 0 if scoped else SandboxExecution.objects.filter(
        status__in=ACTIVE_SANDBOX_STATUSES,
    ).count()

    utc_day_start = observed_at.astimezone(datetime_timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    usage = runs.filter(created_at__gte=utc_day_start).aggregate(
        input_tokens=Sum("input_tokens"),
        output_tokens=Sum("output_tokens"),
        sandbox_seconds=Sum("sandbox_seconds"),
    )
    token_usage = int(usage["input_tokens"] or 0) + int(usage["output_tokens"] or 0)
    sandbox_usage = int(usage["sandbox_seconds"] or 0)
    global_sandbox_usage = 0 if scoped else int(
        AgentRun.objects.filter(created_at__gte=utc_day_start).aggregate(
            sandbox_seconds=Sum("sandbox_seconds")
        )["sandbox_seconds"]
        or 0
    )
    estimated_cost = sandbox_usage * cost_per_second
    global_estimated_cost = global_sandbox_usage * cost_per_second

    agsx_failure_cutoff = observed_at - timedelta(minutes=agsx_failure_window_minutes)
    sandbox_counts = sandboxes.filter(created_at__gte=agsx_failure_cutoff).aggregate(
        total=Count("id"),
        failed=Count("id", filter=Q(status=SandboxExecution.STATUS_FAILED)),
        create_failed=Count(
            "id",
            filter=Q(
                status=SandboxExecution.STATUS_FAILED,
                external_id="",
            ),
        ),
        cleanup_failed=Count(
            "id",
            filter=Q(
                status=SandboxExecution.STATUS_FAILED,
                external_id__gt="",
            ),
        ),
    )
    sandbox_total = int(sandbox_counts["total"] or 0)
    sandbox_failed = int(sandbox_counts["failed"] or 0)
    sandbox_create_failed = int(sandbox_counts["create_failed"] or 0)
    sandbox_cleanup_failed = int(sandbox_counts["cleanup_failed"] or 0)
    sandbox_failure_rate = sandbox_failed / sandbox_total if sandbox_total else 0.0

    failure_cutoff = observed_at - timedelta(minutes=failure_window_minutes)
    terminal_counts = runs.filter(
        completed_at__gte=failure_cutoff,
        status__in=(AgentRun.STATUS_COMPLETED, AgentRun.STATUS_FAILED),
    ).aggregate(
        total=Count("id"),
        failed=Count("id", filter=Q(status=AgentRun.STATUS_FAILED)),
    )
    terminal_total = int(terminal_counts["total"] or 0)
    failed_total = int(terminal_counts["failed"] or 0)
    failure_rate = failed_total / terminal_total if terminal_total else 0.0

    policy = None if scoped else CompanyAgentRuntimePolicy.objects.filter(company=company).first()
    alerts: list[dict[str, Any]] = []
    if orphan_count:
        alerts.append(
            _alert(
                "orphan_sandbox",
                "critical",
                orphan_count,
                0,
                f"发现 {orphan_count} 个持续超过 {orphan_minutes} 分钟的活动沙箱",
                observed_at,
            )
        )

    if policy:
        company_concurrency_limit = max(int(policy.max_concurrent_runs), 1)
        ratio = company_active_sandboxes / company_concurrency_limit
        if ratio >= quota_ratio_threshold:
            alerts.append(
                _alert(
                    "agsx_company_concurrency_quota",
                    "critical" if ratio >= 1 else "warning",
                    company_active_sandboxes,
                    company_concurrency_limit,
                    f"公司活动沙箱已达到并发上限的 {ratio:.1%}",
                    observed_at,
                )
            )

    global_ratio = global_active_sandboxes / global_sandbox_limit
    if global_ratio >= quota_ratio_threshold:
        alerts.append(
            _alert(
                "agsx_global_concurrency_quota",
                "critical" if global_ratio >= 1 else "warning",
                global_active_sandboxes,
                global_sandbox_limit,
                f"平台活动沙箱已达到全局并发上限的 {global_ratio:.1%}",
                observed_at,
            )
        )

    if policy and policy.daily_token_budget:
        ratio = token_usage / int(policy.daily_token_budget)
        if ratio >= quota_ratio_threshold:
            alerts.append(
                _alert(
                    "token_quota",
                    "critical" if ratio >= 1 else "warning",
                    token_usage,
                    int(policy.daily_token_budget),
                    f"今日 Token 用量已达到预算的 {ratio:.1%}",
                    observed_at,
                )
            )
    if policy and policy.daily_sandbox_seconds_budget:
        ratio = sandbox_usage / int(policy.daily_sandbox_seconds_budget)
        if ratio >= quota_ratio_threshold:
            alerts.append(
                _alert(
                    "sandbox_quota",
                    "critical" if ratio >= 1 else "warning",
                    sandbox_usage,
                    int(policy.daily_sandbox_seconds_budget),
                    f"今日沙箱用量已达到预算的 {ratio:.1%}",
                    observed_at,
                )
            )

    if cost_per_second and company_cost_budget:
        ratio = estimated_cost / company_cost_budget
        if ratio >= quota_ratio_threshold:
            alerts.append(
                _alert(
                    "agsx_company_daily_cost",
                    "critical" if ratio >= 1 else "warning",
                    estimated_cost,
                    company_cost_budget,
                    f"今日 AGSX 估算成本已达到公司预算的 {ratio:.1%}",
                    observed_at,
                )
            )
    if cost_per_second and global_cost_budget:
        ratio = global_estimated_cost / global_cost_budget
        if ratio >= quota_ratio_threshold:
            alerts.append(
                _alert(
                    "agsx_global_daily_cost",
                    "critical" if ratio >= 1 else "warning",
                    global_estimated_cost,
                    global_cost_budget,
                    f"今日 AGSX 估算成本已达到平台预算的 {ratio:.1%}",
                    observed_at,
                )
            )

    if (
        sandbox_total >= agsx_failure_min_creates
        and sandbox_failure_rate >= agsx_failure_rate_threshold
    ):
        alerts.append(
            _alert(
                "agsx_create_failure_rate",
                "critical" if sandbox_failure_rate >= 0.5 else "warning",
                round(sandbox_failure_rate, 4),
                agsx_failure_rate_threshold,
                (
                    f"最近 {agsx_failure_window_minutes} 分钟 AGSX 创建/运行失败率为 "
                    f"{sandbox_failure_rate:.1%}"
                ),
                observed_at,
            )
        )
    if sandbox_cleanup_failed:
        alerts.append(
            _alert(
                "agsx_cleanup_failure",
                "critical",
                sandbox_cleanup_failed,
                0,
                f"发现 {sandbox_cleanup_failed} 个需要 Reaper 继续清理的 AGSX 实例",
                observed_at,
            )
        )

    if terminal_total >= failure_min_runs and failure_rate >= failure_rate_threshold:
        alerts.append(
            _alert(
                "run_failure_rate",
                "critical" if failure_rate >= 0.5 else "warning",
                round(failure_rate, 4),
                failure_rate_threshold,
                f"最近 {failure_window_minutes} 分钟 Run 失败率为 {failure_rate:.1%}",
                observed_at,
            )
        )

    return {
        "schemaVersion": 1,
        "scope": "authorized_agents" if scoped else "company",
        "observedAt": observed_at.isoformat(),
        "alerts": alerts,
        "metrics": {
            "orphanSandboxCount": orphan_count,
            "companyActiveSandboxCount": company_active_sandboxes,
            "companyMaxConcurrentSandboxes": (
                max(int(policy.max_concurrent_runs), 1) if policy else None
            ),
            "globalActiveSandboxCount": None if scoped else global_active_sandboxes,
            "globalMaxConcurrentSandboxes": None if scoped else global_sandbox_limit,
            "dailyTokenUsage": token_usage,
            "dailyTokenBudget": policy.daily_token_budget if policy else None,
            "dailySandboxSeconds": sandbox_usage,
            "dailySandboxSecondsBudget": (
                policy.daily_sandbox_seconds_budget if policy else None
            ),
            "dailyEstimatedCostMicroUnits": estimated_cost,
            "companyDailyCostBudgetMicroUnits": company_cost_budget or None,
            "globalDailyEstimatedCostMicroUnits": None if scoped else global_estimated_cost,
            "globalDailyCostBudgetMicroUnits": global_cost_budget or None,
            "costCurrency": cost_currency,
            "agsxFailureWindowMinutes": agsx_failure_window_minutes,
            "sandboxCreateCount": sandbox_total,
            "sandboxFailedCount": sandbox_failed,
            "sandboxPreProviderFailureCount": sandbox_create_failed,
            "sandboxCleanupFailureCount": sandbox_cleanup_failed,
            "sandboxFailureRate": round(sandbox_failure_rate, 4),
            "failureWindowMinutes": failure_window_minutes,
            "terminalRunCount": terminal_total,
            "failedRunCount": failed_total,
            "failureRate": round(failure_rate, 4),
        },
    }


@shared_task(name="apps.agent_runtime.alerts.dispatch_runtime_alerts")
def dispatch_runtime_alerts() -> dict[str, int]:
    """Notify company owners/admins, with cache-backed duplicate suppression."""

    if not bool(getattr(settings, "AGENT_RUNTIME_ALERTS_ENABLED", True)):
        return {"companies": 0, "alerts": 0, "notifications": 0}

    dedup_seconds = int(getattr(settings, "AGENT_RUNTIME_ALERT_DEDUP_SECONDS", 900))
    prefix = str(
        getattr(settings, "AGENT_RUNTIME_ALERT_REDIS_PREFIX", "touming:agent-runtime:alerts")
    ).strip()
    company_count = alert_count = notification_count = 0
    for policy in CompanyAgentRuntimePolicy.objects.select_related("company", "company__account"):
        company_count += 1
        snapshot = evaluate_runtime_alerts(policy.company)
        alerts = snapshot["alerts"]
        alert_count += len(alerts)
        if not alerts:
            continue
        recipients = _alert_recipients(policy.company)
        transparent = TransparentCompany.objects.filter(company=policy.company).first()
        for alert in alerts:
            cache_key = f"{prefix}:{policy.company_id}:{alert['code']}"
            if not cache.add(cache_key, "1", timeout=dedup_seconds):
                continue
            for recipient in recipients:
                NotificationService.create_notification(
                    user=recipient,
                    type=Notification.TYPE_RUNTIME_CONFIG_ALERT,
                    title="Agent Runtime 运行告警",
                    content=f"{policy.company.company_name}：{alert['message']}",
                    transparentCompany=transparent,
                    extraData={
                        "schemaVersion": 1,
                        "companyCid": policy.company.cid,
                        "alert": alert,
                    },
                )
                notification_count += 1
        logger.warning(
            "agent_runtime_alerts company_id=%s alert_codes=%s",
            policy.company_id,
            [alert["code"] for alert in alerts],
        )
    return {
        "companies": company_count,
        "alerts": alert_count,
        "notifications": notification_count,
    }


def _alert_recipients(company):
    recipients = {}
    if (
        company.verification_status != company.VERIFICATION_STATUS_VERIFIED
        and company.account_id
    ):
        recipients[company.account_id] = company.account
    members = CompanyMember.objects.filter(
        company=company,
        status=CompanyMember.STATUS_ACTIVE,
        role__in=("owner", "admin"),
        user__isnull=False,
    ).select_related("user")
    for member in members:
        recipients[member.user_id] = member.user
    return tuple(recipients.values())


def _alert(code, severity, current, threshold, message, observed_at):
    return {
        "code": code,
        "severity": severity,
        "current": current,
        "threshold": threshold,
        "message": message,
        "observedAt": observed_at.isoformat(),
    }
