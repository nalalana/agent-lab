"""Low-cardinality structured sandbox metrics without a new dependency."""

from __future__ import annotations

import logging
from typing import Mapping


logger = logging.getLogger("agent_runtime.sandbox.metrics")


class SandboxMetrics:
    """Emit structured records that can be collected by the deployment stack."""

    ALLOWED_LABELS = frozenset(
        {"provider", "template", "outcome", "error_code", "environment", "quota_scope"}
    )

    def observe(self, name: str, value: int | float, labels: Mapping[str, object] | None = None) -> None:
        logger.info(
            "sandbox_metric",
            extra={
                "metric_name": str(name),
                "metric_value": float(value),
                "metric_labels": self._labels(labels),
            },
        )

    def increment(self, name: str, labels: Mapping[str, object] | None = None) -> None:
        self.observe(name, 1, labels)

    @classmethod
    def _labels(cls, labels: Mapping[str, object] | None) -> dict[str, str]:
        return {
            str(key): str(value)[:128]
            for key, value in dict(labels or {}).items()
            if key in cls.ALLOWED_LABELS
        }


class NullSandboxMetrics(SandboxMetrics):
    """Silent test implementation."""

    def observe(self, name: str, value: int | float, labels=None) -> None:
        return None
