from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('agent_runtime', '0005_sandbox_execution'),
    ]

    operations = [
        migrations.CreateModel(
            name='AgentToolCall',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('model_call_id', models.CharField(max_length=255)),
                ('tool_name', models.CharField(max_length=128)),
                ('executor', models.CharField(max_length=32)),
                ('status', models.CharField(choices=[('running', 'running'), ('completed', 'completed'), ('failed', 'failed')], default='running', max_length=24)),
                ('arguments_redacted', models.JSONField(blank=True, default=dict)),
                ('arguments_hash', models.CharField(max_length=64)),
                ('result_ref', models.CharField(blank=True, default='', max_length=1024)),
                ('duration_ms', models.PositiveIntegerField(blank=True, null=True)),
                ('error_code', models.CharField(blank=True, default='', max_length=64)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='tool_calls', to='agent_runtime.agentrun')),
            ],
            options={
                'db_table': 'agent_tool_call',
            },
        ),
        migrations.AddConstraint(
            model_name='agenttoolcall',
            constraint=models.UniqueConstraint(fields=('run', 'model_call_id'), name='agent_tool_call_run_model_uniq'),
        ),
        migrations.AddIndex(
            model_name='agenttoolcall',
            index=models.Index(fields=['run', 'status'], name='agent_tool_call_run_status_idx'),
        ),
        migrations.AddIndex(
            model_name='agenttoolcall',
            index=models.Index(fields=['tool_name', 'created_at'], name='tool_call_tool_created_idx'),
        ),
    ]
