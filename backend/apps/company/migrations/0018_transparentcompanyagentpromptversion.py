from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('company', '0017_company_profile_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='TransparentCompanyAgentPromptVersion',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('versionLabel', models.CharField(max_length=32)),
                ('content', models.TextField(blank=True, default='')),
                ('changeReason', models.TextField(blank=True, default='')),
                ('changeType', models.CharField(choices=[('feature', 'feature'), ('fix', 'fix'), ('optimization', 'optimization'), ('security', 'security'), ('initial', 'initial')], default='feature', max_length=20)),
                ('createdBy', models.CharField(blank=True, default='', max_length=255)),
                ('approvedBy', models.CharField(blank=True, default='', max_length=255)),
                ('approvedAt', models.CharField(blank=True, default='', max_length=50)),
                ('isCurrent', models.BooleanField(default=True)),
                ('diffSummary', models.JSONField(blank=True, default=dict)),
                ('created_at', models.CharField(max_length=20)),
                ('agent', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='promptVersions', to='company.transparentcompanyagentdefinition')),
            ],
            options={
                'db_table': 'company_transparentcompanyagentpromptversion',
                'ordering': ['-id'],
            },
        ),
        migrations.AddIndex(
            model_name='transparentcompanyagentpromptversion',
            index=models.Index(fields=['agent', '-id'], name='cmp_agent_prompt_idx'),
        ),
        migrations.AddIndex(
            model_name='transparentcompanyagentpromptversion',
            index=models.Index(fields=['agent', 'isCurrent'], name='cmp_agent_prompt_cur_idx'),
        ),
    ]
