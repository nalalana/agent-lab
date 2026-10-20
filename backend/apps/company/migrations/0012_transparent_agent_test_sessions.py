from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("user", "0001_initial"),
        ("company", "0011_alter_companysystemsetting_id"),
    ]

    operations = [
        migrations.CreateModel(
            name="TransparentCompanyAgentTestSession",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("conversationId", models.CharField(max_length=255, unique=True)),
                ("title", models.CharField(blank=True, default="", max_length=255)),
                (
                    "status",
                    models.CharField(
                        choices=[("active", "active"), ("completed", "completed"), ("failed", "failed")],
                        default="active",
                        max_length=20,
                    ),
                ),
                ("messageCount", models.IntegerField(default=0)),
                ("lastMessage", models.TextField(blank=True, default="")),
                ("sessionKey", models.CharField(blank=True, default="", max_length=255)),
                ("created_at", models.CharField(max_length=20)),
                ("updated_at", models.CharField(max_length=20)),
                (
                    "agent",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="testSessions",
                        to="company.transparentcompanyagentdefinition",
                    ),
                ),
                (
                    "operator",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="transparentAgentTestSessions",
                        to="user.user",
                    ),
                ),
                (
                    "transparentCompany",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="agentTestSessions",
                        to="company.transparentcompany",
                    ),
                ),
            ],
            options={
                "db_table": "company_transparentcompanyagenttestsession",
                "ordering": ["-id"],
            },
        ),
        migrations.CreateModel(
            name="TransparentCompanyAgentTestMessage",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "role",
                    models.CharField(
                        choices=[("user", "user"), ("assistant", "assistant"), ("system", "system")],
                        default="user",
                        max_length=20,
                    ),
                ),
                ("content", models.TextField(blank=True, default="")),
                ("tokenCount", models.IntegerField(default=0)),
                ("toolCalls", models.JSONField(blank=True, default=list)),
                ("rawPayload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.CharField(max_length=20)),
                ("updated_at", models.CharField(max_length=20)),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="messages",
                        to="company.transparentcompanyagenttestsession",
                    ),
                ),
            ],
            options={
                "db_table": "company_transparentcompanyagenttestmessage",
                "ordering": ["id"],
            },
        ),
    ]
