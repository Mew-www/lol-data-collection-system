#!/usr/bin/env python
import os
import sys
import requests
import json

import lolapi.app_lib.riotapi_endpoints as riotapi_endpoints
import lolapi.app_lib.datadragon_endpoints as d_endpoints
from lolapi.app_lib.regional_riotapi_hosts import RegionalRiotapiHosts
from lolapi.app_lib.riot_api import RiotApi
from lolapi.app_lib.api_key_container import ApiKeyContainer, MethodRateLimits
from lolapi.app_lib.mysql_requesthistory_checking import MysqlRequestHistory
from lolapi.app_lib.exceptions import RiotApiError, ConfigurationError, RatelimitMismatchError

import django
os.environ['DJANGO_SETTINGS_MODULE'] = 'dj_lol_dcs.settings'
django.setup()
from lolapi.models import GameVersion, Champion, ChampionGameData, StaticGameData
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction


def main():
    # Arguments
    api_key = os.environ['RIOT_API_KEY']
    app_rate_limits = json.loads(os.environ['RIOT_APP_RATE_LIMITS_JSON'])  # [[num-requests, within-seconds], ..]

    # API init
    method_rate_limits = {
        '/lol/summoner/v3/summoners/by-name/{summonerName}': {
            'EUW': [[2000, 60]],
            'KR': [[2000, 60]],
            'NA': [[2000, 60]],
            'EUNE': [[1600, 60]],
            'BR': [[1300, 60]],
            'TR': [[1300, 60]],
            'LAN': [[1000, 60]],
            'LAS': [[1000, 60]],
            'JP': [[800, 60]],
            'OCE': [[800, 60]],
            'RU': [[600, 60]]
        },
        'leagues-v3 endpoints': {
            'EUW': [[300, 60]],
            'NA': [[270, 60]],
            'EUNE': [[165, 60]],
            'BR': [[90, 60]],
            'KR': [[90, 60]],
            'LAN': [[80, 60]],
            'LAS': [[80, 60]],
            'TR': [[60, 60]],
            'OCE': [[55, 60]],
            'JP': [[35, 60]],
            'RU': [[35, 60]]
        },
        '/lol/match/v3/matchlists/by-account/{accountId}': [[1000, 10]],
        '/lol/match/v3/[matches,timelines]': [[500, 10]],
        'All other endpoints': [[20000, 10]]
    }
    riotapi = RiotApi(
        ApiKeyContainer(
            api_key,
            app_rate_limits,
            MethodRateLimits(method_rate_limits)),
        MysqlRequestHistory(
            os.environ['MYSQL_REQUESTHISTORY_USERNAME'],
            os.environ['MYSQL_REQUESTHISTORY_PASSWORD'],
            os.environ['MYSQL_REQUESTHISTORY_DBNAME'],
            None
        ),
        RegionalRiotapiHosts(),
        riotapi_endpoints)

    known_game_versions = list(GameVersion.objects.all())
    updated_game_versions = requests.get(d_endpoints.VERSIONS).json()

    known_game_version_ids = map(lambda gv: gv.semver, known_game_versions)
    new_game_version_ids = [ver for ver in updated_game_versions if ver not in known_game_version_ids]

    for version_id in new_game_version_ids:
        print('Saving new game version {}'.format(version_id))
        new_ver = GameVersion(semver=version_id)
        new_ver.save()

    for ver in list(GameVersion.objects.all()):
        try:
            # Try to query (if it'd exist)
            StaticGameData.objects.get(game_version=ver)
        except ObjectDoesNotExist:
            semver = ver.semver
            print('Found no matching static data set, for version {}'.format(semver))
            # If any of the requests to DataDragon fails, don't save partial static data
            with transaction.atomic():
                profile_icons = requests.get(d_endpoints.PROFILE_ICONS(semver)).json()
                champions_list = requests.get(d_endpoints.CHAMPIONS_LIST(semver)).json()
                champion_gamedata_models = []
                for key, c in champions_list['data'].items():
                    print('Requesting {} data for version {}'.format(c['id'], semver))
                    gamedata = requests.get(d_endpoints.CHAMPION(semver, c['id'])).json()
                    try:
                        Champion.objects.get(name=c['name'])
                    except ObjectDoesNotExist:
                        champion_model = Champion(name=c['name'])
                        champion_model.save()
                        champion_gamedata_model = ChampionGameData(
                            game_version=ver,
                            champion=champion_model,
                            data_json=json.dumps(gamedata)
                        )
                        champion_gamedata_model.save()
                        champion_gamedata_models.append(champion_gamedata_model)
                    items = requests.get(d_endpoints.ITEMS(semver)).json()
                    summonerspells = requests.get(d_endpoints.SUMMONERSPELLS(semver)).json()
                    runes = requests.get(d_endpoints.RUNES(semver)).json()
                    matching_static_data = StaticGameData(
                        game_version=ver,
                        profile_icons_data_json=json.dumps(profile_icons),
                        items_data_json=json.dumps(items),
                        summonerspells_data_json=json.dumps(summonerspells),
                        runes_data_json=json.dumps(runes),
                    )
                    matching_static_data.save()
                    matching_static_data.champions_data.set(champion_gamedata_models)

        except RiotApiError as e:
            print(e)

        except RatelimitMismatchError as e:
            print(e, end='')
            print('. . . Exiting.')
            sys.exit(1)


if __name__ == "__main__":
    main()
