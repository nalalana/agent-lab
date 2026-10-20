from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('company', '0004_transparentcompanyagentdefinition_client_entry_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='transparentcompanyskill',
            name='sourceType',
            field=models.CharField(
                choices=[('repo', 'repo'), ('manual', 'manual'), ('system', 'system')],
                default='repo',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='transparentcompanyskill',
            name='runtimeStatus',
            field=models.CharField(
                choices=[('pending', 'pending'), ('active', 'active'), ('error', 'error')],
                default='pending',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='transparentcompanyskill',
            name='isSystemSkill',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='transparentcompanyskill',
            name='lastPublishedAt',
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
    ]
