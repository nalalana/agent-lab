from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("company", "0009_company_members_and_system_settings"),
    ]

    operations = [
        migrations.AlterField(
            model_name="companysystemsetting",
            name="id",
            field=models.BigAutoField(primary_key=True, serialize=False),
        ),
    ]
