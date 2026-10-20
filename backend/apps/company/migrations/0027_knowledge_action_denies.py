from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("company", "0026_workspace_mutation_permissions"),
    ]

    operations = [
        migrations.AddField(
            model_name="transparentcompanypermissiontemplate",
            name="knowledgeReadDeny",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="transparentcompanypermissiontemplate",
            name="knowledgeWriteDeny",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="transparentcompanypermissiontemplate",
            name="knowledgeDeleteDeny",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="transparentcompanyagentpermissionbinding",
            name="knowledgeReadDeny",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="transparentcompanyagentpermissionbinding",
            name="knowledgeWriteDeny",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="transparentcompanyagentpermissionbinding",
            name="knowledgeDeleteDeny",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
