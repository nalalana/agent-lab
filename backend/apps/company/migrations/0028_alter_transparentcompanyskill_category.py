from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('company', '0027_knowledge_action_denies'),
    ]

    operations = [
        migrations.AlterField(
            model_name='transparentcompanyskill',
            name='category',
            field=models.CharField(default='通用', max_length=50),
        ),
    ]
