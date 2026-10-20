"""Celery workers and failed-queue recovery for OpenViking commits."""

from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone

from apps.agent_runtime.models import AgentRun, MemorySyncJob
from apps.company.models import TransparentCompanyAgentTestMessage

from .errors import MemoryGatewayError, OpenVikingTaskPending, OpenVikingUnavailable
from .gateway import OpenVikingMemoryGateway


RETRYABLE_ERROR_CODES = {
    'MEMORY_UNAVAILABLE',
    'MEMORY_QUEUE_UNAVAILABLE',
    'MEMORY_COMMIT_TIMEOUT',
}
NATIVE_POLL_SECONDS = max(int(getattr(settings, 'OPENVIKING_MEMORY_POLL_SECONDS', 5)), 1)
NATIVE_MAX_POLLS = max(int(getattr(settings, 'OPENVIKING_MEMORY_MAX_POLLS', 120)), 1)


@shared_task(bind=True, max_retries=NATIVE_MAX_POLLS)
def commit_memory_job(self, job_id: str, payload: dict):
    """Commit once, preserving safe retry state in PostgreSQL."""
    job = MemorySyncJob.objects.get(pk=job_id)
    if job.status == MemorySyncJob.STATUS_COMPLETED:
        return {'job_id': job_id, 'status': job.status}
    updates = {
        'status': MemorySyncJob.STATUS_RUNNING,
        'error_code': '',
        'next_retry_at': None,
        'completed_at': None,
    }
    if not job.external_task_id:
        updates['attempts'] = F('attempts') + 1
    MemorySyncJob.objects.filter(pk=job.id).update(**updates)
    try:
        uris = OpenVikingMemoryGateway().process_commit_job(job, payload)
    except OpenVikingTaskPending as exc:
        return _retry_pending_commit(self, job, exc)
    except OpenVikingUnavailable as exc:
        return _retry_or_fail(self, job, exc)
    except MemoryGatewayError as exc:
        _mark_failed(job, exc.code)
        raise
    return {'job_id': job_id, 'status': MemorySyncJob.STATUS_COMPLETED, 'uris': list(uris)}


@shared_task
def retry_failed_memory_jobs(limit: int = 100) -> dict:
    """Requeue retryable failures using source messages as durable payload."""
    now = timezone.now()
    jobs = MemorySyncJob.objects.filter(
        status=MemorySyncJob.STATUS_FAILED,
        error_code__in=RETRYABLE_ERROR_CODES,
    ).filter(Q(next_retry_at__isnull=True) | Q(next_retry_at__lte=now)).order_by('created_at')[:limit]
    queued = 0
    for job in jobs:
        payload = _rebuild_payload(job)
        if payload is None:
            continue
        MemorySyncJob.objects.filter(pk=job.id).update(
            status=MemorySyncJob.STATUS_PENDING,
            error_code='',
            next_retry_at=None,
            completed_at=None,
        )
        commit_memory_job.delay(str(job.id), payload)
        queued += 1
    return {'queued': queued}


def requeue_memory_commit_job(job: MemorySyncJob) -> None:
    """Rebuild and dispatch one administrator-selected durable commit job."""
    if job.job_type != MemorySyncJob.TYPE_MEMORY_COMMIT:
        # TODO(P3-A): route delete jobs through the audited deletion workflow
        # after its Phase 3-A entry point is available.
        raise ValueError('Only memory_commit jobs can be retried here.')
    payload = _rebuild_payload(job)
    if payload is None:
        raise ValueError('Memory commit source messages are no longer available.')
    MemorySyncJob.objects.filter(pk=job.pk).update(
        status=MemorySyncJob.STATUS_PENDING,
        error_code='',
        next_retry_at=None,
        completed_at=None,
    )
    commit_memory_job.delay(str(job.pk), payload)


def _retry_or_fail(task, job: MemorySyncJob, exc: OpenVikingUnavailable):
    if task.request.retries >= task.max_retries:
        _mark_failed(job, exc.code, retry_at=timezone.now() + timedelta(minutes=15))
        raise exc
    countdown = min(2 ** (task.request.retries + 1) * 5, 300)
    MemorySyncJob.objects.filter(pk=job.id).update(
        status=MemorySyncJob.STATUS_PENDING,
        error_code=exc.code,
        next_retry_at=timezone.now() + timedelta(seconds=countdown),
    )
    raise task.retry(exc=exc, countdown=countdown)


def _retry_pending_commit(task, job: MemorySyncJob, exc: OpenVikingTaskPending):
    if task.request.retries >= NATIVE_MAX_POLLS:
        _mark_failed(
            job,
            'MEMORY_COMMIT_TIMEOUT',
            retry_at=timezone.now() + timedelta(minutes=15),
        )
        raise OpenVikingUnavailable('OpenViking memory extraction timed out.') from exc
    retry_at = timezone.now() + timedelta(seconds=NATIVE_POLL_SECONDS)
    MemorySyncJob.objects.filter(pk=job.id).update(
        status=MemorySyncJob.STATUS_PENDING,
        error_code='',
        next_retry_at=retry_at,
        completed_at=None,
    )
    raise task.retry(exc=exc, countdown=NATIVE_POLL_SECONDS)


def _mark_failed(job: MemorySyncJob, error_code: str, retry_at=None) -> None:
    MemorySyncJob.objects.filter(pk=job.id).update(
        status=MemorySyncJob.STATUS_FAILED,
        error_code=str(error_code)[:64],
        next_retry_at=retry_at,
        completed_at=timezone.now(),
    )


def _rebuild_payload(job: MemorySyncJob):
    message_ids = _message_ids(job.source_ref)
    messages = list(
        TransparentCompanyAgentTestMessage.objects.filter(pk__in=message_ids)
        .select_related('session')
        .order_by('id')
    )
    if len(messages) != len(message_ids) or not messages:
        return None
    session_ids = {message.session_id for message in messages}
    if len(session_ids) != 1:
        return None
    run = AgentRun.objects.select_related('output_message').filter(
        company_id=job.company_id,
        agent_id=job.agent_id,
    ).filter(Q(input_message_id__in=message_ids) | Q(output_message_id__in=message_ids)).first()
    native_messages = [
        {'role': message.role, 'content': str(message.content or '')}
        for message in messages
        if message.role in {'user', 'assistant'} and str(message.content or '').strip()
    ]
    if (
        run
        and run.output_message
        and run.output_message_id not in {message.id for message in messages}
        and str(run.output_message.content or '').strip()
    ):
        native_messages.append({
            'role': 'assistant',
            'content': str(run.output_message.content),
        })
    content = '\n'.join(
        f'{message["role"]}: {message["content"]}'
        for message in native_messages
    )
    return {
        'schemaVersion': OpenVikingMemoryGateway.PAYLOAD_SCHEMA_VERSION,
        'run_id': str(run.id) if run else '',
        'company_id': str(job.company_id),
        'agent_id': str(job.agent_id),
        'user_id': str(job.user_id) if job.user_id is not None else None,
        'session_id': str(messages[0].session_id),
        'source_message_ids': [str(value) for value in message_ids],
        'content': content,
        'idempotency_key': job.idempotency_key,
        'messages': native_messages,
    }


def _message_ids(source_ref: str) -> tuple[str, ...]:
    if not source_ref.startswith('messages:'):
        return ()
    return tuple(value for value in source_ref.removeprefix('messages:').split(',') if value)
