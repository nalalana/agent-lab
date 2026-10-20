from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("company", "0013_companymodelpolicy_selectedcredentialname"),
    ]

    operations = [
        migrations.AddField(
            model_name="transparentcompanyagenttestsession",
            name="source",
            field=models.CharField(
                choices=[
                    ("agent_test", "agent_test"),
                    ("public_client_entry", "public_client_entry"),
                ],
                default="agent_test",
                max_length=32,
            ),
        ),
    ]
