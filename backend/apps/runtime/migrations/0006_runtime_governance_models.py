from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("company", "0007_companysessionmemorypolicy_and_agentmemorybinding"),
        ("runtime", "0005_rename_runtime_ope_company_81dc89_idx_runtime_ope_company_565af2_idx_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="RuntimeAlertPolicy",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("status", models.CharField(choices=[("active", "active"), ("muted", "muted")], default="active", max_length=32)),
                ("channels", models.CharField(default="platform", max_length=255)),
                ("config_drift_enabled", models.BooleanField(default=True)),
                ("gateway_unhealthy_enabled", models.BooleanField(default=True)),
                ("workspace_sync_failed_enabled", models.BooleanField(default=True)),
                ("delivery_target", models.CharField(blank=True, max_length=255, null=True)),
                ("created_at", models.CharField(max_length=20)),
                ("updated_at", models.CharField(max_length=20)),
                ("company", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="runtime_alert_policy", to="company.company")),
            ],
            options={"db_table": "runtime_alertpolicy"},
        ),
        migrations.CreateModel(
            name="RuntimeRolloutPolicy",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("strategy", models.CharField(choices=[("direct", "direct"), ("canary", "canary")], default="direct", max_length=32)),
                ("status", models.CharField(choices=[("active", "active"), ("paused", "paused")], default="active", max_length=32)),
                ("canary_percentage", models.IntegerField(default=10)),
                ("auto_promote", models.BooleanField(default=False)),
                ("auto_promote_minutes", models.IntegerField(default=30)),
                ("rollback_on_alert", models.BooleanField(default=True)),
                ("created_at", models.CharField(max_length=20)),
                ("updated_at", models.CharField(max_length=20)),
                ("company", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="runtime_rollout_policy", to="company.company")),
            ],
            options={"db_table": "runtime_rolloutpolicy"},
        ),
        migrations.CreateModel(
            name="RuntimeDriftEvent",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("drift_type", models.CharField(choices=[("config", "config"), ("gateway", "gateway"), ("workspace", "workspace")], default="config", max_length=32)),
                ("status", models.CharField(choices=[("open", "open"), ("acked", "acked"), ("resolved", "resolved")], default="open", max_length=32)),
                ("summary", models.CharField(max_length=255)),
                ("detail", models.TextField(blank=True, null=True)),
                ("resolution", models.CharField(blank=True, choices=[("republish", "republish"), ("accept_runtime", "accept_runtime"), ("manual", "manual")], max_length=32, null=True)),
                ("resolved_by", models.CharField(blank=True, max_length=128, null=True)),
                ("resolved_at", models.CharField(blank=True, max_length=20, null=True)),
                ("created_at", models.CharField(max_length=20)),
                ("updated_at", models.CharField(max_length=20)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="runtime_drift_events", to="company.company")),
                ("instance", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="drift_events", to="runtime.openclawinstance")),
            ],
            options={"db_table": "runtime_driftevent"},
        ),
        migrations.AddIndex(
            model_name="runtimedriftevent",
            index=models.Index(fields=["company", "drift_type", "status"], name="runtime_dri_company_bf9d65_idx"),
        ),
    ]
