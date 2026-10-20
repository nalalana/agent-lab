"""Best-effort cleanup for expired, terminal-Run, and failed sandboxes."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from django.utils import timezone as django_timezone

from .metrics import SandboxMetrics


logger = logging.getLogger("agent_runtime.sandbox.reaper")


@dataclass(frozen=True)
class ReaperResult:
    scanned: int
    terminated: int
    failed: int

    def as_dict(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "terminated": self.terminated,
            "failed": self.failed,
        }


class SandboxReaper:
    """Clean records independently so one provider failure cannot stop a batch."""

    def __init__(self, provider, repository, metrics=None, now=None) -> None:
        self.provider = provider
        self.repository = repository
        self.metrics = metrics or SandboxMetrics()
        self.now = now or django_timezone.now

    async def run(self, *, limit: int = 100) -> ReaperResult:
        records = await asyncio.to_thread(
            self.repository.find_reapable,
            now=self.now(),
            limit=max(int(limit), 1),
        )
        terminated = 0
        failed = 0
        for record in records:
            try:
                await self.provider.terminate(record.id)
                terminated += 1
                self.metrics.increment(
                    "sandbox.reaper.cleanup.total",
                    {"provider": record.provider, "outcome": "success"},
                )
            except Exception:
                failed += 1
                self.metrics.increment(
                    "sandbox.reaper.cleanup.total",
                    {"provider": record.provider, "outcome": "failed"},
                )
                logger.exception(
                    "sandbox reaper cleanup failed",
                    extra={"sandbox_execution_id": record.id, "run_id": record.run_id},
                )
        return ReaperResult(scanned=len(records), terminated=terminated, failed=failed)
