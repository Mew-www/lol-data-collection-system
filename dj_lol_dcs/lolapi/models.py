from django.db import models


class GameVersion(models.Model):
    id = models.CharField(primary_key=True, max_length=255)


class Champion(models.Model):
    name = models.CharField(primary_key=True, max_length=255)


class ChampionGameData(models.Model):
    game_version = models.ForeignKey(
        'GameVersion',
        on_delete=models.CASCADE
    )
    champion = models.ForeignKey(
        'Champion',
        on_delete=models.CASCADE
    )
    data_json = models.TextField()

    class Meta:
        unique_together = tuple(('game_version', 'champion'))


class StaticGameData(models.Model):
    game_version = models.ForeignKey(
        'GameVersion',
        primary_key=True,
        on_delete=models.CASCADE
    )
    profile_icons_data_json = models.TextField()
    champions_data = models.ManyToManyField(
        'ChampionGameData'
    )
    items_data_json = models.TextField()
    summonerspells_data_json = models.TextField()
    runes_data_json = models.TextField()
