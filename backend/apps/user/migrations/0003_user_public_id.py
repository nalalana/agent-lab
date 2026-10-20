from django.db import migrations, models

from apps.user.models import generate_public_id


def backfill_public_ids(apps, schema_editor):
    User = apps.get_model('user', 'User')
    db = schema_editor.connection.alias
    for user in User.objects.using(db).filter(public_id__isnull=True).iterator(chunk_size=500):
        while True:
            public_id = generate_public_id()
            if not User.objects.using(db).filter(public_id=public_id).exists():
                break
        User.objects.using(db).filter(pk=user.pk).update(public_id=public_id)


class Migration(migrations.Migration):
    dependencies = [('user', '0002_user_consent')]

    operations = [
        migrations.AddField(
            model_name='user',
            name='public_id',
            field=models.CharField(max_length=13, null=True, unique=True, editable=False),
        ),
        migrations.RunPython(backfill_public_ids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='user',
            name='public_id',
            field=models.CharField(max_length=13, unique=True, default=generate_public_id, editable=False),
        ),
    ]
