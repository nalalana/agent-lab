from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('runtime', '0001_initial'),
    ]

    operations = [
        migrations.RenameIndex(
            model_name='openclawauditlog',
            new_name='runtime_ope_company_1aee5e_idx',
            old_name='runtime_open_company_1f148d_idx',
        ),
        migrations.RenameIndex(
            model_name='openclawauditlog',
            new_name='runtime_ope_target__f6e4dc_idx',
            old_name='runtime_open_target__5f91e8_idx',
        ),
        migrations.RenameIndex(
            model_name='openclawinstance',
            new_name='runtime_ope_company_45ad1d_idx',
            old_name='runtime_open_company_2aaa10_idx',
        ),
        migrations.RenameIndex(
            model_name='openclawinstance',
            new_name='runtime_ope_instanc_876af3_idx',
            old_name='runtime_open_instanc_4c52b7_idx',
        ),
        migrations.RenameIndex(
            model_name='openclawprovisiontask',
            new_name='runtime_ope_company_a3071e_idx',
            old_name='runtime_open_company_f99730_idx',
        ),
    ]
