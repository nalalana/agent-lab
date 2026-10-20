from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("company", "0007_companysessionmemorypolicy_and_agentmemorybinding"),
    ]

    operations = [
        migrations.CreateModel(
            name="TransparentCompanyPermissionTemplate",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("templateId", models.CharField(max_length=100)),
                ("name", models.CharField(max_length=255)),
                ("description", models.TextField(blank=True, default="")),
                ("agentType", models.CharField(blank=True, default="", max_length=100)),
                ("skillsInclude", models.JSONField(blank=True, default=list)),
                ("skillsExclude", models.JSONField(blank=True, default=list)),
                ("toolsAllow", models.JSONField(blank=True, default=list)),
                ("toolsDeny", models.JSONField(blank=True, default=list)),
                ("knowledgeRead", models.JSONField(blank=True, default=list)),
                ("knowledgeDeny", models.JSONField(blank=True, default=list)),
                ("enabled", models.BooleanField(default=True)),
                ("status", models.CharField(choices=[("active", "active"), ("inactive", "inactive")], default="active", max_length=20)),
                ("created_at", models.CharField(max_length=20)),
                ("updated_at", models.CharField(max_length=20)),
                (
                    "transparentCompany",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="permissionTemplates",
                        to="company.transparentcompany",
                    ),
                ),
            ],
            options={
                "db_table": "company_transparentcompanypermissiontemplate",
                "unique_together": {("transparentCompany", "templateId")},
            },
        ),
        migrations.CreateModel(
            name="CompanyPermissionConfig",
            fields=[
                (
                    "company",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="permissionConfig",
                        serialize=False,
                        to="company.company",
                    ),
                ),
                ("configJson", models.JSONField(blank=True, default=dict)),
                ("enabled", models.BooleanField(default=True)),
                ("updated_at", models.CharField(max_length=20)),
            ],
            options={
                "db_table": "company_companypermissionconfig",
            },
        ),
        migrations.CreateModel(
            name="TransparentCompanyAgentPermissionBinding",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("skillsInclude", models.JSONField(blank=True, default=list)),
                ("skillsExclude", models.JSONField(blank=True, default=list)),
                ("toolsAllow", models.JSONField(blank=True, default=list)),
                ("toolsDeny", models.JSONField(blank=True, default=list)),
                ("knowledgeRead", models.JSONField(blank=True, default=list)),
                ("knowledgeDeny", models.JSONField(blank=True, default=list)),
                ("enabled", models.BooleanField(default=False)),
                ("status", models.CharField(choices=[("active", "active"), ("inactive", "inactive")], default="inactive", max_length=20)),
                ("created_at", models.CharField(max_length=20)),
                ("updated_at", models.CharField(max_length=20)),
                (
                    "agent",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="permissionBindings",
                        to="company.transparentcompanyagentdefinition",
                    ),
                ),
                (
                    "template",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="agentBindings",
                        to="company.transparentcompanypermissiontemplate",
                    ),
                ),
                (
                    "transparentCompany",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="agentPermissionBindings",
                        to="company.transparentcompany",
                    ),
                ),
            ],
            options={
                "db_table": "company_transparentcompanyagentpermissionbinding",
                "unique_together": {("transparentCompany", "agent")},
            },
        ),
    ]
