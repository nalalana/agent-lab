"""Celery tasks for COS-to-OpenViking knowledge indexing."""

from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone

from apps.agent_runtime.memory.errors import MemoryGatewayError, OpenVikingUnavailable
from apps.agent_runtime.models import MemorySyncJob
from apps.runtime.models import WorkspaceFile

from .indexer import KnowledgeIndexError, KnowledgeIndexer
from .services import workspace_file_id


MAX_KNOWLEDGE_INDEX_ATTEMPTS = max(
    int(getattr(settings, "KNOWLEDGE_INDEX_MAX_TOTAL_ATTEMPTS", 8)),
    1,
)


@shared_task(bind=True, max_retries=3)
def index_knowledge_file(self, job_id: str):
    """Index once; retry only failures that can succeed without data changes."""
    job = MemorySyncJob.objects.select_related("company").get(pk=job_id)
    if job.status == MemorySyncJob.STATUS_COMPLETED:
        return {"job_id": job_id, "status": job.status}
    workspace_file = _load_source(job)
    _mark_running(job, workspace_file)
    try:
        result = KnowledgeIndexer().index(workspace_file)
    except OpenVikingUnavailable as exc:
        return _retry_or_fail(self, job, workspace_file, exc)
    except (KnowledgeIndexError, MemoryGatewayError, ValueError) as exc:
        _mark_failed(job, workspace_file, getattr(exc, "code", "KNOWLEDGE_INDEX_FAILED"), str(exc))
        raise
    _mark_completed(job, workspace_file, result)
    return {"job_id": job_id, "status": MemorySyncJob.STATUS_COMPLETED, **result}


@shared_task
def retry_failed_knowledge_jobs(limit: int = 100) -> dict:
    """Recover terminal/retry-scheduled indexing jobs without duplicating them."""
    now = timezone.now()
    jobs = MemorySyncJob.objects.filter(
        job_type=MemorySyncJob.TYPE_KNOWLEDGE_INDEX,
        status=MemorySyncJob.STATUS_FAILED,
        attempts__lt=MAX_KNOWLEDGE_INDEX_ATTEMPTS,
    ).filter(Q(next_retry_at__isnull=True) | Q(next_retry_at__lte=now)).order_by("created_at")[:limit]
    for job in jobs:
        MemorySyncJob.objects.filter(pk=job.pk).update(
            status=MemorySyncJob.STATUS_PENDING,
            error_code="",
            next_retry_at=None,
            completed_at=None,
        )
        index_knowledge_file.delay(str(job.pk))
    return {"queued": len(jobs)}


@shared_task
def enqueue_pending_knowledge_files(limit: int = 100) -> dict:
    """Bridge completed COS uploads to durable index jobs."""
    from .services import KnowledgeIndexService

    files = WorkspaceFile.objects.filter(
        storage_backend=WorkspaceFile.STORAGE_TENCENT_COS,
        sync_status=WorkspaceFile.SYNC_IN_SYNC,
        resource_sync_status=WorkspaceFile.RESOURCE_SYNC_PENDING,
        logical_path__startswith="knowledge/",
    ).select_related("company").order_by("id")[:limit]
    service = KnowledgeIndexService()
    for workspace_file in files:
        service.enqueue(workspace_file)

    return {"queued": len(files)}


def _load_source(job: MemorySyncJob) -> WorkspaceFile:
    if job.job_type != MemorySyncJob.TYPE_KNOWLEDGE_INDEX:
        raise ValueError("MemorySyncJob is not a knowledge index job.")
    try:
        return WorkspaceFile.objects.select_related("company").get(
            pk=workspace_file_id(job.source_ref),
            company_id=job.company_id,
        )
    except WorkspaceFile.DoesNotExist as exc:
        raise KnowledgeIndexError("Knowledge WorkspaceFile no longer exists.") from exc


def _mark_running(job: MemorySyncJob, workspace_file: WorkspaceFile) -> None:
    MemorySyncJob.objects.filter(pk=job.pk).update(
        status=MemorySyncJob.STATUS_RUNNING,
        attempts=F("attempts") + 1,
        error_code="",
        next_retry_at=None,
        completed_at=None,
    )
    WorkspaceFile.objects.filter(pk=workspace_file.pk).update(
        resource_sync_status=WorkspaceFile.RESOURCE_SYNC_INDEXING,
        resource_task_id=str(job.pk),
        resource_last_error="",
    )


def _mark_completed(job: MemorySyncJob, workspace_file: WorkspaceFile, result: dict) -> None:
    now = timezone.now()
    MemorySyncJob.objects.filter(pk=job.pk).update(
        status=MemorySyncJob.STATUS_COMPLETED,
        error_code="",
        next_retry_at=None,
        completed_at=now,
    )
    WorkspaceFile.objects.filter(pk=workspace_file.pk).update(
        resource_uri=result["resource_uri"],
        resource_sync_status=WorkspaceFile.RESOURCE_SYNC_COMPLETED,
        resource_task_id=result.get("openviking_task_id") or str(job.pk),
        resource_last_error="",
        resource_synced_at=str(now.timestamp())[:20],
        updated_at=str(now.timestamp())[:20],
    )


def _retry_or_fail(task, job, workspace_file, exc):
    if task.request.retries >= task.max_retries:
        _mark_failed(job, workspace_file, exc.code, str(exc), retry_at=timezone.now() + timedelta(minutes=15))
        raise exc
    countdown = min(2 ** (task.request.retries + 1) * 5, 300)
    MemorySyncJob.objects.filter(pk=job.pk).update(
        status=MemorySyncJob.STATUS_PENDING,
        error_code=exc.code,
        next_retry_at=timezone.now() + timedelta(seconds=countdown),
    )
    WorkspaceFile.objects.filter(pk=workspace_file.pk).update(
        resource_sync_status=WorkspaceFile.RESOURCE_SYNC_PENDING,
        resource_last_error="OpenViking temporarily unavailable; retry scheduled.",
    )
    raise task.retry(exc=exc, countdown=countdown)


def _mark_failed(job, workspace_file, error_code, message, retry_at=None) -> None:
    now = timezone.now()
    MemorySyncJob.objects.filter(pk=job.pk).update(
        status=MemorySyncJob.STATUS_FAILED,
        error_code=str(error_code)[:64],
        next_retry_at=retry_at,
        completed_at=now,
    )
    WorkspaceFile.objects.filter(pk=workspace_file.pk).update(
        resource_sync_status=WorkspaceFile.RESOURCE_SYNC_FAILED,
        resource_last_error=str(message)[:2000],
        updated_at=str(now.timestamp())[:20],
    )
