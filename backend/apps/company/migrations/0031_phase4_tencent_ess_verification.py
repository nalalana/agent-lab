from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('company', '0030_alter_companymember_unique_together_and_more'),
        ('user', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='companyverification',
            name='application_id',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='companyverification',
            name='last_refreshed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='companyverification',
            name='provider_status',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.CreateModel(
            name='TencentEssEvent',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('application_id', models.CharField(max_length=64)),
                ('msg_id', models.CharField(max_length=255)),
                ('msg_type', models.CharField(max_length=128)),
                ('msg_version', models.CharField(blank=True, default='', max_length=64)),
                ('proxy_organization_open_id', models.CharField(blank=True, default='', max_length=255)),
                ('proxy_operator_open_id', models.CharField(blank=True, max_length=255, null=True)),
                ('authorization_id', models.CharField(blank=True, max_length=255, null=True)),
                ('auth_success', models.BooleanField(blank=True, null=True)),
                ('payload_digest', models.CharField(max_length=64)),
                ('process_status', models.CharField(choices=[('received', 'received'), ('processed', 'processed'), ('ignored', 'ignored'), ('failed', 'failed')], default='received', max_length=20)),
                ('failure_reason', models.CharField(blank=True, default='', max_length=255)),
                ('received_at', models.DateTimeField(auto_now_add=True)),
                ('processed_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'db_table': 'company_tencentess_event',
            },
        ),
        migrations.CreateModel(
            name='TencentEssOperatorIdentity',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('application_id', models.CharField(max_length=64)),
                ('proxy_operator_open_id', models.CharField(max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='tencent_ess_operator_identities', to='user.user')),
            ],
            options={
                'db_table': 'company_tencentessoperatoridentity',
            },
        ),
        migrations.AddConstraint(
            model_name='tencentessoperatoridentity',
            constraint=models.UniqueConstraint(fields=('user', 'application_id'), name='tencent_ess_operator_user_app_uniq'),
        ),
        migrations.AddConstraint(
            model_name='tencentessoperatoridentity',
            constraint=models.UniqueConstraint(fields=('application_id', 'proxy_operator_open_id'), name='tencent_ess_operator_open_id_app_uniq'),
        ),
        migrations.AddConstraint(
            model_name='tencentessevent',
            constraint=models.UniqueConstraint(fields=('application_id', 'msg_id'), name='tencent_ess_event_app_msg_uniq'),
        ),
    ]
