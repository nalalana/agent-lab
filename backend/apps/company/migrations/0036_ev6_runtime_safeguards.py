from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('company', '0035_ev5_verification_reviews'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='companyverificationreviewer',
            name='verification_reviewer_enabled_capability_chk',
        ),
        migrations.AddField(
            model_name='companyverificationreviewer',
            name='can_suspend_verification',
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name='verificationoperation',
            name='operation_kind',
            field=models.CharField(choices=[('start', 'start'), ('cancel', 'cancel'), ('expire', 'expire'), ('transition', 'transition'), ('registry_check', 'registry_check'), ('face_start', 'face_start'), ('face_query', 'face_query'), ('document_upload', 'document_upload'), ('document_scan', 'document_scan'), ('document_text_ocr', 'document_text_ocr'), ('seal_ocr', 'seal_ocr'), ('document_cleanup', 'document_cleanup'), ('submit', 'submit'), ('transcription', 'transcription'), ('review_decision', 'review_decision'), ('review_evidence_refresh', 'review_evidence_refresh'), ('suspend_verification', 'suspend_verification')], max_length=32),
        ),
        migrations.AddConstraint(
            model_name='companyverificationreviewer',
            constraint=models.CheckConstraint(check=models.Q(('enabled', False), ('can_pre_review', True), ('can_final_review', True), ('can_suspend_verification', True), _connector='OR'), name='verification_reviewer_enabled_capability_chk'),
        ),
    ]
