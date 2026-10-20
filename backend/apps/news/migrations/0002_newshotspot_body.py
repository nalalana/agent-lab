from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('news', '0001_initial')]
    operations = [migrations.AddField(
        model_name='newshotspot', name='body', field=models.JSONField(default=list, blank=True),
    )]
