from django.db import migrations, models
import django.db.models.deletion


def enable_unified_runtime_for_transparent_companies(apps, schema_editor):
    TransparentCompany = apps.get_model('company', 'TransparentCompany')
    RuntimePolicy = apps.get_model('agent_runtime', 'CompanyAgentRuntimePolicy')
    company_ids = TransparentCompany.objects.values_list('company_id', flat=True)
    RuntimePolicy.objects.filter(company_id__in=company_ids).update(
        mode='unified',
        canary_percentage=100,
    )


class Migration(migrations.Migration):
    dependencies = [
        ('agent_runtime', '0008_agentschedule'),
        ('company', '0021_transparent_company_prompt_version'),
    ]

    operations = [
        migrations.CreateModel(
            name='KnowledgeFolder',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('logical_path', models.CharField(max_length=512)),
                ('created_by', models.CharField(blank=True, default='', max_length=128)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('company', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='knowledge_folders', to='company.company')),
            ],
            options={
                'db_table': 'agent_runtime_knowledge_folder',
                'indexes': [models.Index(fields=['company', 'logical_path'], name='agent_runtime_kfolder_path_idx')],
                'constraints': [models.UniqueConstraint(fields=('company', 'logical_path'), name='agent_runtime_knowledge_folder_uniq')],
            },
        ),
        migrations.RunPython(
            enable_unified_runtime_for_transparent_companies,
            migrations.RunPython.noop,
        ),
    ]
