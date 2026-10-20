"""Query and serialization helpers for company-admin runtime APIs."""

from __future__ import annotations

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Count, Q, QuerySet

from apps.agent_runtime.models import AgentRun, AgentSchedule, MemorySyncJob
from apps.runtime.models import WorkspaceFile


def run_queryset(company, filters: dict) -> QuerySet:
    queryset = AgentRun.objects.filter(company=company).select_related("agent", "session")
    if filters.get("status"):
        queryset = queryset.filter(status=filters["status"])
    if filters.get("agent_id"):
        queryset = queryset.filter(agent_id=filters["agent_id"])
    return queryset.order_by("-created_at")


def memory_job_queryset(company, filters: dict) -> QuerySet:
    queryset = MemorySyncJob.objects.filter(company=company).select_related("agent", "user")
    if filters.get("status"):
        queryset = queryset.filter(status=filters["status"])
    if filters.get("job_type"):
        queryset = queryset.filter(job_type=filters["job_type"])
    if filters.get("agent_id"):
        queryset = queryset.filter(agent_id=filters["agent_id"])
    return queryset.order_by("-created_at")




def schedule_queryset(company, filters: dict) -> QuerySet:
    queryset = (
        AgentSchedule.objects.filter(company=company)
        .select_related("agent", "operator", "last_run", "last_run__output_message")
        .annotate(
            _run_count=Count(
                "last_run__session__agent_runs",
                filter=Q(
                    last_run__session__agent_runs__source=AgentRun.SOURCE_CRON,
                    last_run__session__agent_runs__status=AgentRun.STATUS_COMPLETED,
                ),
                distinct=True,
            )
        )
    )
    if filters.get("status"):
        queryset = queryset.filter(status=filters["status"])
    if filters.get("agent_id"):
        queryset = queryset.filter(agent_id=filters["agent_id"])
    return queryset.order_by("-created_at")

def knowledge_file_queryset(company, filters: dict) -> QuerySet:
    queryset = WorkspaceFile.objects.filter(
        company=company,
        logical_path__startswith="knowledge/",
    )
    if filters.get("status"):
        queryset = queryset.filter(resource_sync_status=filters["status"])
    return queryset.order_by("-id")


def paginate(queryset, filters: dict, serializer) -> dict:
    total = queryset.count()
    offset = filters["offset"]
    limit = filters["limit"]
    items = [serializer(item) for item in queryset[offset : offset + limit]]
    return {"total": total, "limit": limit, "offset": offset, "items": items}


def serialize_run(run: AgentRun) -> dict:
    return {
        "id": str(run.pk),
        "request_id": run.request_id,
        "agent_id": run.agent_id,
        "agent_business_id": run.agent.agentId,
        "session_id": run.session_id,
        "source": run.source,
        "engine": run.engine,
        "status": run.status,
        "model_provider": run.model_provider,
        "model_name": run.model_name,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "sandbox_seconds": run.sandbox_seconds,
        "error_code": run.error_code,
        "started_at": run.started_at,
        "first_token_at": run.first_token_at,
        "completed_at": run.completed_at,
        "created_at": run.created_at,
    }


def serialize_run_detail(run: AgentRun) -> dict:
    data = serialize_run(run)
    data.update(
        {
            "user_id": run.user_id,
            "config_snapshot_id": str(run.config_snapshot_id) if run.config_snapshot_id else None,
            "input_message_id": run.input_message_id,
            "output_message_id": run.output_message_id,
            "cancel_requested_at": run.cancel_requested_at,
            "error_message": run.error_message,
            "metadata": run.metadata,
            "events": [
                {
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "payload": event.payload,
                    "created_at": event.created_at,
                }
                for event in run.events.all()
            ],
            "tool_calls": [_serialize_tool_call(item) for item in run.tool_calls.all()],
            "sandbox": _serialize_sandbox(run),
        }
    )
    return data


