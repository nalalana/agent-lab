"""Persistence boundary for the Phase 2 ``SandboxExecution`` model.

The lazy model lookup keeps provider unit tests independent from Django model
imports and makes incomplete deployments fail explicitly instead of silently
operating without durable audit state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable
from uuid import uuid4

from django.apps import apps
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .errors import SandboxProviderError
from .policy import ResolvedSandboxPolicy


STATUS_CREATING = "creating"
STATUS_READY = "ready"
STATUS_RUNNING = "running"
STATUS_TERMINATED = "terminated"
STATUS_FAILED = "failed"
ACTIVE_STATUSES = frozenset({STATUS_CREATING, STATUS_READY, STATUS_RUNNING})


class SandboxSchemaUnavailable(SandboxProviderError):
    """Raised when the SandboxExecution model/Migration is unavailable."""


@dataclass(frozen=True)
class SandboxRecord:
    id: str
    run_id: str
    company_id: object
    provider: str
    external_id: str
    status: str
    template: str
    cpu_cores: int
    memory_mib: int
    network_policy: str
    expires_at: datetime
    created_at: datetime
    ready_at: datetime | None = None
    terminated_at: datetime | None = None
    billed_seconds: int = 0
    error_code: str = ""


class DjangoSandboxExecutionRepository:
    provider_name = "tencent_agsx"

    @property
    def model(self):
        try:
            return apps.get_model("agent_runtime", "SandboxExecution")
        except LookupError as exc:
            raise SandboxSchemaUnavailable(
                "SandboxExecution model is required before AGSX can be enabled"
            ) from exc

    @transaction.atomic
    def reserve(
        self,
        *,
        run_id: str,
        policy: ResolvedSandboxPolicy,
        expires_at: datetime,
    ) -> tuple[SandboxRecord, bool]:
        model = self.model
        run_model = model._meta.get_field("run").remote_field.model
        try:
            run_company_id = run_model.objects.values_list("company_id", flat=True).get(
                pk=run_id
            )
        except run_model.DoesNotExist as exc:
            raise SandboxProviderError("sandbox Run does not exist") from exc
        if run_company_id != policy.company_id:
            raise SandboxProviderError("sandbox Run belongs to another company")

        instance, created = model.objects.get_or_create(
            run_id=run_id,
            defaults={
                "id": uuid4(),
                "company_id": policy.company_id,
                "provider": self.provider_name,
                "external_id": "",
                "status": STATUS_CREATING,
                "template": policy.template,
                "cpu_cores": policy.cpu_cores,
                "memory_mib": policy.memory_mib,
                "network_policy": policy.network_policy,
                "expires_at": expires_at,
                "billed_seconds": 0,
                "error_code": "",
            },
        )
        if instance.company_id != run_company_id:
            raise SandboxProviderError("sandbox Run belongs to another company")
        return self._record(instance), created

    def get(self, record_id: str) -> SandboxRecord:
        try:
            return self._record(self.model.objects.get(pk=record_id))
        except self.model.DoesNotExist as exc:
            raise SandboxProviderError("sandbox execution does not exist") from exc

    def get_by_external_id(self, external_id: str) -> SandboxRecord | None:
        instance = self.model.objects.filter(external_id=external_id).first()
        return self._record(instance) if instance else None

    def mark_provider_instance(self, record_id: str, *, external_id: str) -> SandboxRecord:
        """Persist the supplier ID before any post-create validation.

        A failed validation or transient cleanup error must leave enough durable
        information for the Reaper to reconnect and remove the cloud instance.
        """
        value = str(external_id).strip()
        if not value:
            raise SandboxProviderError("sandbox provider instance ID is missing")
        updated = self.model.objects.filter(
            pk=record_id,
            status=STATUS_CREATING,
            external_id="",
        ).update(external_id=value)
        if not updated:
            existing = self.get(record_id)
            if existing.status != STATUS_CREATING or existing.external_id != value:
                raise SandboxProviderError("sandbox provider instance could not be recorded")
            return existing
        return self.get(record_id)

    def mark_ready(
        self,
        record_id: str,
        *,
        external_id: str,
        ready_at: datetime,
        expires_at: datetime,
    ) -> SandboxRecord:
        self.model.objects.filter(pk=record_id).update(
            external_id=external_id,
            status=STATUS_READY,
            ready_at=ready_at,
            expires_at=expires_at,
            error_code="",
        )
        return self.get(record_id)

    def mark_running(self, record_id: str) -> None:
        updated = self.model.objects.filter(
            pk=record_id,
            status__in=(STATUS_READY, STATUS_RUNNING),
        ).update(status=STATUS_RUNNING)
        if not updated:
            raise SandboxProviderError("sandbox is not ready for execution")

    def mark_ready_after_execute(self, record_id: str) -> None:
        self.model.objects.filter(pk=record_id, status=STATUS_RUNNING).update(status=STATUS_READY)

    def mark_failed(self, record_id: str, error_code: str) -> None:
        self.model.objects.filter(pk=record_id).exclude(status=STATUS_TERMINATED).update(
            status=STATUS_FAILED,
            error_code=str(error_code)[:64],
        )

    @transaction.atomic
    def mark_terminated(
        self,
        record_id: str,
        *,
        terminated_at: datetime,
        billed_seconds: int,
    ) -> None:
        billed = max(int(billed_seconds), 0)
        instance = self.model.objects.filter(pk=record_id).only("run_id").first()
        if instance is None:
            return
        self.model.objects.filter(pk=record_id).update(
            status=STATUS_TERMINATED,
            terminated_at=terminated_at,
            billed_seconds=billed,
            error_code="",
        )
        # AgentRun is the billing/alert aggregation source. Assignment keeps
        # terminate idempotent instead of double-counting repeated cleanup.
        run_model = self.model._meta.get_field("run").remote_field.model
        run_model.objects.filter(pk=instance.run_id).update(sandbox_seconds=billed)

    def find_reapable(self, *, now: datetime, limit: int) -> Iterable[SandboxRecord]:
        terminal_runs = ("COMPLETED", "FAILED", "CANCELLED")
        queryset = (
            self.model.objects.filter(
                Q(status__in=ACTIVE_STATUSES, expires_at__lte=now)
                | Q(status__in=ACTIVE_STATUSES, run__status__in=terminal_runs)
                | Q(status=STATUS_FAILED, external_id__gt="")
            )
            .select_related("run")
            .order_by("expires_at")[: max(int(limit), 1)]
        )
        return tuple(self._record(instance) for instance in queryset)

    @staticmethod
    def _record(instance) -> SandboxRecord:
        if instance is None:
            raise SandboxProviderError("sandbox execution does not exist")
        return SandboxRecord(
            id=str(instance.pk),
            run_id=str(instance.run_id),
            company_id=instance.company_id,
            provider=str(instance.provider),
            external_id=str(instance.external_id or ""),
            status=str(instance.status),
            template=str(instance.template),
            cpu_cores=int(instance.cpu_cores),
            memory_mib=int(instance.memory_mib),
            network_policy=str(instance.network_policy),
            expires_at=instance.expires_at,
            created_at=instance.created_at or timezone.now(),
            ready_at=instance.ready_at,
            terminated_at=instance.terminated_at,
            billed_seconds=int(instance.billed_seconds or 0),
            error_code=str(instance.error_code or ""),
        )
