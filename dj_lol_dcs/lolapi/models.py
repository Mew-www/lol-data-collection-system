from django.db import models


# Rate limiting


class RiotapiRequest(models.Model):
    """A request made-or-to-be-made to Riot API, a lot of sorts to assist ratelimit tracking"""
    at_time = models.DateTimeField(auto_now_add=True)
    api_key = models.CharField(max_length=255)  # Redundant but more convenient to query, not big overhead
    region = models.ForeignKey(
        'Region',
        on_delete=models.CASCADE,
        null=True
    )
    method = models.CharField(max_length=255)


# Static game data


class GameVersion(models.Model):
    """Game client version (SemVer based)"""
    semver = models.CharField(max_length=255, unique=True)


class Champion(models.Model):
    """Champion name enumeration"""
    name = models.CharField(max_length=255, unique=True)


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
        unique=True,
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
    name = models.CharField(max_length=255, unique=True)


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


class SummonerTierHistory(models.Model):
    """A player's tier on a given moment in time"""
    summoner = models.ForeignKey(
        'Summoner',
        on_delete=models.CASCADE
    )
    at_time = models.DateTimeField(auto_now_add=True)
    tier = models.CharField(max_length=255)
    tiers_json = models.TextField()

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
    regional_tier_avg = models.CharField(max_length=255, null=True)
    regional_tier_meta = models.TextField(max_length=255, null=True)
    game_duration = models.IntegerField(null=True)
    match_result_json = models.TextField(null=True)
    match_timeline_json = models.TextField(null=True)

    class Meta:
        unique_together = tuple(('match_id', 'region'))
