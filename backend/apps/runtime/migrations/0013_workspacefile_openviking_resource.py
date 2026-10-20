from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("runtime", "0012_companyruntimebinding_openclawruntimesnapshot_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="workspacefile",
            name="resource_uri",
            field=models.CharField(blank=True, default="", max_length=1024),
        ),
        migrations.AddField(
            model_name="workspacefile",
            name="resource_sync_status",
            field=models.CharField(
                choices=[
                    ("not_applicable", "非知识资源"),
                    ("pending", "待提交 OpenViking"),
                    ("submitted", "已提交 OpenViking"),
                    ("failed", "OpenViking 同步失败"),
                ],
                default="not_applicable",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="workspacefile",
            name="resource_task_id",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="workspacefile",
            name="resource_last_error",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="workspacefile",
            name="resource_synced_at",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
    ]
