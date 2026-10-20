from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("company", "0036_ev6_runtime_safeguards"),
    ]

    operations = [
        migrations.AddField(
            model_name="companyverification",
            name="applicant_role",
            field=models.CharField(
                blank=True,
                choices=[
                    ("legal_representative", "legal_representative"),
                    ("authorized_person", "authorized_person"),
                ],
                default="",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="companyverification",
            name="stage",
            field=models.CharField(
                choices=[
                    ("legacy_ess", "legacy_ess"),
                    ("business_license_upload", "business_license_upload"),
                    ("company_check", "company_check"),
                    ("applicant_face", "applicant_face"),
                    ("authorization_upload", "authorization_upload"),
                    ("document_check", "document_check"),
                    ("ready_to_submit", "ready_to_submit"),
                    ("manual_review", "manual_review"),
                    ("supplement_required", "supplement_required"),
                    ("completed", "completed"),
                ],
                default="legacy_ess",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="verificationdocument",
            name="document_type",
            field=models.CharField(
                choices=[
                    ("authorization", "authorization"),
                    ("business_license", "business_license"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="verificationcheck",
            name="kind",
            field=models.CharField(
                choices=[
                    ("registry", "registry"),
                    ("face", "face"),
                    ("business_license", "business_license"),
                    ("business_license_security", "business_license_security"),
                    ("document_text", "document_text"),
                    ("seal", "seal"),
                    ("consistency", "consistency"),
                    ("document_security", "document_security"),
                ],
                max_length=32,
            ),
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
