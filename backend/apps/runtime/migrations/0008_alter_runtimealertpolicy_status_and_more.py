from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("runtime", "0007_rename_runtime_dri_company_047aa7_idx_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="runtimealertpolicy",
            name="status",
            field=models.CharField(
                choices=[("active", "active"), ("muted", "muted")],
                default="active",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="runtimerolloutpolicy",
            name="status",
            field=models.CharField(
                choices=[("active", "active"), ("paused", "paused")],
                default="active",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="runtimerolloutpolicy",
            name="strategy",
            field=models.CharField(
                choices=[("direct", "direct"), ("canary", "canary")],
                default="direct",
                max_length=32,
            ),
        ),
    ]
