import hashlib

from django.db import migrations, models
import django.db.models.deletion


def backfill_pending_bindings(apps, schema_editor):
    Cluster = apps.get_model('company', 'OpenVikingCluster')
    CompanyBinding = apps.get_model('company', 'CompanyVikingBinding')
    TransparentCompany = apps.get_model('company', 'TransparentCompany')
    VikingSpace = apps.get_model('company', 'TransparentCompanyAgentVikingSpace')

    cluster, _ = Cluster.objects.get_or_create(
        name='TSDS-OpenViking',
        defaults={
            'endpoint': 'http://127.0.0.1:11933',
            'status': 'active',
            'capacity': 1000,
            'rootCredentialRef': 'file:root.key',
            'isDefault': True,
        },
    )
    for transparent in TransparentCompany.objects.select_related('company').iterator():
        cid = str(transparent.company.cid)
        account_id = f"tm-{hashlib.sha256(cid.encode()).hexdigest()[:24]}"
        binding, _ = CompanyBinding.objects.get_or_create(
            company=transparent.company,
            defaults={
                'cluster': cluster,
                'accountId': account_id,
                'adminUserId': 'touming-admin',
                'runtimeUserId': 'touming-runtime',
                'status': 'pending',
            },
        )
        VikingSpace.objects.filter(transparentCompany=transparent).update(
            companyBinding=binding,
            credentialRef='',
            status='pending',
            lastError='',
        )


class Migration(migrations.Migration):
    dependencies = [('company', '0023_platform_qwen_only')]

    operations = [
        migrations.CreateModel(
            name='OpenVikingCluster',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=128, unique=True)),
                ('endpoint', models.CharField(max_length=512)),
                ('status', models.CharField(choices=[('active', 'active'), ('degraded', 'degraded'), ('disabled', 'disabled')], default='active', max_length=20)),
                ('capacity', models.PositiveIntegerField(default=1000)),
                ('rootCredentialRef', models.CharField(max_length=512)),
                ('isDefault', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'db_table': 'company_openvikingcluster'},
        ),
        migrations.CreateModel(
            name='CompanyVikingBinding',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('accountId', models.CharField(max_length=128)),
                ('adminUserId', models.CharField(max_length=128)),
                ('runtimeUserId', models.CharField(max_length=128)),
                ('adminCredentialRef', models.CharField(blank=True, default='', max_length=512)),
                ('runtimeCredentialRef', models.CharField(blank=True, default='', max_length=512)),
                ('status', models.CharField(choices=[('pending', 'pending'), ('provisioning', 'provisioning'), ('active', 'active'), ('failed', 'failed'), ('archived', 'archived')], default='pending', max_length=20)),
                ('storageQuotaBytes', models.PositiveBigIntegerField(default=10737418240)),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('nextRetryAt', models.DateTimeField(blank=True, null=True)),
                ('provisionedAt', models.DateTimeField(blank=True, null=True)),
                ('lastError', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('cluster', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='companyBindings', to='company.openvikingcluster')),
                ('company', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='vikingBinding', to='company.company')),
            ],
            options={'db_table': 'company_companyvikingbinding'},
        ),
        migrations.AddConstraint(
            model_name='companyvikingbinding',
            constraint=models.UniqueConstraint(fields=('cluster', 'accountId'), name='company_viking_cluster_account_unique'),
        ),
        migrations.AddField(
            model_name='transparentcompanyagentvikingspace',
            name='companyBinding',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='agentSpaces', to='company.companyvikingbinding'),
        ),
        migrations.AddField(
            model_name='transparentcompanyagentvikingspace',
            name='credentialRef',
            field=models.CharField(blank=True, default='', max_length=512),
        ),
        migrations.AlterField(
            model_name='transparentcompanyagentvikingspace',
            name='status',
            field=models.CharField(choices=[('pending', 'pending'), ('provisioning', 'provisioning'), ('active', 'active'), ('archived', 'archived'), ('failed', 'failed')], default='pending', max_length=20),
        ),
        migrations.RunPython(backfill_pending_bindings, migrations.RunPython.noop),
    ]
