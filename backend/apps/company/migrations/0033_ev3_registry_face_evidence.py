# Generated manually for EV-3 registry and FaceID evidence binding.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("company", "0032_ev2_manual_verification_domain")]

    operations = [
        migrations.AddField(
            model_name="verificationfacesession",
            name="query_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="verificationoperation",
            name="application_revision",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="verificationoperation",
            name="face_attempt_no",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="verificationoperation",
            name="operation_kind",
            field=models.CharField(
                choices=[
                    ("start", "start"),
                    ("cancel", "cancel"),
                    ("expire", "expire"),
                    ("transition", "transition"),
                    ("registry_check", "registry_check"),
                    ("face_start", "face_start"),
                    ("face_query", "face_query"),
                ],
                max_length=32,
            ),
        ),
    ]
