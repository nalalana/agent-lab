from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('agent_runtime', '0009_knowledgefolder'),
    ]

    operations = [
        migrations.AddField(
            model_name='memorysyncjob',
            name='external_archive_uri',
            field=models.CharField(blank=True, default='', max_length=1024),
        ),
        migrations.AddField(
            model_name='memorysyncjob',
            name='external_session_id',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='memorysyncjob',
            name='external_task_id',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
    ]
