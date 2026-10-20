from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('company', '0005_transparentcompanyskill_governance_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='ModelPackage',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('packageId', models.CharField(max_length=100, unique=True)),
                ('name', models.CharField(max_length=255)),
                ('description', models.TextField(blank=True, null=True)),
                ('status', models.CharField(choices=[('active', 'active'), ('inactive', 'inactive')], default='active', max_length=20)),
                ('monthlyTokenQuota', models.BigIntegerField(default=0)),
                ('monthlyRequestQuota', models.BigIntegerField(default=0)),
                ('priority', models.IntegerField(default=100)),
                ('isDefault', models.BooleanField(default=False)),
                ('created_at', models.CharField(max_length=20)),
                ('updated_at', models.CharField(max_length=20)),
            ],
            options={'db_table': 'company_modelpackage'},
        ),
        migrations.CreateModel(
            name='ModelProvider',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('providerId', models.CharField(max_length=100, unique=True)),
                ('name', models.CharField(max_length=255)),
                ('slug', models.CharField(max_length=100, unique=True)),
                ('baseUrl', models.CharField(blank=True, max_length=512, null=True)),
                ('authMode', models.CharField(choices=[('api_key', 'api_key'), ('company_key', 'company_key'), ('platform_key', 'platform_key')], default='platform_key', max_length=30)),
                ('status', models.CharField(choices=[('active', 'active'), ('inactive', 'inactive')], default='active', max_length=20)),
                ('isPlatformManaged', models.BooleanField(default=True)),
                ('metadata', models.TextField(blank=True, null=True)),
                ('created_at', models.CharField(max_length=20)),
                ('updated_at', models.CharField(max_length=20)),
            ],
            options={'db_table': 'company_modelprovider'},
        ),
        migrations.CreateModel(
            name='ModelDefinition',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('modelId', models.CharField(max_length=100)),
                ('displayName', models.CharField(max_length=255)),
                ('category', models.CharField(default='chat', max_length=50)),
                ('status', models.CharField(choices=[('active', 'active'), ('inactive', 'inactive')], default='active', max_length=20)),
                ('supportsTools', models.BooleanField(default=False)),
                ('contextWindow', models.IntegerField(default=0)),
                ('inputPricePer1k', models.FloatField(default=0)),
                ('outputPricePer1k', models.FloatField(default=0)),
                ('isPlatformDefault', models.BooleanField(default=False)),
                ('metadata', models.TextField(blank=True, null=True)),
                ('created_at', models.CharField(max_length=20)),
                ('updated_at', models.CharField(max_length=20)),
                ('provider', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='models', to='company.modelprovider')),
            ],
            options={
                'db_table': 'company_modeldefinition',
                'unique_together': {('provider', 'modelId')},
            },
        ),
        migrations.CreateModel(
            name='CompanyModelPolicy',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('sourceMode', models.CharField(choices=[('platform', 'platform'), ('byok', 'byok'), ('hybrid', 'hybrid')], default='platform', max_length=20)),
                ('status', models.CharField(choices=[('active', 'active'), ('inactive', 'inactive')], default='active', max_length=20)),
                ('rateLimitRpm', models.IntegerField(default=60)),
                ('rateLimitTpm', models.IntegerField(default=120000)),
                ('fallbackToPlatform', models.BooleanField(default=True)),
                ('created_at', models.CharField(max_length=20)),
                ('updated_at', models.CharField(max_length=20)),
                ('company', models.OneToOneField(on_delete=models.deletion.CASCADE, related_name='modelPolicy', to='company.company')),
                ('defaultModel', models.ForeignKey(blank=True, null=True, on_delete=models.deletion.SET_NULL, to='company.modeldefinition')),
                ('package', models.ForeignKey(blank=True, null=True, on_delete=models.deletion.SET_NULL, to='company.modelpackage')),
                ('provider', models.ForeignKey(blank=True, null=True, on_delete=models.deletion.SET_NULL, to='company.modelprovider')),
            ],
            options={'db_table': 'company_companymodelpolicy'},
        ),
        migrations.CreateModel(
            name='CompanyApiCredential',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('credentialName', models.CharField(max_length=255)),
                ('keyMasked', models.CharField(blank=True, max_length=255, null=True)),
                ('secretEncrypted', models.TextField(blank=True, null=True)),
                ('status', models.CharField(choices=[('active', 'active'), ('inactive', 'inactive')], default='inactive', max_length=20)),
                ('lastVerifiedAt', models.CharField(blank=True, max_length=20, null=True)),
                ('created_at', models.CharField(max_length=20)),
                ('updated_at', models.CharField(max_length=20)),
                ('company', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='apiCredentials', to='company.company')),
                ('provider', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='companyCredentials', to='company.modelprovider')),
            ],
            options={'db_table': 'company_companyapicredential'},
        ),
    ]
