from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("runtime", "0006_runtime_governance_models"),
    ]

    operations = [
        migrations.RenameIndex(
            model_name="runtimedriftevent",
            new_name="runtime_dri_company_047aa7_idx",
            old_name="runtime_dri_company_bf9d65_idx",
        ),
        migrations.AlterField(
            model_name="runtimealertpolicy",
            name="status",
            field=models.CharField(
                choices=[("active", "active"), ("muted", "muted")],
                default="active",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="runtimedriftevent",
            name="drift_type",
            field=models.CharField(
                choices=[
                    ("config", "Config drift"),
                    ("gateway", "Gateway drift"),
                    ("workspace", "Workspace drift"),
                ],
                default="config",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="runtimedriftevent",
            name="resolution",
            field=models.CharField(
                blank=True,
                choices=[
                    ("republish", "Republish platform config"),
                    ("accept_runtime", "Accept runtime state"),
                    ("manual", "Manual resolution"),
                ],
                max_length=32,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="runtimedriftevent",
            name="status",
            field=models.CharField(
                choices=[
                    ("open", "Open"),
                    ("acked", "Acknowledged"),
                    ("resolved", "Resolved"),
                ],
                default="open",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="runtimerolloutpolicy",
            name="status",
            field=models.CharField(
                choices=[("active", "active"), ("paused", "paused")],
                default="active",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="runtimerolloutpolicy",
            name="strategy",
            field=models.CharField(
                choices=[("direct", "direct"), ("canary", "canary")],
                default="direct",
                max_length=32,
            ),
        ),
    ]
