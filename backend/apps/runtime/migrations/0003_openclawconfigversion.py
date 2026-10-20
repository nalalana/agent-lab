from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("runtime", "0002_rename_runtime_open_company_1f148d_idx_runtime_ope_company_1aee5e_idx_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="OpenClawConfigVersion",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("version", models.CharField(max_length=64, unique=True)),
                ("schema_version", models.CharField(default="v2-preview", max_length=64)),
                (
                    "source",
                    models.CharField(
                        choices=[("platform_gui", "平台 GUI"), ("gitee_import", "Gitee 导入")],
                        default="platform_gui",
                        max_length=32,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("draft", "草稿"), ("published", "已发布"), ("superseded", "已过期")],
                        default="draft",
                        max_length=32,
                    ),
                ),
                ("created_by", models.CharField(blank=True, max_length=128, null=True)),
                ("notes", models.TextField(blank=True, null=True)),
                ("config_payload", models.TextField()),
                ("published_at", models.CharField(blank=True, max_length=20, null=True)),
                ("created_at", models.CharField(max_length=20)),
                ("updated_at", models.CharField(max_length=20)),
                (
                    "company",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="runtime_config_versions",
                        to="company.company",
                    ),
                ),
                (
                    "instance",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="config_versions",
                        to="runtime.openclawinstance",
                    ),
                ),
            ],
            options={
                "db_table": "runtime_openclawconfigversion",
            },
        ),
        migrations.AddIndex(
            model_name="openclawconfigversion",
            index=models.Index(fields=["company", "status"], name="runtime_ope_company_81dc89_idx"),
        ),
        migrations.AddIndex(
            model_name="openclawconfigversion",
            index=models.Index(fields=["company", "created_at"], name="runtime_ope_company_0c4e95_idx"),
        ),
    ]
