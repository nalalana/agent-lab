from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("company", "0012_transparent_agent_test_sessions"),
    ]

    operations = [
        migrations.AddField(
            model_name="companymodelpolicy",
            name="selectedCredentialName",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
    ]
