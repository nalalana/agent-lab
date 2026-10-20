from django.db import migrations, models
import django.db.models.deletion


def upgrade_verified_applicants(apps, schema_editor):
    Company = apps.get_model('company', 'Company')
    Grant = apps.get_model('iam', 'Grant')
    Principal = apps.get_model('iam', 'Principal')
    Resource = apps.get_model('iam', 'Resource')
    for company in Company.objects.filter(
        verification_status='verified',
        verified_verification__applicant_user__isnull=False,
    ).select_related('verified_verification__applicant_user').iterator():
        applicant = company.verified_verification.applicant_user
        if company.verification_provider == 'tencent_ess':
            principal, _ = Principal.objects.get_or_create(type='user', external_id=applicant.uid)
            resource, _ = Resource.objects.get_or_create(type='company', external_id=company.cid)
            Grant.objects.update_or_create(
                principal=principal, resource=resource,
                defaults={'permissions': 4, 'granter_id': applicant.id, 'expires_at': None, 'inherit': True},
            )
            continue
        Grant.objects.filter(
            principal__type='user', principal__external_id=applicant.uid,
            resource__type='company', resource__external_id=company.cid,
            permissions=3,
        ).update(permissions=4)


class Migration(migrations.Migration):
    dependencies = [('company', '0039_verification_applicant_identity_operation')]

    operations = [
        migrations.CreateModel(
            name='CompanyScopedAdmin',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('role_name', models.CharField(default='受限管理员', max_length=100)),
                ('file_rules', models.JSONField(blank=True, default=list)),
                ('agent_ids', models.JSONField(blank=True, default=list)),
                ('active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('company', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='scoped_admins', to='company.company')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='scoped_company_admins', to='user.user')),
                ('granted_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='user.user')),
            ],
            options={'db_table': 'company_scoped_admin'},
        ),
        migrations.AddConstraint(
            model_name='companyscopedadmin',
            constraint=models.UniqueConstraint(fields=('company', 'user'), name='company_scoped_admin_user_uniq'),
        ),
        migrations.RunPython(upgrade_verified_applicants, migrations.RunPython.noop),
    ]
