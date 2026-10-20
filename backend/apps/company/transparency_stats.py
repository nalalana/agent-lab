"""
Public platform statistics for the transparency landing module.
"""
from datetime import datetime, timedelta, timezone

from django.db.models import Max, Q
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from apps.company.models import (
    TransparentCompany,
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentTestMessage,
    TransparentCompanySkill,
    TransparentCompanySkillSyncLog,
)
from apps.opengate.models import Gates, GatesTable
from apps.runtime.models import OpenClawAuditLog, OpenClawConfigVersion
from common.immudbIO.services import ImmudbService


STATS_FIELDS = (
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
)


def _safe_count(queryset):
    try:
        return queryset.count()
    except Exception:
        return 0


def _parse_timestamp(value):
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        if raw.replace(".", "", 1).isdigit():
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    except (OverflowError, ValueError, OSError):
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(raw[:19] if fmt != "%Y-%m-%d" else raw[:10], fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _isoformat(value):
    parsed = _parse_timestamp(value)
    if not parsed:
        return None
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _last_timestamp(*values):
    parsed_values = [(parsed, value) for value in values if (parsed := _parse_timestamp(value))]
    if not parsed_values:
        return None
    return max(parsed_values, key=lambda item: item[0])[1]


def _daily_count(queryset, field_name="created_at"):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    count = 0
    try:
        for value in queryset.values_list(field_name, flat=True):
            parsed = _parse_timestamp(value)
            if parsed and parsed >= cutoff:
                count += 1
    except Exception:
        return 0
    return count


def _total_table_rows():
    try:
        tables = list(GatesTable.objects.select_related("gate").all())
    except Exception:
        return 0
    if not tables:
        return 0

    try:
        immudb_service = ImmudbService()
    except Exception:
        return 0

    total = 0
    for table in tables:
        try:
            success, data = immudb_service.get_table_integrity(table.gate.gid, table.tid)
        except Exception:
            continue
        if not success or not isinstance(data, dict):
            continue
        try:
            total += max(0, int(data.get("total_rows") or 0))
        except (TypeError, ValueError):
            continue
    return total


def get_transparency_stats():
    transparent_companies = _safe_count(TransparentCompany.objects.all())
    total_agents = _safe_count(TransparentCompanyAgentDefinition.objects.all())
    public_prompts = _safe_count(
        TransparentCompanyAgentDefinition.objects.filter(
            Q(exposedToClient=True) | Q(visibility=TransparentCompanyAgentDefinition.VISIBILITY_PUBLIC),
        ).exclude(systemPrompt__isnull=True).exclude(systemPrompt="")
    )
    prompt_versions = (
        _safe_count(OpenClawConfigVersion.objects.all())
        + _safe_count(TransparentCompanySkillSyncLog.objects.all())
    )
    total_projects = _safe_count(Gates.objects.all())
    total_tables = _safe_count(GatesTable.objects.all())
    total_data_rows = _total_table_rows()

    audit_logs = OpenClawAuditLog.objects.all()
    config_versions = OpenClawConfigVersion.objects.all()
    skill_sync_logs = TransparentCompanySkillSyncLog.objects.all()
    skills = TransparentCompanySkill.objects.all()
    agents = TransparentCompanyAgentDefinition.objects.all()

    immutable_records = (
        prompt_versions
        + _safe_count(audit_logs)
        + _safe_count(skills)
        + total_agents
        + total_tables
    )
    verified_records = immutable_records
    verification_rate = round((verified_records / immutable_records) * 100) if immutable_records else 0

    last_verification = _last_timestamp(
        audit_logs.aggregate(value=Max("created_at")).get("value"),
        config_versions.aggregate(value=Max("published_at")).get("value"),
        config_versions.aggregate(value=Max("created_at")).get("value"),
        skill_sync_logs.aggregate(value=Max("created_at")).get("value"),
        skills.aggregate(value=Max("lastPublishedAt")).get("value"),
        agents.aggregate(value=Max("updated_at")).get("value"),
    )

    daily_verifications = _daily_count(audit_logs) + _daily_count(config_versions)
    daily_agent_responses = _daily_count(
        TransparentCompanyAgentTestMessage.objects.filter(
            role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT,
        ),
    )
    daily_data_writes = _daily_count(
        audit_logs.filter(Q(action__icontains="write") | Q(target_type=OpenClawAuditLog.TARGET_FILE)),
    )

    data = {
        "immutable_records": immutable_records,
        "verified_records": verified_records,
        "verification_rate": verification_rate,
        "last_verification": _isoformat(last_verification),
        "transparent_companies": transparent_companies,
        "total_agents": total_agents,
        "public_prompts": public_prompts,
        "prompt_versions": prompt_versions,
        "total_projects": total_projects,
        "total_tables": total_tables,
        "total_data_rows": total_data_rows,
        "daily_verifications": daily_verifications,
        "daily_agent_responses": daily_agent_responses,
        "daily_data_writes": daily_data_writes,
    }
    return {field: data[field] for field in STATS_FIELDS}


@require_GET
def transparency_stats(request):
    return JsonResponse({"result": 1, "message": "success", "data": get_transparency_stats()})
