from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('company', '0006_model_provider_package_policy_and_credentials'),
    ]

    operations = [
        migrations.CreateModel(
            name='CompanySessionMemoryPolicy',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('mode', models.CharField(choices=[('session', 'session'), ('disabled', 'disabled')], default='session', max_length=20)),
                ('status', models.CharField(choices=[('active', 'active'), ('inactive', 'inactive')], default='active', max_length=20)),
                ('enabled', models.BooleanField(default=True)),
                ('recentMessageWindow', models.IntegerField(default=12)),
                ('maxTokens', models.IntegerField(default=4000)),
                ('summaryTriggerTokens', models.IntegerField(default=2800)),
                ('summaryMaxTokens', models.IntegerField(default=800)),
                ('summaryStyle', models.CharField(choices=[('concise', 'concise'), ('detailed', 'detailed')], default='concise', max_length=20)),
                ('sessionTtlDays', models.IntegerField(default=30)),
                ('redactPii', models.BooleanField(default=False)),
                ('created_at', models.CharField(max_length=20)),
                ('updated_at', models.CharField(max_length=20)),
                ('company', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='sessionMemoryPolicy', to='company.company')),
            ],
            options={
                'db_table': 'company_companysessionmemorypolicy',
            },
        ),
        migrations.CreateModel(
            name='TransparentCompanyAgentMemoryBinding',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('memoryScope', models.CharField(choices=[('session', 'session'), ('disabled', 'disabled')], default='session', max_length=20)),
                ('enabled', models.BooleanField(default=False)),
                ('inheritCompanyPolicy', models.BooleanField(default=True)),
                ('maxTokensOverride', models.IntegerField(blank=True, null=True)),
                ('summaryStyleOverride', models.CharField(blank=True, choices=[('concise', 'concise'), ('detailed', 'detailed')], max_length=20, null=True)),
                ('note', models.CharField(blank=True, max_length=255, null=True)),
                ('status', models.CharField(choices=[('active', 'active'), ('inactive', 'inactive')], default='inactive', max_length=20)),
                ('created_at', models.CharField(max_length=20)),
                ('updated_at', models.CharField(max_length=20)),
                ('agent', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='memoryBindings', to='company.transparentcompanyagentdefinition')),
                ('transparentCompany', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='agentMemoryBindings', to='company.transparentcompany')),
            ],
            options={
                'db_table': 'company_transparentcompanyagentmemorybinding',
                'unique_together': {('transparentCompany', 'agent')},
            },
        ),
    ]
