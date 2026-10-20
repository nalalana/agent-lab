from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True
    dependencies = [('user', '0003_user_public_id')]
    operations = [migrations.CreateModel(
        name='NewsHotspot',
        fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('title', models.CharField(max_length=120)),
            ('summary', models.CharField(max_length=300)),
            ('content', models.TextField()),
            ('published_at', models.DateTimeField(auto_now_add=True)),
            ('author', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to='user.user')),
        ],
        options={'ordering': ['-published_at', '-id']},
    )]
