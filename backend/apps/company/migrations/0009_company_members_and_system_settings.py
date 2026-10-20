from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("user", "0001_initial"),
        ("company", "0008_permission_templates_and_configs"),
    ]

    operations = [
        migrations.CreateModel(
            name="CompanyMember",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("memberUid", models.CharField(blank=True, default="", max_length=255)),
                ("name", models.CharField(max_length=255)),
                ("email", models.CharField(blank=True, default="", max_length=255)),
                ("role", models.CharField(default="member", max_length=100)),
                (
                    "status",
                    models.CharField(
                        choices=[("active", "active"), ("invited", "invited"), ("disabled", "disabled")],
                        default="invited",
                        max_length=20,
                    ),
                ),
                ("lastActiveAt", models.CharField(blank=True, default="", max_length=50)),
                ("invitedByUid", models.CharField(blank=True, default="", max_length=255)),
                ("created_at", models.CharField(max_length=20)),
                ("updated_at", models.CharField(max_length=20)),
                (
                    "company",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="members",
                        to="company.company",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="companyMemberships",
                        to="user.user",
                    ),
                ),
            ],
            options={
                "db_table": "company_companymember",
                "unique_together": {("company", "email")},
            },
        ),
        migrations.CreateModel(
            name="CompanySystemSetting",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("displayName", models.CharField(blank=True, default="", max_length=255)),
                ("systemDescription", models.TextField(blank=True, default="")),
                ("enablePublicEntry", models.BooleanField(default=True)),
                ("showMonitoringCards", models.BooleanField(default=True)),
                ("requireDiffBeforePublish", models.BooleanField(default=True)),
                ("alertOnDrift", models.BooleanField(default=True)),
                ("alertWebhook", models.CharField(blank=True, default="", max_length=512)),
                ("allowBrowserDirectCosUpload", models.BooleanField(default=True)),
                ("workspacePrefix", models.CharField(blank=True, default="", max_length=255)),
                ("syncTimeoutSeconds", models.IntegerField(default=30)),
                ("defaultEntryAgentId", models.CharField(blank=True, default="", max_length=100)),
                ("defaultModelId", models.CharField(blank=True, default="", max_length=100)),
                ("saveAgentTestSessions", models.BooleanField(default=True)),
                ("notifyPublishSuccess", models.BooleanField(default=True)),
                ("notifyAuditTasks", models.BooleanField(default=True)),
                ("notifyDriftDetected", models.BooleanField(default=True)),
                ("enabled", models.BooleanField(default=True)),
                ("created_at", models.CharField(max_length=20)),
                ("updated_at", models.CharField(max_length=20)),
                (
                    "company",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="systemSetting",
                        to="company.company",
                    ),
                ),
            ],
            options={
                "db_table": "company_companysystemsetting",
            },
        ),
    ]
