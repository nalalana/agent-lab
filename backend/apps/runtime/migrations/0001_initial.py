from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='OpenClawInstance',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('provider', models.CharField(choices=[('tencent_lighthouse', '腾讯云 Lighthouse')], default='tencent_lighthouse', max_length=64)),
                ('region', models.CharField(blank=True, max_length=64, null=True)),
                ('zone', models.CharField(blank=True, max_length=64, null=True)),
                ('instance_id', models.CharField(blank=True, max_length=128, null=True)),
                ('instance_name', models.CharField(blank=True, max_length=255, null=True)),
                ('public_ip', models.CharField(blank=True, max_length=64, null=True)),
                ('private_ip', models.CharField(blank=True, max_length=64, null=True)),
                ('status', models.CharField(choices=[('pending', '待创建'), ('provisioning', '创建中'), ('provision_failed', '创建失败'), ('bootstrapping', '部署中'), ('bootstrap_failed', '部署失败'), ('running', '运行中'), ('degraded', '降级运行'), ('suspended', '已暂停'), ('destroyed', '已销毁')], default='pending', max_length=32)),
                ('openclaw_status', models.CharField(choices=[('unknown', '未知'), ('starting', '启动中'), ('healthy', '健康'), ('unhealthy', '异常'), ('stopped', '已停止')], default='unknown', max_length=32)),
                ('deploy_version', models.CharField(blank=True, max_length=64, null=True)),
                ('current_publish_version', models.CharField(blank=True, max_length=64, null=True)),
                ('create_source', models.CharField(choices=[('platform', '平台创建'), ('manual', '人工创建')], default='platform', max_length=32)),
                ('last_health_check_at', models.CharField(blank=True, max_length=32, null=True)),
                ('last_error', models.TextField(blank=True, null=True)),
                ('created_at', models.CharField(max_length=20)),
                ('updated_at', models.CharField(max_length=20)),
                ('company', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='runtime_instances', to='company.company')),
            ],
            options={'db_table': 'runtime_openclawinstance'},
        ),
        migrations.CreateModel(
            name='OpenClawAuditLog',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('actor_type', models.CharField(choices=[('system', '系统'), ('user', '用户'), ('admin', '管理员')], default='system', max_length=32)),
                ('actor_id', models.CharField(blank=True, max_length=128, null=True)),
                ('action', models.CharField(max_length=128)),
                ('target_type', models.CharField(choices=[('instance', '实例'), ('task', '任务'), ('config', '配置'), ('agent', 'Agent'), ('skill', 'Skill')], max_length=32)),
                ('target_id', models.CharField(blank=True, max_length=128, null=True)),
                ('detail', models.TextField(blank=True, null=True)),
                ('created_at', models.CharField(max_length=20)),
                ('company', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='runtime_audit_logs', to='company.company')),
                ('instance', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='audit_logs', to='runtime.openclawinstance')),
            ],
            options={'db_table': 'runtime_openclawauditlog'},
        ),
        migrations.CreateModel(
            name='OpenClawProvisionTask',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('task_type', models.CharField(choices=[('create_instance', '创建实例'), ('bootstrap_openclaw', '部署 OpenClaw'), ('health_check', '健康检查'), ('retry', '重试任务')], max_length=64)),
                ('status', models.CharField(choices=[('pending', '待执行'), ('running', '执行中'), ('succeeded', '成功'), ('failed', '失败'), ('cancelled', '已取消')], default='pending', max_length=32)),
                ('step', models.CharField(blank=True, max_length=128, null=True)),
                ('retry_count', models.IntegerField(default=0)),
                ('request_payload', models.TextField(blank=True, null=True)),
                ('result_payload', models.TextField(blank=True, null=True)),
                ('error_message', models.TextField(blank=True, null=True)),
                ('started_at', models.CharField(blank=True, max_length=20, null=True)),
                ('finished_at', models.CharField(blank=True, max_length=20, null=True)),
                ('created_at', models.CharField(max_length=20)),
                ('updated_at', models.CharField(max_length=20)),
                ('company', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='runtime_provision_tasks', to='company.company')),
                ('instance', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='tasks', to='runtime.openclawinstance')),
            ],
            options={'db_table': 'runtime_openclawprovisiontask'},
        ),
        migrations.AddIndex(
            model_name='openclawinstance',
            index=models.Index(fields=['company', 'status'], name='runtime_open_company_2aaa10_idx'),
        ),
        migrations.AddIndex(
            model_name='openclawinstance',
            index=models.Index(fields=['instance_id'], name='runtime_open_instanc_4c52b7_idx'),
        ),
        migrations.AddIndex(
            model_name='openclawprovisiontask',
            index=models.Index(fields=['company', 'task_type', 'status'], name='runtime_open_company_f99730_idx'),
        ),
        migrations.AddIndex(
            model_name='openclawauditlog',
            index=models.Index(fields=['company', 'action'], name='runtime_open_company_1f148d_idx'),
        ),
        migrations.AddIndex(
            model_name='openclawauditlog',
            index=models.Index(fields=['target_type', 'target_id'], name='runtime_open_target__5f91e8_idx'),
        ),
    ]
