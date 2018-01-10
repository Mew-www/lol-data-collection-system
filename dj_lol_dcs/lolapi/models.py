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


# Player data


class Region(models.Model):
    """A game server (or regional group of game servers)"""
    name = models.CharField(primary_key=True, max_length=255)


class Summoner(models.Model):
    """A player's game account per specific game server"""
    account_id = models.BigIntegerField()
    summoner_id = models.BigIntegerField()
    latest_name = models.CharField(max_length=255)
    region = models.ForeignKey(
        'Region',
        on_delete=models.SET_NULL,
        null=True
    )

    class Meta:
        unique_together = (('region', 'account_id'), ('region', 'summoner_id'))


# Match history data


class HistoricalMatch(models.Model):
    """A match that has ended; Match-ID per specific game server"""
    match_id = models.BigIntegerField()
    region = models.ForeignKey(
        'Region',
        on_delete=models.SET_NULL,
        null=True
    )
    game_version = models.ForeignKey(
        'GameVersion',
        on_delete=models.SET_NULL,
        null=True
    )
    regional_tier_avg = models.CharField(max_length=255)
    match_result_json = models.TextField(null=True)
    match_timeline_json = models.TextField(null=True)

    class Meta:
        unique_together = tuple(('match_id', 'region'))
