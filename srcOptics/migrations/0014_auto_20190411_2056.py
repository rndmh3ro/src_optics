# Generated by Django 2.1.5 on 2019-04-11 20:56

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('srcOptics', '0013_repository_earliest_commit'),
    ]

    operations = [
        migrations.AlterField(
            model_name='statistic',
            name='interval',
            field=models.TextField(choices=[('DY', 'Day'), ('WK', 'Week'), ('MN', 'Month')], db_index=True, max_length=5),
        ),
    ]