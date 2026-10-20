from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("company", "0010_alter_companysystemsetting_id"),
    ]

    operations = [
        migrations.AlterField(
            model_name="companysystemsetting",
            name="id",
            field=models.BigAutoField(
                auto_created=True,
                primary_key=True,
                serialize=False,
                verbose_name="ID",
            ),
        ),
    ]
