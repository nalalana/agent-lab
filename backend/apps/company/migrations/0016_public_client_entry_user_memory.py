from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("user", "0001_initial"),
        ("company", "0015_transparentcompanyagenttestsession_memory_summary"),
    ]

    operations = [
        migrations.CreateModel(
            name="CompanyUserMemoryPolicy",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("enabled", models.BooleanField(default=False)),
                (
                    "status",
                    models.CharField(
                        choices=[("active", "active"), ("inactive", "inactive")],
                        default="inactive",
                        max_length=20,
                    ),
                ),
                ("maxRecallItems", models.IntegerField(default=6)),
                ("maxRecallTokens", models.IntegerField(default=600)),
                ("allowSensitive", models.BooleanField(default=False)),
                ("writebackEnabled", models.BooleanField(default=False)),
                (
                    "writebackMode",
                    models.CharField(
                        choices=[("disabled", "disabled"), ("shadow", "shadow")],
                        default="disabled",
                        max_length=20,
                    ),
                ),
                ("writeMinConfidence", models.FloatField(default=0.9)),
                ("created_at", models.CharField(max_length=20)),
                ("updated_at", models.CharField(max_length=20)),
                (
                    "company",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="userMemoryPolicy",
                        to="company.company",
                    ),
                ),
            ],
            options={
                "db_table": "company_companyusermemorypolicy",
            },
        ),
        migrations.CreateModel(
            name="TransparentCompanyAgentUserMemoryBinding",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "memoryScope",
                    models.CharField(
                        choices=[
                            ("disabled", "disabled"),
                            ("company_user", "company_user"),
                            ("agent_user", "agent_user"),
                        ],
                        default="company_user",
                        max_length=20,
                    ),
                ),
                ("enabled", models.BooleanField(default=False)),
                ("inheritCompanyPolicy", models.BooleanField(default=True)),
                ("maxRecallItemsOverride", models.IntegerField(blank=True, null=True)),
                ("maxRecallTokensOverride", models.IntegerField(blank=True, null=True)),
                ("note", models.CharField(blank=True, max_length=255, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[("active", "active"), ("inactive", "inactive")],
                        default="inactive",
                        max_length=20,
                    ),
                ),
                ("created_at", models.CharField(max_length=20)),
                ("updated_at", models.CharField(max_length=20)),
                (
                    "agent",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="userMemoryBindings",
                        to="company.transparentcompanyagentdefinition",
                    ),
                ),
                (
                    "transparentCompany",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="agentUserMemoryBindings",
                        to="company.transparentcompany",
                    ),
                ),
            ],
            options={
                "db_table": "company_transparentcompanyagentusermemorybinding",
                "unique_together": {("transparentCompany", "agent")},
            },
        ),
        migrations.CreateModel(
            name="CompanyUserMemoryItem",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "scope",
                    models.CharField(
                        choices=[("company", "company"), ("agent", "agent")],
                        default="company",
                        max_length=20,
                    ),
                ),
                ("memoryKey", models.CharField(max_length=255)),
                (
                    "memoryType",
                    models.CharField(
                        choices=[
                            ("identity", "identity"),
                            ("preference", "preference"),
                            ("constraint", "constraint"),
                            ("goal", "goal"),
                            ("followup", "followup"),
                        ],
                        default="preference",
                        max_length=20,
                    ),
                ),
                ("content", models.TextField(blank=True, default="")),
                ("confidence", models.FloatField(default=1.0)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("active", "active"),
                            ("stale", "stale"),
                            ("suppressed", "suppressed"),
                        ],
                        default="active",
                        max_length=20,
                    ),
                ),
                ("sourceMessageId", models.BigIntegerField(blank=True, null=True)),
                ("lastSeenAt", models.CharField(blank=True, default="", max_length=20)),
                ("lastConfirmedAt", models.CharField(blank=True, default="", max_length=20)),
                ("created_at", models.CharField(max_length=20)),
                ("updated_at", models.CharField(max_length=20)),
                (
                    "agent",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="userMemoryItems",
                        to="company.transparentcompanyagentdefinition",
                    ),
                ),
                (
                    "company",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="userMemoryItems",
                        to="company.company",
                    ),
                ),
                (
                    "sourceSession",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="userMemoryItems",
                        to="company.transparentcompanyagenttestsession",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="companyMemoryItems",
                        to="user.user",
                    ),
                ),
            ],
            options={
                "db_table": "company_companyusermemoryitem",
            },
        ),
        migrations.CreateModel(
            name="CompanyUserMemoryWriteLog",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("sourceMessageId", models.BigIntegerField(blank=True, null=True)),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("create", "create"),
                            ("update", "update"),
                            ("skip", "skip"),
                            ("suppress", "suppress"),
                        ],
                        max_length=20,
                    ),
                ),
                ("candidatePayload", models.JSONField(blank=True, default=dict)),
                ("resultPayload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.CharField(max_length=20)),
                (
                    "agent",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="userMemoryWriteLogs",
                        to="company.transparentcompanyagentdefinition",
                    ),
                ),
                (
                    "company",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="userMemoryWriteLogs",
                        to="company.company",
                    ),
                ),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="userMemoryWriteLogs",
                        to="company.transparentcompanyagenttestsession",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="companyMemoryWriteLogs",
                        to="user.user",
                    ),
                ),
            ],
            options={
                "db_table": "company_companyusermemorywritelog",
            },
        ),
    ]
