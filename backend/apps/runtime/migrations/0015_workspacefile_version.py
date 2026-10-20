from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("runtime", "0014_workspacefile_knowledge_index_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="workspacefile",
            name="version",
            field=models.PositiveBigIntegerField(default=1),
        ),
    ]
