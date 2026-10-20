from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("company", "0037_verification_registration_routes"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="companyverification",
            name="company_verification_manual_snapshot_chk",
        ),
        migrations.AddConstraint(
            model_name="companyverification",
            constraint=models.CheckConstraint(
                check=(
                    ~models.Q(provider="tencent_manual_v1")
                    | (
                        ~models.Q(company_name_snapshot="")
                        & ~models.Q(registration_no_snapshot="")
                        & (
                            (
                                ~models.Q(applicant_name_snapshot="")
                                & ~models.Q(applicant_id_encrypted="")
                                & ~models.Q(applicant_id_hmac="")
                            )
                            | (
                                models.Q(applicant_role__in=(
                                    "legal_representative",
                                    "authorized_person",
                                ))
                                & models.Q(applicant_name_snapshot="")
                                & models.Q(applicant_id_encrypted="")
                                & models.Q(applicant_id_hmac="")
                            )
                        )
                        & ~models.Q(consent_version="")
                        & models.Q(consented_at__isnull=False)
                        & models.Q(expires_at__isnull=False)
                    )
                ),
                name="company_verification_manual_snapshot_chk",
            ),
        ),
    ]
