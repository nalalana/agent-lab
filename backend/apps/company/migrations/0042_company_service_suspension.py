from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('company', '0041_seed_default_transparent_company_skills'), ('company', '0040_seed_default_transparent_company_skills')]

    operations = [
        migrations.AddField(
            model_name='company',
            name='service_suspended_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='company',
            name='service_suspension_reason',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddConstraint(
            model_name='company',
            constraint=models.CheckConstraint(
                check=models.Q(
                    models.Q(
                        ('service_suspended_at__isnull', True),
                        ('service_suspension_reason', ''),
                    ),
                    models.Q(
                        ('service_suspended_at__isnull', False),
                        models.Q(('service_suspension_reason', ''), _negated=True),
                    ),
                    _connector='OR',
                ),
                name='company_service_suspension_pair_chk',
            ),
        ),
    ]
