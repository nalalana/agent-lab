from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('opengate', '0002_apikey_audit_fields')]

    operations = [
        migrations.AddField(
            model_name='gates',
            name='public_access',
            field=models.CharField(
                choices=[
                    ('internal', '仅内部可见'),
                    ('public_read', '外部可读不可写'),
                    ('public_write', '外部可读可写'),
                ],
                default='internal',
                max_length=16,
            ),
        ),
    ]
