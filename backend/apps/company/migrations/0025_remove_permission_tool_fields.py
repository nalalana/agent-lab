from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("company", "0024_openviking_native_multitenancy"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="transparentcompanypermissiontemplate",
            name="toolsAllow",
        ),
        migrations.RemoveField(
            model_name="transparentcompanypermissiontemplate",
            name="toolsDeny",
        ),
        migrations.RemoveField(
            model_name="transparentcompanyagentpermissionbinding",
            name="toolsAllow",
        ),
        migrations.RemoveField(
            model_name="transparentcompanyagentpermissionbinding",
            name="toolsDeny",
        ),
    ]
