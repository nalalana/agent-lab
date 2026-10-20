from django.db import migrations, models
from django.db.models import Count


def remove_duplicate_bindings(apps, schema_editor):
    CompanyGate = apps.get_model('company', 'companyGate')
    Resource = apps.get_model('iam', 'Resource')
    duplicate_ids = (
        CompanyGate.objects.values('opengate_id')
        .annotate(binding_count=Count('id'))
        .filter(binding_count__gt=1)
        .values_list('opengate_id', flat=True)
    )
    for gate_id in duplicate_ids:
        bindings = list(
            CompanyGate.objects.filter(opengate_id=gate_id)
            .select_related('company', 'opengate')
            .order_by('id')
        )
        resource = Resource.objects.filter(
            type='project', external_id=bindings[0].opengate.gid,
        ).select_related('tenant').first()
        tenant_id = resource.tenant.external_id if resource and resource.tenant else ''
        keep = next(
            (binding for binding in bindings if tenant_id == f'organization:{binding.company.cid}'),
            bindings[0],
        )
        CompanyGate.objects.filter(opengate_id=gate_id).exclude(pk=keep.pk).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('company', '0042_company_service_suspension'),
        ('iam', '0003_backfill_unified_permissions'),
    ]

    operations = [
        migrations.RunPython(remove_duplicate_bindings, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='companygate',
            constraint=models.UniqueConstraint(fields=('opengate',), name='company_gate_single_company_project'),
        ),
    ]
