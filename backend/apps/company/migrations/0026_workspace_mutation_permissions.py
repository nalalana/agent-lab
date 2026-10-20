from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("company", "0025_remove_permission_tool_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="transparentcompanypermissiontemplate",
            name="knowledgeWrite",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="transparentcompanypermissiontemplate",
            name="knowledgeDelete",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="transparentcompanyagentpermissionbinding",
            name="knowledgeWrite",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="transparentcompanyagentpermissionbinding",
            name="knowledgeDelete",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
