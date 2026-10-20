"""Celery autodiscovery entry point for unified Agent runtime tasks."""

from celery import shared_task

from apps.agent_runtime.alerts import dispatch_runtime_alerts  # noqa: F401

# Importing registers nested memory tasks with the shared Celery application.
from apps.agent_runtime.memory.tasks import (  # noqa: F401
    commit_memory_job,
    retry_failed_memory_jobs,
)
from apps.agent_runtime.memory.provisioning import (  # noqa: F401
    provision_company_viking_binding,
    retry_failed_viking_bindings,
)
from apps.agent_runtime.providers.sandbox.tasks import reap_expired_sandboxes  # noqa: F401
from apps.agent_runtime.knowledge.tasks import (  # noqa: F401
    enqueue_pending_knowledge_files,
    retry_failed_knowledge_jobs,
)


@shared_task(name="apps.agent_runtime.tasks.run_due_agent_schedules")
def run_due_agent_schedules(limit: int = 50) -> dict:
    """Execute due platform schedules as normal AgentRuns."""
    from apps.agent_runtime.schedules import scan_due_agent_schedules

    return scan_due_agent_schedules(limit=limit)


@shared_task(name="apps.agent_runtime.tasks.run_agent_schedule")
def run_agent_schedule(schedule_id: str) -> dict:
    """Run one manually-triggered schedule outside the HTTP request."""
    from apps.agent_runtime.models import AgentSchedule
    from apps.agent_runtime.schedules import run_due_schedule

    result = run_due_schedule(AgentSchedule(pk=schedule_id))
    return result.__dict__
