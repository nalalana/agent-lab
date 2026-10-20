from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("runtime", "0008_alter_runtimealertpolicy_status_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="runtimealertpolicy",
            name="status",
            field=models.CharField(
                choices=[("active", "鍚敤"), ("muted", "闈欓煶")],
                default="active",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="runtimerolloutpolicy",
            name="status",
            field=models.CharField(
                choices=[("active", "鍚敤"), ("paused", "鏆傚仠")],
                default="active",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="runtimerolloutpolicy",
            name="strategy",
            field=models.CharField(
                choices=[("direct", "鐩存帴鍙戝竷"), ("canary", "鐏板害鍙戝竷")],
                default="direct",
                max_length=32,
            ),
        ),
    ]
