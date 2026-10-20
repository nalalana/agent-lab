from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("user", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="terms_version",
            field=models.CharField(max_length=20, null=True),
        ),
        migrations.AddField(
            model_name="user",
            name="privacy_version",
            field=models.CharField(max_length=20, null=True),
        ),
        migrations.AddField(
            model_name="user",
            name="consented_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name="user",
            name="consent_ip",
            field=models.GenericIPAddressField(null=True),
        ),
    ]
