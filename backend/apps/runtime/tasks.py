from celery import shared_task

from apps.runtime.legacy_control import (
    legacy_runtime_is_read_only,
    legacy_runtime_skip_payload,
)
from apps.runtime.models import OpenClawProvisionTask, WorkspaceTransferTask
from apps.runtime.services import OpenClawRuntimeService


def _skip_retired_runtime_task():
    """Prevent queued legacy jobs from reaching providers after cutover."""
    if legacy_runtime_is_read_only():
        return legacy_runtime_skip_payload()
    return None


def _retired_infrastructure_task_payload():
    """Acknowledge stale queue messages without touching retired providers."""

    return {
        **legacy_runtime_skip_payload(),
        "schemaVersion": 1,
        "retired": True,
    }


@shared_task
def provision_openclaw_instance_task(task_id: int):
    return _retired_infrastructure_task_payload()


@shared_task
def bootstrap_openclaw_instance_task(task_id: int):
    return _retired_infrastructure_task_payload()


@shared_task
def deploy_lighthouse_bridge_task(task_id: int):
    return _retired_infrastructure_task_payload()


@shared_task
def check_openclaw_health_task(instance_id: int):
    return _retired_infrastructure_task_payload()


@shared_task
def readback_openclaw_config_task(task_id: int):
    if skipped := _skip_retired_runtime_task():
        return skipped
    task = OpenClawProvisionTask.objects.get(id=task_id)
    return OpenClawRuntimeService.run_config_readback(task)


@shared_task
def sync_workspace_transfer_task(transfer_task_id: int):
    transfer_task = WorkspaceTransferTask.objects.get(id=transfer_task_id)
    return OpenClawRuntimeService.run_workspace_transfer(transfer_task)


@shared_task
def sync_openviking_resource_task(
    company_id: str,
    file_id: int | None,
    operation: str,
    logical_path: str,
    previous_logical_path: str = "",
    mime_type: str = "",
):
    return OpenClawRuntimeService.run_openviking_resource_sync(
        company_id=company_id,
        file_id=file_id,
        operation=operation,
        logical_path=logical_path,
        previous_logical_path=previous_logical_path,
        mime_type=mime_type,
    )


@shared_task
def backup_openclaw_runtime_state_task(instance_id: int):
    if skipped := _skip_retired_runtime_task():
        return skipped
    from apps.runtime.models import OpenClawInstance

    instance = OpenClawInstance.objects.get(id=instance_id)
    return OpenClawRuntimeService.backup_runtime_state(instance)


@shared_task
def restore_openclaw_runtime_state_task(instance_id: int, snapshot_id: int | None = None):
    if skipped := _skip_retired_runtime_task():
        return skipped
    from apps.runtime.models import OpenClawInstance

    instance = OpenClawInstance.objects.get(id=instance_id)
    return OpenClawRuntimeService.restore_runtime_state(instance, snapshot_id=snapshot_id)


@shared_task
def sync_runtime_server_resources_task():
    return OpenClawRuntimeService.list_server_resources(limit=1, offset=0)

@shared_task
def release_expired_runtime_resources_task(limit: int = 100, dry_run: bool = False):
    if skipped := _skip_retired_runtime_task():
        return skipped
    return OpenClawRuntimeService.release_expired_runtime_resources(limit=limit, dry_run=dry_run)
