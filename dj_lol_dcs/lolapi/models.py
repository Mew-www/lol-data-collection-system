from django.db import models

# Static game data


class GameVersion(models.Model):
    """Game client version (SemVer based)"""
    id = models.CharField(primary_key=True, max_length=255)


class Champion(models.Model):
    """Champion name enumeration"""
    name = models.CharField(primary_key=True, max_length=255)


class ChampionGameData(models.Model):
    """Champion data per specific game client version"""
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
    """Aggregate of game client data per specific game client version"""
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
