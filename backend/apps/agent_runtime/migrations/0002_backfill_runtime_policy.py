"""Put every existing company on the legacy engine before rollout begins."""

from django.db import migrations


def create_legacy_runtime_policies(apps, schema_editor):
    Company = apps.get_model('company', 'Company')
    RuntimePolicy = apps.get_model('agent_runtime', 'CompanyAgentRuntimePolicy')
    policies = (
        RuntimePolicy(company_id=company.id, mode='legacy')
        for company in Company.objects.only('id').iterator()
    )
    RuntimePolicy.objects.bulk_create(policies, ignore_conflicts=True, batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ('agent_runtime', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(create_legacy_runtime_policies, migrations.RunPython.noop),
    ]
