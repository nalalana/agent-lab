"""Logging helpers that are safe to import before Django apps are loaded."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone


class SandboxMetricJsonFormatter(logging.Formatter):
    """Serialize metric LogRecords as one valid, collector-friendly JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        occurred_at = datetime.fromtimestamp(record.created, tz=timezone.utc)
        payload = {
            "schemaVersion": 1,
            "event": "sandbox_metric",
            "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
            "metric_name": str(getattr(record, "metric_name", "")),
            "metric_value": float(getattr(record, "metric_value", 0)),
            "metric_labels": dict(getattr(record, "metric_labels", {}) or {}),
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
