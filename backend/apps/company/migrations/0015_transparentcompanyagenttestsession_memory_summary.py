from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("company", "0014_transparentcompanyagenttestsession_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="transparentcompanyagenttestsession",
            name="memorySummary",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="transparentcompanyagenttestsession",
            name="memorySummaryMessageCount",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="transparentcompanyagenttestsession",
            name="memorySummaryUpdatedAt",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
    ]
