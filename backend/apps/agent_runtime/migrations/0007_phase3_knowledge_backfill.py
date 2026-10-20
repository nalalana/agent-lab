import hashlib
import time

from django.db import migrations


def phase3_backfill(apps, schema_editor):
    Company = apps.get_model("company", "Company")
    Agent = apps.get_model("company", "TransparentCompanyAgentDefinition")
    VikingSpace = apps.get_model("company", "TransparentCompanyAgentVikingSpace")
    WorkspaceFile = apps.get_model("runtime", "WorkspaceFile")
    MemorySyncJob = apps.get_model("agent_runtime", "MemorySyncJob")

    _repair_viking_spaces(Company, Agent, VikingSpace)
    _create_knowledge_jobs(WorkspaceFile, MemorySyncJob)


def _repair_viking_spaces(Company, Agent, VikingSpace):
    company_cids = dict(Company.objects.values_list("id", "cid"))
    agents = Agent.objects.select_related("transparentCompany").all()
    for agent in agents.iterator():
        company_id = agent.transparentCompany.company_id
        cid = company_cids.get(company_id)
        if cid is None:
            continue
        account_id = f"tm-{_digest(cid)}"
        user_id = f"agent-{_digest(f'{cid}:{agent.agentId}')}"
        status = "archived" if agent.status == "deprecated" else "active"
        now = str(time.time())[:20]
        VikingSpace.objects.update_or_create(
            transparentCompany_id=agent.transparentCompany_id,
            agent_id=agent.pk,
            defaults={
                "vikingSpaceId": f"{account_id}:{user_id}",
                "accountId": account_id,
                "userId": user_id,
                "rootUri": f"viking://user/{user_id}/memories/",
                "status": status,
                "lastError": "",
                "created_at": now,
                "updated_at": now,
            },
        )


def _create_knowledge_jobs(WorkspaceFile, MemorySyncJob):
    files = WorkspaceFile.objects.filter(
        logical_path__startswith="knowledge/",
        storage_backend="tencent_cos",
    ).exclude(resource_sync_status="completed")
    for workspace_file in files.iterator():
        version_seed = workspace_file.checksum or workspace_file.storage_key
        version = hashlib.sha256(str(version_seed).encode("utf-8")).hexdigest()
        path_digest = hashlib.sha256(workspace_file.logical_path.encode("utf-8")).hexdigest()[:16]
        key = f"knowledge:{workspace_file.company_id}:{workspace_file.pk}:{path_digest}:{version}"
        MemorySyncJob.objects.get_or_create(
            idempotency_key=key,
            defaults={
                "company_id": workspace_file.company_id,
                "job_type": "knowledge_index",
                "source_ref": f"workspace_file:{workspace_file.pk}",
                "status": "pending",
            },
        )
        WorkspaceFile.objects.filter(pk=workspace_file.pk).update(
            resource_sync_status="pending",
            resource_task_id="",
            resource_last_error="",
        )


def _digest(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:24]


class Migration(migrations.Migration):

    dependencies = [
        ("runtime", "0014_workspacefile_knowledge_index_status"),
        ("agent_runtime", "0006_agent_tool_call"),
    ]

    operations = [
        migrations.RunPython(phase3_backfill, migrations.RunPython.noop),
    ]
