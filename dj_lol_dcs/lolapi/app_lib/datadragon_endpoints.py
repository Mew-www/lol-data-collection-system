"""Centralized location for (DataDragon-)API endpoints"""

VERSIONS = 'https://ddragon.leagueoflegends.com/api/versions.json'
PROFILE_ICONS = lambda version_id: (
    'http://ddragon.leagueoflegends.com/cdn/{}/data/en_US/profileicon.json'.format(version_id)
)
CHAMPIONS_LIST = lambda version_id: (
    'http://ddragon.leagueoflegends.com/cdn/{}/data/en_US/champion.json'.format(version_id)
)
CHAMPION = lambda version_id, champion_id: (
    'http://ddragon.leagueoflegends.com/cdn/{}/data/en_US/champion/{}.json'.format(version_id, champion_id)
)
ITEMS = lambda version_id: (
    'http://ddragon.leagueoflegends.com/cdn/{}/data/en_US/item.json'.format(version_id)
)
SUMMONERSPELLS = lambda version_id: (
    'http://ddragon.leagueoflegends.com/cdn/{}/data/en_US/summoner.json'.format(version_id)
)
RUNES = lambda version_id: (
    'http://ddragon.leagueoflegends.com/cdn/{}/data/en_US/runesReforged.json'.format(version_id)
)
