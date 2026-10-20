from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('company', '0016_public_client_entry_user_memory'),
    ]

    operations = [
        migrations.AddField(
            model_name='company',
            name='company_size',
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
        migrations.AddField(
            model_name='company',
            name='contact_phone',
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
        migrations.AddField(
            model_name='company',
            name='contact_email',
            field=models.EmailField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name='company',
            name='website',
            field=models.CharField(blank=True, max_length=200, null=True),
        ),
        migrations.AddField(
            model_name='company',
            name='address',
            field=models.CharField(blank=True, max_length=200, null=True),
        ),
    ]
