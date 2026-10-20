"""Secret-free projections used by Agent Runtime operations pages."""

from __future__ import annotations

from datetime import datetime, timezone as datetime_timezone
from typing import Any

from django.db.models import Q
from django.utils import timezone

from apps.agent_runtime.enums import RunEventType
from apps.agent_runtime.models import AgentRun, SandboxExecution

SCHEMA_VERSION = 1
_SENSITIVE_KEYS = (
    "api_key",
    "authorization",
    "credential",
    "external_id",
    "password",
    "prompt",
    "secret",
    "token",
)


def list_company_runs(company, filters: dict[str, Any], *, agent_ids=None) -> dict[str, Any]:
    """Return a deterministic cursor page scoped to an authorized company."""

    queryset = AgentRun.objects.filter(company=company).select_related(
        "agent", "user", "session"
    )
    if agent_ids is not None:
        queryset = queryset.filter(agent__agentId__in=agent_ids)
    for field in ("status", "source"):
        if filters.get(field):
            queryset = queryset.filter(**{field: filters[field]})
    if filters.get("agent_id"):
        # Phase3-A exposed the database id while the operations console uses
        # the stable business id. Accept both during the compatibility window.
        agent_id = str(filters["agent_id"])
        agent_query = Q(agent__agentId=agent_id)
        if agent_id.isdigit():
            agent_query |= Q(agent_id=int(agent_id))
        queryset = queryset.filter(agent_query)
    if filters.get("user_id"):
        queryset = queryset.filter(user__uid=filters["user_id"])
    if filters.get("session_id"):
        queryset = queryset.filter(session__conversationId=filters["session_id"])
    if filters.get("trace_id"):
        queryset = queryset.filter(metadata__trace_id=filters["trace_id"])
    if filters.get("created_from"):
        queryset = queryset.filter(created_at__gte=filters["created_from"])
    if filters.get("created_to"):
        queryset = queryset.filter(created_at__lte=filters["created_to"])

    # Keep the Phase3-A total/offset projection alongside Phase3-C cursor
    # pagination so existing admin clients can move without a flag day.
    total = queryset.count()
    cursor = filters.get("cursor")
    if cursor:
        cursor_run = queryset.select_related(None).filter(pk=cursor).only(
            "id", "created_at"
        ).first()
        if cursor_run:
            queryset = queryset.filter(
                Q(created_at__lt=cursor_run.created_at)
                | Q(created_at=cursor_run.created_at, id__lt=cursor_run.id)
            )

    limit = filters.get("limit", 25)
    offset = filters.get("offset", 0)
    rows = list(
        queryset.order_by("-created_at", "-id")[offset : offset + limit + 1]
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [serialize_run_summary(run) for run in rows],
        "page": {
            "limit": limit,
            "hasMore": has_more,
            "nextCursor": str(rows[-1].id) if has_more and rows else None,
        },
    }


def runtime_metrics(company, filters: dict[str, Any] | None = None, *, agent_ids=None) -> dict[str, Any]:
    filters = dict(filters or {})
    window_minutes = max(int(filters.get("window_minutes") or 1440), 1)
    since = timezone.now() - timezone.timedelta(minutes=window_minutes)
    queryset = AgentRun.objects.filter(company=company, created_at__gte=since)
    if agent_ids is not None:
        queryset = queryset.filter(agent__agentId__in=agent_ids)
    runs = list(queryset.prefetch_related("tool_calls").order_by("-created_at"))
    completed = [run for run in runs if run.completed_at]
    first_token_values = [
        _milliseconds(run.started_at or run.created_at, run.first_token_at)
        for run in runs
        if run.first_token_at
    ]
    total_values = [
        _milliseconds(run.started_at or run.created_at, run.completed_at)
        for run in completed
    ]
    model_failures = [
        run for run in runs
        if run.status == AgentRun.STATUS_FAILED
        and str(run.error_code or "").startswith("MODEL_")
    ]
    tool_calls = [call for run in runs for call in run.tool_calls.all()]
    failed_tool_calls = [call for call in tool_calls if call.status == "failed"]
    route_counts: dict[str, int] = {}
    for run in runs:
        route_counts[run.engine] = route_counts.get(run.engine, 0) + 1
    return {
        "schemaVersion": SCHEMA_VERSION,
        "windowMinutes": window_minutes,
        "runCount": len(runs),
        "routeCounts": route_counts,
        "firstTokenMs": _metric_summary(first_token_values),
        "totalDurationMs": _metric_summary(total_values),
        "modelErrorRate": _rate(len(model_failures), len(runs)),
        "toolErrorRate": _rate(len(failed_tool_calls), len(tool_calls)),
        "modelErrorCount": len(model_failures),
        "toolErrorCount": len(failed_tool_calls),
        "toolCallCount": len(tool_calls),
        "generatedAt": _iso(timezone.now()),
    }
