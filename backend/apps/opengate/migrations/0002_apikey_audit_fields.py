from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('opengate', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='apikey',
            name='last_used_at',
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
        migrations.AddField(
            model_name='apikey',
            name='last_used_ip',
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name='apikey',
            name='last_used_path',
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name='apikey',
            name='usage_count',
            field=models.PositiveIntegerField(default=0),
        ),
    ]
