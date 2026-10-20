# Generated for Phase 2-B OpenViking MemoryGateway.

from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('user', '0001_initial'),
        ('agent_runtime', '0002_backfill_runtime_policy'),
    ]

    operations = [
        migrations.CreateModel(
            name='MemorySyncJob',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('job_type', models.CharField(choices=[('knowledge_index', 'knowledge_index'), ('memory_commit', 'memory_commit'), ('delete', 'delete')], max_length=32)),
                ('idempotency_key', models.CharField(max_length=255, unique=True)),
                ('source_ref', models.CharField(max_length=1024)),
                ('status', models.CharField(choices=[('pending', 'pending'), ('running', 'running'), ('completed', 'completed'), ('failed', 'failed')], default='pending', max_length=24)),
                ('attempts', models.PositiveSmallIntegerField(default=0)),
                ('next_retry_at', models.DateTimeField(blank=True, null=True)),
                ('error_code', models.CharField(blank=True, default='', max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('agent', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='memory_sync_jobs', to='company.transparentcompanyagentdefinition')),
                ('company', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='memory_sync_jobs', to='company.company')),
                ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='memory_sync_jobs', to='user.user')),
            ],
            options={
                'db_table': 'agent_runtime_memory_sync_job',
            },
        ),
        migrations.AddIndex(
            model_name='memorysyncjob',
            index=models.Index(fields=['status', 'next_retry_at'], name='memory_job_status_retry_idx'),
        ),
        migrations.AddIndex(
            model_name='memorysyncjob',
            index=models.Index(fields=['company', 'created_at'], name='memory_job_company_created_idx'),
        ),
    ]