def serialize_run_summary(run: AgentRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        # Safe Phase3-A aliases are intentionally retained for existing admin
        # clients. Secret-bearing metadata and tool arguments stay excluded.
        "request_id": run.request_id,
        "agent_id": run.agent_id,
        "agent_business_id": run.agent.agentId,
        "session_id": run.session_id,
        "model_provider": run.model_provider,
        "model_name": run.model_name,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "sandbox_seconds": run.sandbox_seconds,
        "error_code": run.error_code,
        "started_at": _iso(run.started_at),
        "first_token_at": _iso(run.first_token_at),
        "completed_at": _iso(run.completed_at),
        "created_at": _iso(run.created_at),
        "requestId": run.request_id,
        "traceId": str((run.metadata or {}).get("trace_id", "")),
        "status": run.status,
        "source": run.source,
        "engine": run.engine,
        "agent": {
            "id": str(run.agent.agentId),
            "name": run.agent.name,
        },
        "userId": str(run.user.uid) if run.user_id else None,
        "sessionId": str(run.session.conversationId),
        "model": {
            "provider": run.model_provider,
            "name": run.model_name,
        },
        "usage": {
            "inputTokens": run.input_tokens,
            "outputTokens": run.output_tokens,
            "sandboxSeconds": run.sandbox_seconds,
        },
        "timing": _timing(run),
        "error": {
            "code": run.error_code,
            "message": run.error_message,
        }
        if run.error_code
        else None,
        "createdAt": _iso(run.created_at),
    }


def serialize_run_detail(run: AgentRun) -> dict[str, Any]:
    data = serialize_run_summary(run)
    tool_calls = list(run.tool_calls.all().order_by("started_at", "created_at", "id"))
    tool_call_payloads = [
        {
            "id": str(call.id),
            "modelCallId": call.model_call_id,
            "model_call_id": call.model_call_id,
            "name": call.tool_name,
            "tool_name": call.tool_name,
            "executor": call.executor,
            "status": call.status,
            # Arguments and raw results never cross this operational API.
            "durationMs": call.duration_ms,
            "duration_ms": call.duration_ms,
            "resultRef": call.result_ref,
            "result_ref": call.result_ref,
            "errorCode": call.error_code,
            "error_code": call.error_code,
            "startedAt": _iso(call.started_at),
            "started_at": _iso(call.started_at),
            "completedAt": _iso(call.completed_at),
            "completed_at": _iso(call.completed_at),
        }
        for call in tool_calls
    ]
    event_payloads = [
        {
            "sequence": event.sequence,
            "type": event.event_type,
            "event_type": event.event_type,
            "payload": redact_payload(event.payload),
            "createdAt": _iso(event.created_at),
            "created_at": _iso(event.created_at),
        }
        for event in run.events.all().order_by("sequence")
    ]
    data.update(
        {
            "schemaVersion": SCHEMA_VERSION,
            "user_id": run.user_id,
            "config_snapshot_id": (
                str(run.config_snapshot_id) if run.config_snapshot_id else None
            ),
            "input_message_id": run.input_message_id,
            "output_message_id": run.output_message_id,
            "cancel_requested_at": _iso(run.cancel_requested_at),
            "error_message": run.error_message,
            # Keep the legacy key with an allow-listed, redacted projection.
            "metadata": {"trace_id": (run.metadata or {}).get("trace_id", "")},
            "toolCalls": tool_call_payloads,
            "tool_calls": tool_call_payloads,
            "events": event_payloads,
            "sandbox": _serialize_sandbox(run, tool_calls),
        }
    )
    return data


