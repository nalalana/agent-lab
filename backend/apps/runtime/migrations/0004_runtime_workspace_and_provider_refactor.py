from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("runtime", "0003_openclawconfigversion"),
    ]

    operations = [
        migrations.AddField(
            model_name="openclawinstance",
            name="config_path",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="openclawinstance",
            name="config_status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("in_sync", "In sync"),
                    ("drifted", "Drifted"),
                    ("readback_failed", "Readback failed"),
                ],
                default="pending",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="openclawinstance",
            name="connection_mode",
            field=models.CharField(
                choices=[("platform_ssh", "Platform SSH"), ("agent", "Instance agent")],
                default="platform_ssh",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="openclawinstance",
            name="current_runtime_version",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="openclawinstance",
            name="gateway_endpoint",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="openclawinstance",
            name="gateway_port",
            field=models.IntegerField(default=18789),
        ),
        migrations.AddField(
            model_name="openclawinstance",
            name="host_id",
            field=models.CharField(blank=True, max_length=128, null=True),
        ),
        migrations.AddField(
            model_name="openclawinstance",
            name="last_readback_at",
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
        migrations.AddField(
            model_name="openclawinstance",
            name="runtime_namespace",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="openclawinstance",
            name="workspace_mode",
            field=models.CharField(
                choices=[("cos_backed", "COS backed"), ("local_only", "Local only")],
                default="cos_backed",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="openclawinstance",
            name="workspace_prefix",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AlterField(
            model_name="openclawinstance",
            name="provider",
            field=models.CharField(
                choices=[
                    ("docker_shared_host", "Shared host Docker"),
                    ("tencent_lighthouse", "Tencent Lighthouse"),
                ],
                default="docker_shared_host",
                max_length=64,
            ),
        ),
        migrations.AlterField(
            model_name="openclawprovisiontask",
            name="task_type",
            field=models.CharField(
                choices=[
                    ("create_instance", "Create instance"),
                    ("bootstrap_openclaw", "Bootstrap OpenClaw"),
                    ("health_check", "Health check"),
                    ("retry", "Retry"),
                    ("config_readback", "Config readback"),
                    ("workspace_sync", "Workspace sync"),
                ],
                max_length=64,
            ),
        ),
        migrations.AlterField(
            model_name="openclawauditlog",
            name="target_type",
            field=models.CharField(
                choices=[
                    ("instance", "Instance"),
                    ("task", "Task"),
                    ("config", "Config"),
                    ("agent", "Agent"),
                    ("skill", "Skill"),
                    ("file", "File"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddIndex(
            model_name="openclawinstance",
            index=models.Index(fields=["provider", "host_id"], name="runtime_ope_provide_6f4248_idx"),
        ),
        migrations.AddIndex(
            model_name="openclawinstance",
            index=models.Index(fields=["runtime_namespace"], name="runtime_ope_runtime_57cecf_idx"),
        ),
        migrations.CreateModel(
            name="WorkspaceFile",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("logical_path", models.CharField(max_length=512)),
                ("file_name", models.CharField(max_length=255)),
                (
                    "storage_backend",
                    models.CharField(
                        choices=[("tencent_cos", "Tencent COS"), ("runtime_cache", "Runtime cache")],
                        default="tencent_cos",
                        max_length=32,
                    ),
                ),
                ("storage_key", models.CharField(max_length=1024)),
                ("checksum", models.CharField(blank=True, max_length=128, null=True)),
                ("mime_type", models.CharField(blank=True, max_length=128, null=True)),
                ("size", models.BigIntegerField(default=0)),
                (
                    "sync_status",
                    models.CharField(
                        choices=[("pending", "Pending"), ("in_sync", "In sync"), ("failed", "Failed")],
                        default="pending",
                        max_length=32,
                    ),
                ),
                ("presigned_url_expire_at", models.CharField(blank=True, max_length=20, null=True)),
                ("created_by", models.CharField(blank=True, max_length=128, null=True)),
                ("created_at", models.CharField(max_length=20)),
                ("updated_at", models.CharField(max_length=20)),
                (
                    "company",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="workspace_files",
                        to="company.company",
                    ),
                ),
                (
                    "instance",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="workspace_files",
                        to="runtime.openclawinstance",
                    ),
                ),
            ],
            options={"db_table": "runtime_workspacefile"},
        ),
        migrations.AddIndex(
            model_name="workspacefile",
            index=models.Index(fields=["company", "logical_path"], name="runtime_wor_company_7dd4de_idx"),
        ),
        migrations.AddIndex(
            model_name="workspacefile",
            index=models.Index(fields=["company", "sync_status"], name="runtime_wor_company_2c786b_idx"),
        ),
        migrations.CreateModel(
            name="WorkspaceTransferTask",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "direction",
                    models.CharField(
                        choices=[
                            ("platform_to_runtime", "Platform to runtime"),
                            ("runtime_to_platform", "Runtime to platform"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("running", "Running"),
                            ("succeeded", "Succeeded"),
                            ("failed", "Failed"),
                        ],
                        default="pending",
                        max_length=32,
                    ),
                ),
                ("logical_path", models.CharField(max_length=512)),
                ("request_payload", models.TextField(blank=True, null=True)),
                ("result_payload", models.TextField(blank=True, null=True)),
                ("error_message", models.TextField(blank=True, null=True)),
                ("started_at", models.CharField(blank=True, max_length=20, null=True)),
                ("finished_at", models.CharField(blank=True, max_length=20, null=True)),
                ("created_at", models.CharField(max_length=20)),
                ("updated_at", models.CharField(max_length=20)),
                (
                    "company",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="workspace_transfer_tasks",
                        to="company.company",
                    ),
                ),
                (
                    "file",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="transfer_tasks",
                        to="runtime.workspacefile",
                    ),
                ),
                (
                    "instance",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="workspace_transfer_tasks",
                        to="runtime.openclawinstance",
                    ),
                ),
            ],
            options={"db_table": "runtime_workspacetransfertask"},
        ),
        migrations.AddIndex(
            model_name="workspacetransfertask",
            index=models.Index(fields=["company", "direction", "status"], name="runtime_wor_company_4efeb6_idx"),
        ),
    ]
