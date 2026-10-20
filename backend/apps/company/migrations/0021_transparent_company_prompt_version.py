"""Version existing company prompts without removing the legacy source field."""

import hashlib

from django.db import migrations, models
import django.db.models.deletion


def backfill_active_prompt_versions(apps, schema_editor):
    TransparentCompany = apps.get_model('company', 'TransparentCompany')
    PromptVersion = apps.get_model('company', 'TransparentCompanyPromptVersion')
    versions = []
    for company in TransparentCompany.objects.only('id', 'prompts').iterator():
        content = company.prompts or ''
        versions.append(
            PromptVersion(
                company_id=company.id,
                version=1,
                content=content,
                checksum=hashlib.sha256(content.encode('utf-8')).hexdigest(),
                status='active',
            )
        )
    PromptVersion.objects.bulk_create(versions, ignore_conflicts=True, batch_size=500)


def remove_backfilled_prompt_versions(apps, schema_editor):
    PromptVersion = apps.get_model('company', 'TransparentCompanyPromptVersion')
    PromptVersion.objects.filter(version=1, status='active', created_by_id=None).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('company', '0020_agent_viking_memory_space'),
    ]

    operations = [
        migrations.CreateModel(
            name='TransparentCompanyPromptVersion',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('version', models.PositiveIntegerField()),
                ('content', models.TextField(blank=True, default='')),
                ('checksum', models.CharField(max_length=64)),
                (
                    'status',
                    models.CharField(
                        choices=[('draft', 'draft'), ('active', 'active'), ('archived', 'archived')],
                        default='draft',
                        max_length=16,
                    ),
                ),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                (
                    'company',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='prompt_versions',
                        to='company.transparentcompany',
                    ),
                ),
                (
                    'created_by',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='transparent_company_prompt_versions',
                        to='user.user',
                    ),
                ),
            ],
            options={
                'db_table': 'company_transparentcompanypromptversion',
                'ordering': ['-version'],
            },
        ),
        migrations.AddConstraint(
            model_name='transparentcompanypromptversion',
            constraint=models.UniqueConstraint(
                fields=('company', 'version'), name='cmp_company_prompt_version_uniq'
            ),
        ),
        migrations.AddConstraint(
            model_name='transparentcompanypromptversion',
            constraint=models.UniqueConstraint(
                condition=models.Q(('status', 'active')),
                fields=('company',),
                name='cmp_company_prompt_active_uniq',
            ),
        ),
        migrations.RunPython(backfill_active_prompt_versions, remove_backfilled_prompt_versions),
    ]
