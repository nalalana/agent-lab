"""Add per-Agent OpenViking spaces and independent memory permissions.

This migration has two responsibilities:
1. Extend permission templates and Agent bindings with read/write/update/delete
   memory switches.
2. Create a stable OpenViking identity for every existing and future Agent.

The migration stores only logical OpenViking account/user/URI mappings. Actual
memory content remains in OpenViking and is accessed through Memory Gateway.
"""

import hashlib

from django.db import migrations, models
import django.db.models.deletion


def create_existing_agent_spaces(apps, schema_editor):
    """Backfill deterministic OpenViking spaces for Agents created before 0020.

    Django migration models are loaded through ``apps`` instead of importing the
    current application models. This keeps the migration reproducible even when
    model definitions change in later releases.
    """
    Agent = apps.get_model('company', 'TransparentCompanyAgentDefinition')
    VikingSpace = apps.get_model('company', 'TransparentCompanyAgentVikingSpace')
    for agent in Agent.objects.select_related('transparentCompany__company').iterator():
        cid = str(agent.transparentCompany.company.cid)

        # One company maps to one OpenViking account. Hashing avoids exposing
        # the original company ID to the memory service.
        account_id = f"tm-{hashlib.sha256(cid.encode('utf-8')).hexdigest()[:24]}"

        # One Agent maps to one OpenViking user. Combining company and Agent IDs
        # prevents equal Agent IDs in different companies from sharing memory.
        user_digest = hashlib.sha256(f"{cid}:{agent.agentId}".encode('utf-8')).hexdigest()[:24]
        user_id = f"agent-{user_digest}"

        # get_or_create makes the data migration safe to retry. The OneToOne
        # relation also guarantees that an Agent owns at most one Viking space.
        VikingSpace.objects.get_or_create(
            transparentCompany=agent.transparentCompany,
            agent=agent,
            defaults={
                'vikingSpaceId': f"{account_id}:{user_id}",
                'accountId': account_id,
                'userId': user_id,
                'rootUri': f"viking://user/{user_id}/memories/",
                'status': 'active',
                'lastError': '',
                'created_at': agent.created_at,
                'updated_at': agent.updated_at,
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ('company', '0019_openclawversioncatalog_and_more'),
    ]

    operations = [
        # Template-level memory switches provide reusable permission defaults.
        migrations.AddField(
            model_name='transparentcompanypermissiontemplate',
            name='memoryDelete',
            field=models.BooleanField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name='transparentcompanypermissiontemplate',
            name='memoryRead',
            field=models.BooleanField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name='transparentcompanypermissiontemplate',
            name='memoryUpdate',
            field=models.BooleanField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name='transparentcompanypermissiontemplate',
            name='memoryWrite',
            field=models.BooleanField(blank=True, default=None, null=True),
        ),
        # Binding-level switches allow each Agent to override template defaults.
        migrations.AddField(
            model_name='transparentcompanyagentpermissionbinding',
            name='memoryDelete',
            field=models.BooleanField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name='transparentcompanyagentpermissionbinding',
            name='memoryRead',
            field=models.BooleanField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name='transparentcompanyagentpermissionbinding',
            name='memoryUpdate',
            field=models.BooleanField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name='transparentcompanyagentpermissionbinding',
            name='memoryWrite',
            field=models.BooleanField(blank=True, default=None, null=True),
        ),
        # This table is the control-plane mapping between a Touming Agent and
        # its isolated OpenViking account/user namespace.
        migrations.CreateModel(
            name='TransparentCompanyAgentVikingSpace',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('vikingSpaceId', models.CharField(max_length=128, unique=True)),
                ('accountId', models.CharField(max_length=128)),
                ('userId', models.CharField(max_length=128)),
                ('rootUri', models.CharField(max_length=512)),
                (
                    'status',
                    models.CharField(
                        choices=[
                            ('active', 'active'),
                            ('archived', 'archived'),
                            ('failed', 'failed'),
                        ],
                        default='active',
                        max_length=20,
                    ),
                ),
                ('lastError', models.TextField(blank=True, default='')),
                ('created_at', models.CharField(max_length=20)),
                ('updated_at', models.CharField(max_length=20)),
                (
                    'agent',
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='vikingSpace',
                        to='company.transparentcompanyagentdefinition',
                    ),
                ),
                (
                    'transparentCompany',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='agentVikingSpaces',
                        to='company.transparentcompany',
                    ),
                ),
            ],
            options={
                'db_table': 'company_transparentcompanyagentvikingspace',
                'unique_together': {('transparentCompany', 'agent')},
            },
        ),
        # Existing Agents need the same one-Agent-one-space invariant as Agents
        # created after deployment. Reverse is intentionally a no-op because
        # rolling back the schema removes this table and its generated records.
        migrations.RunPython(create_existing_agent_spaces, migrations.RunPython.noop),
    ]
