# Generated by Django 2.0.1 on 2018-01-16 23:08

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('lolapi', '0002_auto_20180116_1525'),
    ]

    operations = [
        migrations.CreateModel(
            name='SummonerTierHistory',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('at_time', models.DateTimeField(auto_now_add=True)),
                ('tier', models.CharField(max_length=255)),
                ('summoner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='lolapi.Summoner')),
            ],
        ),
    ]
