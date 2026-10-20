from django.db import migrations

from apps.company.default_skills_v1 import ensure_default_skills_v1


def seed_existing_transparent_companies(apps, schema_editor):
    transparent_company_model = apps.get_model("company", "TransparentCompany")
    skill_model = apps.get_model("company", "TransparentCompanySkill")
    database = schema_editor.connection.alias
    for transparent_company in (
        transparent_company_model.objects.using(database)
        .order_by("pk")
        .iterator(chunk_size=200)
    ):
        ensure_default_skills_v1(
            transparent_company,
            skill_model,
            using=database,
        )


class Migration(migrations.Migration):
    dependencies = [("company", "0040_scoped_admin_and_verified_super_admin")]

    operations = [
        migrations.RunPython(
            seed_existing_transparent_companies,
            migrations.RunPython.noop,
        ),
    ]
