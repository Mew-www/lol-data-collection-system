# Generated by Django 2.0.1 on 2018-01-19 00:54

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('lolapi', '0006_riotapirequest'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='riotapirequest',
            name='region',
        ),
        migrations.DeleteModel(
            name='RiotapiRequest',
        ),
    ]