def redact_payload(value: Any, *, key: str = "") -> Any:
    """Defence-in-depth redaction for historical events written by older code."""

    lowered = key.lower()
    if any(fragment in lowered for fragment in _SENSITIVE_KEYS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact_payload(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_payload(item, key=key) for item in value]
    return value


def _serialize_sandbox(run: AgentRun, tool_calls: list[Any]) -> dict[str, Any] | None:
    try:
        sandbox = run.sandbox_execution
    except SandboxExecution.DoesNotExist:
        return None

    lifecycle = _sandbox_lifecycle_from_events(run)
    if not lifecycle:
        lifecycle = [{"status": "creating", "at": _iso(sandbox.created_at)}]
        if sandbox.ready_at:
            lifecycle.append({"status": "ready", "at": _iso(sandbox.ready_at)})
        sandbox_calls = [call for call in tool_calls if call.executor == "sandbox"]
        if sandbox_calls:
            started_at = sandbox_calls[0].started_at or sandbox_calls[0].created_at
            lifecycle.append({"status": "running", "at": _iso(started_at)})
        if sandbox.terminated_at:
            lifecycle.append({"status": "terminated", "at": _iso(sandbox.terminated_at)})
        elif sandbox.status == SandboxExecution.STATUS_FAILED:
            lifecycle.append({"status": "failed", "at": None})
    return {
        "id": str(sandbox.id),
        "provider": sandbox.provider,
        "status": sandbox.status,
        "template": sandbox.template,
        "cpuCores": sandbox.cpu_cores,
        "memoryMib": sandbox.memory_mib,
        "networkPolicy": sandbox.network_policy,
        "expiresAt": _iso(sandbox.expires_at),
        "readyAt": _iso(sandbox.ready_at),
        "terminatedAt": _iso(sandbox.terminated_at),
        "billedSeconds": sandbox.billed_seconds,
        "errorCode": sandbox.error_code or "",
        "lifecycle": lifecycle,
    }


def _sandbox_lifecycle_from_events(run: AgentRun) -> list[dict[str, Any]]:
    lifecycle = []
    for event in run.events.filter(
        event_type__in=[
            str(RunEventType.SANDBOX_STARTED),
            str(RunEventType.SANDBOX_COMPLETED),
        ]
    ).order_by("sequence"):
        if event.event_type == str(RunEventType.SANDBOX_STARTED):
            lifecycle.append({"status": "running", "at": _iso(event.created_at)})
        elif event.event_type == str(RunEventType.SANDBOX_COMPLETED):
            lifecycle.append({"status": "terminated", "at": _iso(event.created_at)})
    return lifecycle

def _timing(run: AgentRun) -> dict[str, int | None]:
    started_at = run.started_at or run.created_at
    end_at = run.completed_at
    return {
        "durationMs": _milliseconds(started_at, end_at),
        "firstTokenMs": _milliseconds(started_at, run.first_token_at),
        "startedAt": _iso(run.started_at),
        "firstTokenAt": _iso(run.first_token_at),
        "completedAt": _iso(run.completed_at),
    }


def _milliseconds(start: datetime | None, end: datetime | None) -> int | None:
    if not start or not end:
        return None
    return max(int((end - start).total_seconds() * 1000), 0)


def _metric_summary(values: list[int | None]) -> dict[str, int | None]:
    clean = sorted(int(value) for value in values if value is not None)
    if not clean:
        return {"count": 0, "avg": None, "p50": None, "p95": None}
    return {
        "count": len(clean),
        "avg": int(sum(clean) / len(clean)),
        "p50": clean[_percentile_index(clean, 0.50)],
        "p95": clean[_percentile_index(clean, 0.95)],
    }


def _percentile_index(values: list[int], percentile: float) -> int:
    return min(max(int(round((len(values) - 1) * percentile)), 0), len(values) - 1)


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(float(numerator) / float(denominator), 6)
def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if timezone.is_naive(value):
        value = timezone.make_aware(value, datetime_timezone.utc)
    return value.isoformat()
