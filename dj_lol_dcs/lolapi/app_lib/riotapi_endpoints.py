"""Centralized location for (Riot-)API endpoints"""

SUMMONER_BY_NAME = lambda api_host, name, api_key: (
    "https://{}/lol/summoner/v3/summoners/by-name/{}?api_key={}".format(
        api_host,
        name,
        api_key)
)
TIERS_BY_SUMMONER_ID = lambda api_host, summoner_id, api_key: (
    "https://{}/lol/league/v3/positions/by-summoner/{}?api_key={}".format(
        api_host,
        summoner_id,
        api_key
    )
)
SPECTATOR_BY_SUMMONER_ID = lambda api_host, summoner_id, api_key: (
    "https://{}/lol/spectator/v3/active-games/by-summoner/{}?api_key={}".format(
        api_host,
        summoner_id,
        api_key)
)
MATCHLIST_BY_ACCOUNT_ID = lambda api_host, account_id, api_key, end_time, begin_time: (
    "https://{}/lol/match/v3/matchlists/by-account/{}?queue=420&api_key={}&endTime={}&beginTime={}".format(
        api_host,
        account_id,
        api_key,
        end_time,
        begin_time)
)
MATCH_BY_MATCH_ID = lambda api_host, match_id, api_key: (
    "https://{}/lol/match/v3/matches/{}?api_key={}".format(
        api_host,
        match_id,
        api_key)
)
TIMELINE_BY_MATCH_ID = lambda api_host, match_id, api_key: (
    "https://{}/lol/match/v3/timelines/by-match/{}?api_key={}".format(
        api_host,
        match_id,
        api_key)
)
