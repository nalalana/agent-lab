"""Align AgentRun.source values with the frozen Phase 0 RunRequest contract."""

from django.db import migrations, models


FORWARD_SOURCE_MAP = {
    'test': 'agent_test',
    'public': 'public_client',
}
REVERSE_SOURCE_MAP = {value: key for key, value in FORWARD_SOURCE_MAP.items()}


def _rewrite_sources(apps, source_map):
    agent_run = apps.get_model('agent_runtime', 'AgentRun')
    for old_value, new_value in source_map.items():
        agent_run.objects.filter(source=old_value).update(source=new_value)


def align_agent_run_sources(apps, schema_editor):
    _rewrite_sources(apps, FORWARD_SOURCE_MAP)


def restore_legacy_agent_run_sources(apps, schema_editor):
    _rewrite_sources(apps, REVERSE_SOURCE_MAP)


class Migration(migrations.Migration):

    dependencies = [
        ('agent_runtime', '0003_memory_sync_job'),
    ]

    operations = [
        migrations.RunPython(
            align_agent_run_sources,
            restore_legacy_agent_run_sources,
        ),
        migrations.AlterField(
            model_name='agentrun',
            name='source',
            field=models.CharField(
                choices=[
                    ('agent_test', 'agent_test'),
                    ('public_client', 'public_client'),
                    ('cron', 'cron'),
                ],
                max_length=24,
            ),
        ),
    ]
