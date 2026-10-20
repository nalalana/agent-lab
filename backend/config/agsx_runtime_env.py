"""Load the generated Phase4-C runtime environment with a strict allowlist."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import dotenv_values


RUNTIME_ENV_KEYS = frozenset(
    {
        "AGSX_ENABLED",
        "E2B_DOMAIN",
        "E2B_API_KEY",
        "AGSX_TEMPLATE",
        "AGSX_DEFAULT_CPU_CORES",
        "AGSX_DEFAULT_MEMORY_MIB",
        "AGSX_DEFAULT_TTL_SECONDS",
        "AGSX_NETWORK_POLICY",
        "AGSX_REQUEST_TIMEOUT_SECONDS",
        "AGSX_SANDBOX_CLEANUP_GRACE_SECONDS",
        "AGSX_REAPER_BATCH_SIZE",
        "AGSX_GLOBAL_MAX_CONCURRENT_SANDBOXES",
        "AGSX_FAIL_CLOSED_ON_QUOTA_BACKEND_ERROR",
        "AGSX_ESTIMATED_COST_MICRO_UNITS_PER_SECOND_2C2G",
        "AGSX_DEFAULT_COMPANY_DAILY_COST_BUDGET_MICRO_UNITS",
        "AGSX_GLOBAL_DAILY_COST_BUDGET_MICRO_UNITS",
        "AGSX_COST_CURRENCY",
        "AGSX_ENFORCE_EFFECTIVE_RESOURCES",
        "AGSX_ALERT_FAILURE_WINDOW_MINUTES",
        "AGSX_ALERT_FAILURE_RATE",
        "AGSX_ALERT_FAILURE_MIN_CREATES",
        "AGENT_RUNTIME_ARTIFACT_COS_BUCKET",
        "AGENT_RUNTIME_ARTIFACT_COS_REGION",
        "AGENT_RUNTIME_ARTIFACT_COS_SECRET_ID",
        "AGENT_RUNTIME_ARTIFACT_COS_SECRET_KEY",
        "AGENT_RUNTIME_ARTIFACT_COS_TOKEN",
        "AGENT_RUNTIME_ARTIFACT_COS_PREFIX",
    }
)


def load_agsx_runtime_env(raw_path: str) -> Path:
    """Load a script-generated runtime.env and reject every unexpected key."""
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise ImproperlyConfigured("AGSX_RUNTIME_ENV_FILE must be an absolute path")
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ImproperlyConfigured(f"AGSX runtime environment does not exist: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ImproperlyConfigured(
            f"AGSX runtime environment must be a regular non-symlink file: {path}"
        )

    values = dotenv_values(dotenv_path=path, interpolate=False)
    unknown = sorted(set(values) - RUNTIME_ENV_KEYS)
    if unknown:
        raise ImproperlyConfigured(
            "AGSX runtime environment contains forbidden keys: " + ", ".join(unknown)
        )
    invalid = sorted(key for key, value in values.items() if value is None)
    if invalid:
        raise ImproperlyConfigured(
            "AGSX runtime environment contains entries without values: " + ", ".join(invalid)
        )
    for key, value in values.items():
        assert value is not None
        os.environ[key] = value
    return path
