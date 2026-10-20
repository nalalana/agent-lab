from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("company", "0038_verification_registration_identity_deferred"),
    ]

    operations = [
        migrations.AlterField(
            model_name="verificationoperation",
            name="operation_kind",
            field=models.CharField(
                choices=[
                    ("start", "start"),
                    ("cancel", "cancel"),
                    ("expire", "expire"),
                    ("transition", "transition"),
                    ("applicant_identity", "applicant_identity"),
                    ("registry_check", "registry_check"),
                    ("business_license_ocr", "business_license_ocr"),
                    ("face_start", "face_start"),
                    ("face_query", "face_query"),
                    ("document_upload", "document_upload"),
                    ("document_scan", "document_scan"),
                    ("document_text_ocr", "document_text_ocr"),
                    ("seal_ocr", "seal_ocr"),
                    ("document_cleanup", "document_cleanup"),
                    ("submit", "submit"),
                    ("transcription", "transcription"),
                    ("review_decision", "review_decision"),
                    ("review_evidence_refresh", "review_evidence_refresh"),
                    ("suspend_verification", "suspend_verification"),
                ],
                max_length=32,
            ),
        ),
    ]
