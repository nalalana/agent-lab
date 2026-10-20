"""Scheduled Agent execution through the unified runtime."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.agent_runtime.models import AgentRun, AgentSchedule, AgentToolCall
from apps.agent_runtime.runtime import UnifiedRuntimeService
from apps.company.models import (
    TransparentCompanyAgentTestMessage,
    TransparentCompanyAgentTestSession,
)

SIDE_EFFECT_TOOL_NAMES = frozenset({
    # The Phase 2 built-in tools are idempotent or sandbox-local. External
    # mutation tools must be added here when their definitions land.
})


@dataclass(frozen=True)
class ScheduleRunResult:
    schedule_id: str
    status: str
    run_id: str | None = None
    skipped_reason: str = ""


def initialize_next_run(schedule: AgentSchedule, *, now=None) -> AgentSchedule:
    now = now or timezone.now()
    next_run_at = compute_next_run_at(schedule.schedule_type, schedule.schedule_value, now=now)
    AgentSchedule.objects.filter(pk=schedule.pk).update(next_run_at=next_run_at)
    schedule.next_run_at = next_run_at
    return schedule


def scan_due_agent_schedules(*, limit: int = 50, now=None) -> dict:
    now = now or timezone.now()
    due_ids = list(
        AgentSchedule.objects.filter(
            enabled=True,
            status=AgentSchedule.STATUS_ACTIVE,
            next_run_at__isnull=False,
            next_run_at__lte=now,
        ).order_by('next_run_at').values_list('pk', flat=True)[:limit]
    )
    results = []
    for schedule_id in due_ids:
        schedule = AgentSchedule(pk=schedule_id)
        results.append(run_due_schedule(schedule, now=now))
    return {
        'scanned': len(due_ids),
        'runs': [result.__dict__ for result in results],
    }


@transaction.atomic
def claim_due_schedule(schedule_id, *, now=None) -> AgentSchedule | None:
    now = now or timezone.now()
    schedule = (
        AgentSchedule.objects.select_for_update(skip_locked=True)
        .filter(
            pk=schedule_id,
            enabled=True,
            status=AgentSchedule.STATUS_ACTIVE,
            next_run_at__isnull=False,
            next_run_at__lte=now,
        )
        .first()
    )
    if schedule is None:
        return None
    AgentSchedule.objects.filter(pk=schedule.pk).update(next_run_at=None, retry_after=None)
    return AgentSchedule.objects.select_related(
        'company',
        'agent',
        'agent__transparentCompany',
        'operator',
    ).get(pk=schedule.pk)


def run_due_schedule(schedule: AgentSchedule, *, now=None, runtime_service=None) -> ScheduleRunResult:
    now = now or timezone.now()
    claimed = claim_due_schedule(schedule.pk, now=now)
    if claimed is None:
        return ScheduleRunResult(str(schedule.pk), 'skipped', skipped_reason='not_due')
    schedule = claimed
    try:
        if schedule.last_run_id and _has_completed_side_effect_tool(schedule.last_run_id):
            _mark_schedule_failed(
                schedule,
                '上一次运行已完成副作用工具，禁止自动重试以避免重复副作用',
                now=now,
            )
            return ScheduleRunResult(str(schedule.pk), 'skipped', skipped_reason='side_effect_retry_blocked')
        session, input_message = _create_schedule_input(schedule, now=now)
        cached_run = reusable_schedule_run(schedule)
        if cached_run is not None:
            run_id = _copy_cached_schedule_reply(
                schedule,
                session=session,
                input_message=input_message,
                source_run=cached_run,
                now=now,
            )
            _mark_schedule_completed(schedule, run_id=run_id, now=now)
            return ScheduleRunResult(str(schedule.pk), 'completed', run_id=run_id)
        # The saved message stays concise for the conversation. Only the
        # in-memory runtime input gets due-task context, preventing the agent
        # from trying to schedule again or refusing the reminder request.
        input_message.content = (
            '这是系统已经到期并触发的定时任务。请立即执行任务或直接给出到期提醒；'
            '不要尝试再次创建定时任务，也不要回复当前环境不支持自动定时或周期性提醒。\n\n'
            f'任务：{schedule.message}'
        )
        events = (runtime_service or UnifiedRuntimeService()).stream(
            company=schedule.company,
            agent=schedule.agent,
            session=session,
            input_message=input_message,
            source='cron',
        )
        run_id = _drain_runtime_events(events, session=session)
        _mark_schedule_completed(schedule, run_id=run_id, now=now)
        return ScheduleRunResult(str(schedule.pk), 'completed', run_id=run_id)
    except Exception as exc:
        _mark_schedule_failed(schedule, str(exc), now=now)
        return ScheduleRunResult(str(schedule.pk), 'failed', skipped_reason=exc.__class__.__name__)


def reusable_schedule_run(schedule: AgentSchedule) -> AgentRun | None:
    """Return a safe previous reply for an unchanged reminder-only task."""
    metadata = schedule.metadata if isinstance(schedule.metadata, dict) else {}
    preference = metadata.get('reuse_last_reply')
    message = str(schedule.message or '').strip()
    if preference is False:
        return None
    if preference is not True and not any(keyword in message for keyword in ('提醒', '通知', '闹钟')):
        return None
    if not schedule.last_run_id:
        return None
    source_run = (
        AgentRun.objects.select_related('input_message', 'output_message', 'config_snapshot')
        .filter(
            pk=schedule.last_run_id,
            status=AgentRun.STATUS_COMPLETED,
            source=AgentRun.SOURCE_CRON,
        )
        .first()
    )
    if source_run is None or source_run.output_message_id is None:
        return None
    if str(source_run.input_message.content or '').strip() != message:
        return None
    if not str(source_run.output_message.content or '').strip():
        return None
    if AgentToolCall.objects.filter(run_id=source_run.pk).exists():
        return None
    return source_run


@transaction.atomic
def _copy_cached_schedule_reply(
    schedule: AgentSchedule,
    *,
    session: TransparentCompanyAgentTestSession,
    input_message: TransparentCompanyAgentTestMessage,
    source_run: AgentRun,
    now,
) -> str:
    from apps.company.services import CompanyService

    assistant_message = CompanyService._create_agent_test_message(
        session,
        role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT,
        content=source_run.output_message.content,
        token_count=source_run.output_tokens,
        tool_calls=[],
        raw_payload={
            'schedule_cache_hit': True,
            'cached_from_run_id': str(source_run.pk),
        },
    )
    CompanyService._update_agent_test_session_summary(
        session,
        last_message=assistant_message.content,
        status=TransparentCompanyAgentTestSession.STATUS_ACTIVE,
    )
    cached_run = AgentRun.objects.create(
        request_id=f'message:{input_message.pk}',
        company=schedule.company,
        agent=schedule.agent,
        user_id=session.operator_id,
        session=session,
        source=AgentRun.SOURCE_CRON,
        engine=source_run.engine,
        status=AgentRun.STATUS_COMPLETED,
        config_snapshot=source_run.config_snapshot,
        input_message=input_message,
        output_message=assistant_message,
        model_provider=source_run.model_provider,
        model_name=source_run.model_name,
        output_tokens=source_run.output_tokens,
        started_at=now,
        first_token_at=now,
        completed_at=now,
        metadata={
            'schemaVersion': 1,
            'schedule_cache_hit': True,
            'cached_from_run_id': str(source_run.pk),
        },
    )
    return str(cached_run.pk)


def compute_next_run_at(schedule_type: str, schedule_value: str, *, now=None) -> datetime | None:
    now = now or timezone.now()
    schedule_type = str(schedule_type or '').strip().lower()
    value = str(schedule_value or '').strip()
    if schedule_type == AgentSchedule.SCHEDULE_AT:
        target = _parse_datetime(value)
        return target if target > now else None
    if schedule_type == AgentSchedule.SCHEDULE_EVERY:
        seconds = _parse_every_seconds(value)
        return now + timedelta(seconds=seconds)
    if schedule_type == AgentSchedule.SCHEDULE_CRON:
        return _next_cron_time(value, now=now)
    raise ValueError('Unsupported schedule_type')


def ensure_schedule_session(schedule: AgentSchedule, *, now=None) -> TransparentCompanyAgentTestSession:
    """Create the stable task conversation before background execution starts."""
    now = now or timezone.now()
    transparent_company = schedule.agent.transparentCompany
    session_key = schedule.session_key or f'schedule:{schedule.pk}'
    timestamp = _legacy_timestamp(now)
    if schedule.session_key != session_key:
        AgentSchedule.objects.filter(pk=schedule.pk).update(session_key=session_key)
        schedule.session_key = session_key
    session = (
        TransparentCompanyAgentTestSession.objects.filter(
            transparentCompany=transparent_company,
            agent=schedule.agent,
            sessionKey=session_key,
        )
        .order_by('-id')
        .first()
    )
    if session is None:
        session = TransparentCompanyAgentTestSession.objects.create(
            transparentCompany=transparent_company,
            agent=schedule.agent,
            operator=schedule.operator,
            conversationId=f'cron:{schedule.pk}',
            title=schedule.name,
            sessionKey=session_key,
            source=TransparentCompanyAgentTestSession.SOURCE_AGENT_TEST,
            created_at=timestamp,
            updated_at=timestamp,
        )
    else:
        session.title = schedule.name
        session.operator = schedule.operator
        session.status = TransparentCompanyAgentTestSession.STATUS_ACTIVE
        session.updated_at = timestamp
        session.save(update_fields=['title', 'operator', 'status', 'updated_at'])
    return session


def _create_schedule_input(schedule: AgentSchedule, *, now) -> tuple[TransparentCompanyAgentTestSession, TransparentCompanyAgentTestMessage]:
    session = ensure_schedule_session(schedule, now=now)
    timestamp = _legacy_timestamp(now)
    message = TransparentCompanyAgentTestMessage.objects.create(
        session=session,
        role=TransparentCompanyAgentTestMessage.ROLE_USER,
        content=schedule.message,
        created_at=timestamp,
        updated_at=timestamp,
    )
    return session, message


def _drain_runtime_events(events, *, session) -> str | None:
    run_id = None
    error = None
    content = ''
    token_count = 0
    tool_calls = []
    complete_event = None
    for event in events:
        if not isinstance(event, dict):
            continue
        run_id = str(event.get('run_id') or run_id or '') or None
        event_type = str(event.get('event') or '').strip().lower()
        if event_type == 'delta':
            content += str(event.get('delta') or '')
        elif event_type == 'complete':
            complete_event = event
            content = str(event.get('content') or content)
            token_count = int(event.get('token_count') or 0)
            tool_calls = list(event.get('tool_calls') or [])
        elif event_type == 'error':
            error = event
    if error:
        raise RuntimeError(str(error.get('message') or error.get('code') or 'AgentRun failed'))
    if content:
        from apps.company.services import CompanyService
        from apps.runtime.security import sanitize_tool_calls

        assistant_message = CompanyService._create_agent_test_message(
            session,
            role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT,
            content=content,
            token_count=token_count,
            tool_calls=sanitize_tool_calls(tool_calls),
            raw_payload={},
        )
        if complete_event:
            CompanyService._attach_agent_runtime_output(complete_event, assistant_message)
        CompanyService._update_agent_test_session_summary(
            session,
            last_message=content,
            status=TransparentCompanyAgentTestSession.STATUS_ACTIVE,
        )
    return run_id


def _mark_schedule_completed(schedule: AgentSchedule, *, run_id: str | None, now) -> None:
    one_time = schedule.schedule_type == AgentSchedule.SCHEDULE_AT
    next_run_at = None if one_time else compute_next_run_at(
        schedule.schedule_type,
        schedule.schedule_value,
        now=now,
    )
    AgentSchedule.objects.filter(pk=schedule.pk).update(
        last_run_at=now,
        last_run_id=run_id,
        last_error='',
        consecutive_failures=0,
        retry_after=None,
        next_run_at=next_run_at,
        enabled=False if one_time else schedule.enabled,
        status=AgentSchedule.STATUS_PAUSED if one_time else schedule.status,
    )


def _mark_schedule_failed(schedule: AgentSchedule, error: str, *, now) -> None:
    retry_at = now + timedelta(minutes=min(60, 2 ** min(schedule.consecutive_failures, 5)))
    next_run_at = retry_at
    AgentSchedule.objects.filter(pk=schedule.pk).update(
        last_run_at=now,
        last_error=str(error)[:2000],
        consecutive_failures=schedule.consecutive_failures + 1,
        retry_after=retry_at,
        next_run_at=next_run_at,
    )


def _has_completed_side_effect_tool(run_id) -> bool:
    if not SIDE_EFFECT_TOOL_NAMES:
        return False
    return AgentToolCall.objects.filter(
        run_id=run_id,
        status=AgentToolCall.STATUS_COMPLETED,
        tool_name__in=SIDE_EFFECT_TOOL_NAMES,
    ).exists()


def _parse_datetime(value: str) -> datetime:
    normalized = value.replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError('schedule_value must be an ISO datetime for at schedules') from exc
    if settings.USE_TZ and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    elif not settings.USE_TZ and timezone.is_aware(parsed):
        parsed = timezone.make_naive(parsed, timezone.get_current_timezone())
    return parsed


def _parse_every_seconds(value: str) -> int:
    raw = value.strip().lower()
    if raw.isdigit():
        seconds = int(raw)
    else:
        units = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
        unit = raw[-1:]
        if unit not in units or not raw[:-1].isdigit():
            raise ValueError('schedule_value must be seconds or <n>s/<n>m/<n>h/<n>d for every schedules')
        seconds = int(raw[:-1]) * units[unit]
    if seconds < 60:
        raise ValueError('every schedule interval must be at least 60 seconds')
    return seconds


def _next_cron_time(value: str, *, now) -> datetime:
    parts = value.split()
    if len(parts) != 5:
        raise ValueError('cron schedule_value must have five fields')
    minute, hour, day, month, weekday = [_parse_cron_field(part, index) for index, part in enumerate(parts)]
    candidate = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(366 * 24 * 60):
        if (
            candidate.minute in minute
            and candidate.hour in hour
            and candidate.day in day
            and candidate.month in month
            # Cron uses Sunday=0; Python weekday() uses Monday=0.
            and candidate.isoweekday() % 7 in weekday
        ):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError('cron schedule has no occurrence in the next year')


def _parse_cron_field(raw: str, index: int) -> set[int]:
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    lower, upper = ranges[index]
    values = set()
    for token in raw.split(','):
        token = token.strip()
        if token == '*':
            values.update(range(lower, upper + 1))
            continue
        if token.startswith('*/') and token[2:].isdigit():
            step = int(token[2:])
            if step <= 0:
                raise ValueError('cron step must be positive')
            values.update(range(lower, upper + 1, step))
            continue
        if '-' in token:
            start, end = token.split('-', 1)
            if not start.isdigit() or not end.isdigit():
                raise ValueError('cron range is invalid')
            start_value, end_value = int(start), int(end)
            if start_value > end_value:
                raise ValueError('cron range is invalid')
            values.update(range(start_value, end_value + 1))
            continue
        if token.isdigit():
            values.add(int(token))
            continue
        raise ValueError('cron field is invalid')
    if not values or min(values) < lower or max(values) > upper:
        raise ValueError('cron field is out of range')
    return values


def _legacy_timestamp(value) -> str:
    return str(value.timestamp())[:20]


def _timestamp_key(value) -> str:
    return hashlib.sha256(f'{value.isoformat()}:{uuid.uuid4()}'.encode('utf-8')).hexdigest()[:24]
