from django.db import migrations, models
import django.db.models.deletion

import apps.moderation.models


class Migration(migrations.Migration):
    dependencies = [
        ("moderation", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="TextModerationAsset",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("content_hash", models.CharField(max_length=64)),
                ("content_preview", models.CharField(blank=True, default="", max_length=512)),
                ("source", models.CharField(max_length=64)),
                ("source_field", models.CharField(blank=True, default="", max_length=64)),
                ("owner_type", models.CharField(blank=True, default="", max_length=32)),
                ("owner_id", models.CharField(blank=True, default="", max_length=128)),
                ("actor_type", models.CharField(blank=True, default="", max_length=32)),
                ("actor_id", models.CharField(blank=True, default="", max_length=128)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "pending"),
                            ("approved", "approved"),
                            ("rejected", "rejected"),
                            ("review_required", "review_required"),
                            ("failed", "failed"),
                        ],
                        default="pending",
                        max_length=32,
                    ),
                ),
                ("provider", models.CharField(blank=True, default="", max_length=32)),
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
                "db_table": "moderation_textmoderationasset",
                "indexes": [
                    models.Index(fields=["content_hash"], name="moderation__content_b70d38_idx"),
                    models.Index(fields=["status"], name="moderation__status_8dd15b_idx"),
                    models.Index(fields=["source", "source_field"], name="moderation__source_242ec9_idx"),
                    models.Index(fields=["owner_type", "owner_id"], name="moderation__owner_t_28e684_idx"),
                    models.Index(fields=["actor_type", "actor_id"], name="moderation__actor_t_bf4d4b_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="SensitiveWordRule",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("word", models.CharField(max_length=255, unique=True)),
                ("normalized_word", models.CharField(db_index=True, max_length=255)),
                ("label", models.CharField(blank=True, default="sensitive_word", max_length=64)),
                (
                    "action",
                    models.CharField(
                        choices=[("reject", "reject"), ("review_required", "review_required")],
                        default="reject",
                        max_length=32,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("active", "active"), ("inactive", "inactive")],
                        default="active",
                        max_length=20,
                    ),
                ),
                ("created_by", models.CharField(blank=True, default="", max_length=128)),
                ("created_at", models.CharField(default=apps.moderation.models._now_ts, max_length=20)),
                ("updated_at", models.CharField(default=apps.moderation.models._now_ts, max_length=20)),
            ],
            options={
                "db_table": "moderation_sensitivewordrule",
                "indexes": [
                    models.Index(fields=["status"], name="moderation__status_053e13_idx"),
                    models.Index(fields=["label"], name="moderation__label_2e65cc_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="TextModerationEvent",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("event_type", models.CharField(max_length=64)),
                ("from_status", models.CharField(blank=True, default="", max_length=32)),
                ("to_status", models.CharField(blank=True, default="", max_length=32)),
                ("actor_type", models.CharField(blank=True, default="", max_length=32)),
                ("actor_id", models.CharField(blank=True, default="", max_length=128)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.CharField(default=apps.moderation.models._now_ts, max_length=20)),
                (
                    "asset",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="events",
                        to="moderation.textmoderationasset",
                    ),
                ),
            ],
            options={
                "db_table": "moderation_textmoderationevent",
                "indexes": [
                    models.Index(fields=["asset", "created_at"], name="moderation__asset_i_4c5261_idx"),
                    models.Index(fields=["event_type"], name="moderation__event_t_1e911d_idx"),
                ],
            },
        ),
    ]