def _serialize_tool_call(item) -> dict:
    return {
        "id": str(item.pk),
        "model_call_id": item.model_call_id,
        "tool_name": item.tool_name,
        "executor": item.executor,
        "status": item.status,
        "arguments_redacted": item.arguments_redacted,
        "result_ref": item.result_ref,
        "duration_ms": item.duration_ms,
        "error_code": item.error_code,
        "started_at": item.started_at,
        "completed_at": item.completed_at,
    }


def _serialize_sandbox(run: AgentRun):
    try:
        sandbox = run.sandbox_execution
    except ObjectDoesNotExist:
        return None
    return {
        "id": str(sandbox.pk),
        "provider": sandbox.provider,
        "status": sandbox.status,
        "template": sandbox.template,
        "network_policy": sandbox.network_policy,
        "expires_at": sandbox.expires_at,
        "ready_at": sandbox.ready_at,
        "terminated_at": sandbox.terminated_at,
        "billed_seconds": sandbox.billed_seconds,
        "error_code": sandbox.error_code,
    }


def serialize_memory_job(job: MemorySyncJob) -> dict:
    return {
        "id": str(job.pk),
        "agent_id": job.agent_id,
        "agent_business_id": job.agent.agentId if job.agent else "",
        "agent_name": job.agent.name if job.agent else "",
        "user_id": job.user_id,
        "job_type": job.job_type,
        "source_ref": job.source_ref,
        "status": job.status,
        "attempts": job.attempts,
        "next_retry_at": job.next_retry_at,
        "error_code": job.error_code,
        "created_at": job.created_at,
        "completed_at": job.completed_at,
        "updated_at": job.completed_at or job.created_at,
    }


def serialize_knowledge_file(workspace_file: WorkspaceFile) -> dict:
    return {
        "id": workspace_file.pk,
        "logical_path": workspace_file.logical_path,
        "file_name": workspace_file.file_name,
        "mime_type": workspace_file.mime_type,
        "size": workspace_file.size,
        "checksum": workspace_file.checksum,
        "index_status": workspace_file.resource_sync_status,
        "resource_uri": workspace_file.resource_uri,
        "index_job_id": workspace_file.resource_task_id,
        "index_error": workspace_file.resource_last_error,
        "indexed_at": workspace_file.resource_synced_at,
        "created_at": workspace_file.created_at,
        "updated_at": workspace_file.updated_at,
    }
def serialize_schedule(schedule: AgentSchedule) -> dict:
    metadata = schedule.metadata if isinstance(schedule.metadata, dict) else {}
    last_run_id = str(schedule.last_run_id) if schedule.last_run_id else None
    acknowledged_run_id = str(metadata.get("acknowledged_run_id") or "") or None
    run_count = getattr(schedule, "_run_count", None)
    if run_count is None:
        run_count = (
            AgentRun.objects.filter(
                session_id=schedule.last_run.session_id,
                source=AgentRun.SOURCE_CRON,
                status=AgentRun.STATUS_COMPLETED,
            ).count()
            if schedule.last_run_id
            else 0
        )
    return {
        "id": str(schedule.pk),
        "name": schedule.name,
        "agent_id": schedule.agent_id,
        "agent_business_id": schedule.agent.agentId,
        "agent_name": schedule.agent.name,
        "run_count": run_count,
        "last_reply_summary": schedule.last_run.output_message.content if schedule.last_run_id and schedule.last_run.output_message else "",
        "schedule_type": schedule.schedule_type,
        "schedule_value": schedule.schedule_value,
        "message": schedule.message,
        "session": schedule.session_key or f"schedule:{schedule.pk}",
        "enabled": schedule.enabled,
        "status": schedule.status,
        "next_run_at": schedule.next_run_at,
        "last_run_at": schedule.last_run_at,
        "last_run_id": last_run_id,
        "acknowledged_run_id": acknowledged_run_id,
        "pending_acknowledgement": bool(last_run_id and acknowledged_run_id != last_run_id),
        "last_error": schedule.last_error,
        "consecutive_failures": schedule.consecutive_failures,
        "retry_after": schedule.retry_after,
        "created_at": schedule.created_at,
        "updated_at": schedule.updated_at,
    }
