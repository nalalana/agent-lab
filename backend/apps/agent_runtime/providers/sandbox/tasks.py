"""Celery entry points for AGSX cleanup."""

from __future__ import annotations

import asyncio

from celery import shared_task
from django.conf import settings

from .reaper import SandboxReaper
from .repository import DjangoSandboxExecutionRepository
from .tencent_agsx import TencentAgsxSandboxProvider


@shared_task
def reap_expired_sandboxes(limit: int | None = None) -> dict[str, int | str]:
    """Clean audit-backed sandboxes; disabled environments remain side-effect free."""
    if not bool(getattr(settings, "AGSX_ENABLED", False)):
        return {"status": "disabled", "scanned": 0, "terminated": 0, "failed": 0}
    repository = DjangoSandboxExecutionRepository()
    provider = TencentAgsxSandboxProvider(repository=repository)
    result = asyncio.run(
        SandboxReaper(provider, repository).run(
            limit=limit or int(getattr(settings, "AGSX_REAPER_BATCH_SIZE", 100))
        )
    )
    return {"status": "completed", **result.as_dict()}
