from django.db import migrations, models

import apps.moderation.models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="ImageModerationAsset",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("storage_key", models.CharField(max_length=1024, unique=True)),
                ("source", models.CharField(max_length=64)),
                ("source_field", models.CharField(blank=True, default="", max_length=64)),
                ("owner_type", models.CharField(blank=True, default="", max_length=32)),
                ("owner_id", models.CharField(blank=True, default="", max_length=128)),
                ("mime_type", models.CharField(blank=True, default="", max_length=128)),
                ("size", models.BigIntegerField(default=0)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "待审核"),
                            ("approved", "审核通过"),
                            ("rejected", "审核拒绝"),
                            ("review_required", "需要人工复核"),
                            ("failed", "审核失败"),
                        ],
                        default="pending",
                        max_length=32,
                    ),
                ),
                ("provider", models.CharField(blank=True, default="", max_length=32)),
                ("provider_job_id", models.CharField(blank=True, default="", max_length=128)),
                ("provider_result", models.JSONField(blank=True, default=dict)),
                ("suggestion", models.CharField(blank=True, default="", max_length=32)),
                ("label", models.CharField(blank=True, default="", max_length=64)),
                ("score", models.FloatField(blank=True, null=True)),
                ("failure_reason", models.TextField(blank=True, default="")),
                ("reviewed_by", models.CharField(blank=True, default="", max_length=128)),
                ("reviewed_at", models.CharField(blank=True, default="", max_length=20)),
                ("created_at", models.CharField(default=apps.moderation.models._now_ts, max_length=20)),
                ("updated_at", models.CharField(default=apps.moderation.models._now_ts, max_length=20)),
            ],
            options={
                "db_table": "moderation_imagemoderationasset",
                "indexes": [
                    models.Index(fields=["storage_key"], name="moderation__storage_02afd2_idx"),
                    models.Index(fields=["status"], name="moderation__status_d40caf_idx"),
                    models.Index(fields=["source", "source_field"], name="moderation__source_05d497_idx"),
                    models.Index(fields=["owner_type", "owner_id"], name="moderation__owner_t_606ddf_idx"),
                ],
            },
        ),
    ]
