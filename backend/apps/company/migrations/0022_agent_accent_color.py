from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("company", "0021_transparent_company_prompt_version")]

    operations = [
        migrations.AddField(
            model_name="transparentcompanyagentdefinition",
            name="accentColor",
            field=models.CharField(blank=True, default="", max_length=7),
        ),
    ]
