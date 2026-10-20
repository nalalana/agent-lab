"""Durable enqueue and state transitions for knowledge indexing."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.agent_runtime.models import MemorySyncJob
from apps.runtime.models import WorkspaceFile

from .identity import knowledge_idempotency_key, normalize_knowledge_path


class KnowledgeIndexService:
    """Creates one durable job for each company/file/content version."""

    def __init__(self, dispatcher=None):
        self.dispatcher = dispatcher or self._dispatch

    @transaction.atomic
    def enqueue(self, workspace_file: WorkspaceFile, *, force: bool = False) -> MemorySyncJob:
        normalize_knowledge_path(workspace_file.logical_path)
        job, created = MemorySyncJob.objects.get_or_create(
            idempotency_key=knowledge_idempotency_key(workspace_file),
            defaults={
                "company": workspace_file.company,
                "job_type": MemorySyncJob.TYPE_KNOWLEDGE_INDEX,
                "source_ref": f"workspace_file:{workspace_file.pk}",
            },
        )
        self._assert_owner(job, workspace_file)
        should_dispatch = created or force or job.status == MemorySyncJob.STATUS_FAILED
        if should_dispatch:
            self._reset_job(job)
            self._mark_file_pending(workspace_file)
            transaction.on_commit(lambda: self.dispatcher(str(job.pk)))
            job.refresh_from_db()
        return job

    @staticmethod
    def _assert_owner(job: MemorySyncJob, workspace_file: WorkspaceFile) -> None:
        expected_ref = f"workspace_file:{workspace_file.pk}"
        if (
            job.company_id != workspace_file.company_id
            or job.job_type != MemorySyncJob.TYPE_KNOWLEDGE_INDEX
            or job.source_ref != expected_ref
        ):
            raise ValueError("Knowledge idempotency key belongs to another source.")

    @staticmethod
    def _reset_job(job: MemorySyncJob) -> None:
        MemorySyncJob.objects.filter(pk=job.pk).update(
            status=MemorySyncJob.STATUS_PENDING,
            next_retry_at=None,
            error_code="",
            completed_at=None,
        )

    @staticmethod
    def _mark_file_pending(workspace_file: WorkspaceFile) -> None:
        now = str(timezone.now().timestamp())[:20]
        WorkspaceFile.objects.filter(pk=workspace_file.pk).update(
            resource_sync_status=WorkspaceFile.RESOURCE_SYNC_PENDING,
            resource_last_error="",
            resource_task_id="",
            updated_at=now,
        )

    @staticmethod
    def _dispatch(job_id: str) -> None:
        from .tasks import index_knowledge_file

        index_knowledge_file.delay(job_id)


def workspace_file_id(source_ref: str) -> int:
    prefix = "workspace_file:"
    if not str(source_ref).startswith(prefix):
        raise ValueError("Knowledge job source_ref is invalid.")
    try:
        return int(str(source_ref).removeprefix(prefix))
    except (TypeError, ValueError) as exc:
        raise ValueError("Knowledge job WorkspaceFile ID is invalid.") from exc
