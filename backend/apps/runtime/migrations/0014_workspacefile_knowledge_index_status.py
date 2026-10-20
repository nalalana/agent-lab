from django.db import migrations, models


def requeue_submitted_resources(apps, schema_editor):
    """Legacy `submitted` did not prove vector indexing had completed."""
    WorkspaceFile = apps.get_model("runtime", "WorkspaceFile")
    WorkspaceFile.objects.filter(resource_sync_status="submitted").update(
        resource_sync_status="pending",
        resource_task_id="",
        resource_last_error="",
        resource_synced_at="",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("runtime", "0013_workspacefile_openviking_resource"),
    ]

    operations = [
        migrations.RunPython(requeue_submitted_resources, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="workspacefile",
            name="resource_sync_status",
            field=models.CharField(
                choices=[
                    ("not_applicable", "非知识资源"),
                    ("pending", "待提交 OpenViking"),
                    ("indexing", "OpenViking 索引中"),
                    ("completed", "OpenViking 索引完成"),
                    ("failed", "OpenViking 同步失败"),
                ],
                default="not_applicable",
                max_length=32,
            ),
        ),
        migrations.AddIndex(
            model_name="workspacefile",
            index=models.Index(
                fields=["company", "resource_sync_status"],
                name="workspace_company_resource_idx",
            ),
        ),
    ]
