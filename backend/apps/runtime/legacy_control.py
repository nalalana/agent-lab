"""Phase 4 controls that freeze the retired OpenClaw data plane."""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import JsonResponse


MODE_READ_WRITE = "read_write"
MODE_READ_ONLY = "read_only"
SUPPORTED_MODES = frozenset({MODE_READ_WRITE, MODE_READ_ONLY})

LEGACY_MODEL_LABELS = frozenset(
    {
        "runtime.openclawinstance",
        "runtime.openclawprovisiontask",
        "runtime.openclawruntimesnapshot",
        "runtime.openclawconfigversion",
        "runtime.companyruntimebinding",
    }
)

LEGACY_MUTATION_PATHS = frozenset(
    {
        "/api/runtime/agent-tools/workspace",
        "/api/runtime/config/publish",
        "/api/runtime/config/publish-and-verify",
        "/api/runtime/config/readback",
        "/api/runtime/cron/jobs/create",
        "/api/runtime/cron/remove",
        "/api/runtime/cron/run",
        "/api/runtime/instance/bootstrap",
        "/api/runtime/instance/deploy-bridge",
        "/api/runtime/instance/health-check",
        "/api/runtime/instance/provision",
        "/api/runtime/instance/retry-provision",
        "/api/runtime/snapshots/backup",
        "/api/runtime/snapshots/restore",
    }
)


class LegacyRuntimeReadOnlyError(RuntimeError):
    """Raised before an application write reaches a retired legacy table."""


def legacy_runtime_write_mode() -> str:
    mode = str(getattr(settings, "OPENCLAW_LEGACY_WRITE_MODE", MODE_READ_ONLY)).strip().lower()
    if mode not in SUPPORTED_MODES:
        raise ImproperlyConfigured(
            "OPENCLAW_LEGACY_WRITE_MODE must be read_write or read_only."
        )
    return mode


def legacy_runtime_is_read_only() -> bool:
    return legacy_runtime_write_mode() == MODE_READ_ONLY


def legacy_runtime_skip_payload() -> dict:
    return {
        "skipped": True,
        "reason": "openclaw_legacy_read_only",
        "message": "Unified runtime is active; legacy OpenClaw synchronization is disabled.",
    }


class LegacyRuntimeWriteRouter:
    """Reject ORM writes to the five frozen models after the cutover gate."""

    def db_for_read(self, model, **hints):
        return None

    def db_for_write(self, model, **hints):
        label = str(getattr(model._meta, "label_lower", ""))
        if label in LEGACY_MODEL_LABELS and legacy_runtime_is_read_only():
            raise LegacyRuntimeReadOnlyError(
                f"Legacy OpenClaw model {label} is read-only."
            )
        return None

    def allow_relation(self, obj1, obj2, **hints):
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return None


class LegacyRuntimeReadOnlyMiddleware:
    """Stop external legacy mutations before calling providers or bridges."""

    MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._is_blocked(request):
            return self._read_only_response()
        try:
            return self.get_response(request)
        except LegacyRuntimeReadOnlyError:
            return self._read_only_response()

    @classmethod
    def _is_blocked(cls, request) -> bool:
        path = str(getattr(request, "path", "") or "").rstrip("/")
        return bool(
            legacy_runtime_is_read_only()
            and request.method.upper() in cls.MUTATING_METHODS
            and path in LEGACY_MUTATION_PATHS
        )

    @staticmethod
    def _read_only_response():
        return JsonResponse(
            {
                "schemaVersion": 1,
                "result": 0,
                "message": "OpenClaw旧运行时已归档，当前仅支持只读查询",
                "data": {"retired": True},
            },
            status=410,
        )
