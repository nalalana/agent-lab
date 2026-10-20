from django.db import migrations, models
import django.db.models.deletion


def backfill_transfer_file_snapshot(apps, schema_editor):
    WorkspaceTransferTask = apps.get_model("runtime", "WorkspaceTransferTask")
    for task in WorkspaceTransferTask.objects.select_related("file").iterator():
        file = task.file
        if not file:
            continue
        task.file_logical_path = file.logical_path or ""
        task.file_name = file.file_name or ""
        task.file_mime_type = file.mime_type or ""
        task.file_size = file.size or 0
        task.save(update_fields=["file_logical_path", "file_name", "file_mime_type", "file_size"])


class Migration(migrations.Migration):

    dependencies = [
        ("runtime", "0009_alter_runtimealertpolicy_status_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="workspacetransfertask",
            name="file_logical_path",
            field=models.CharField(blank=True, default="", max_length=512),
        ),
        migrations.AddField(
            model_name="workspacetransfertask",
            name="file_mime_type",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="workspacetransfertask",
            name="file_name",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="workspacetransfertask",
            name="file_size",
            field=models.BigIntegerField(default=0),
        ),
        migrations.RunPython(backfill_transfer_file_snapshot, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="workspacetransfertask",
            name="file",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="transfer_tasks",
                to="runtime.workspacefile",
            ),
        ),
    ]
