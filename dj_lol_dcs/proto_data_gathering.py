#!/usr/bin/env python
import os
import sys
import requests
import json
from operator import itemgetter
import time

import lolapi.app_lib.riotapi_endpoints as r_endpoints
import lolapi.app_lib.datadragon_endpoints as d_endpoints

import django
os.environ['DJANGO_SETTINGS_MODULE'] = 'dj_lol_dcs.settings'
django.setup()
from lolapi.models import GameVersion, Champion, ChampionGameData, StaticGameData
from lolapi.models import Region, Summoner
from lolapi.models import HistoricalMatch
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction


def main():
    # Arguments
    if len(sys.argv) < 2:
        print('Usage: python proto_data_gathering.py Region SummonerNameWithoutSpaces')
        sys.exit(1)
    api_key = os.environ['RIOT_API_KEY']
    region = sys.argv[1].upper()
    target_summoner_name = sys.argv[2]
    app_rate_limits = [[20, 1], [100, 120]]  # [[num-requests, within-seconds], ..]
    request_history_timestamps = []

    # API init
    api_hosts = RegionalRiotapiHosts()

    # (GET) Summoner data => account_id
    summoner_r = request_riotapi(
        r_endpoints.SUMMONER_BY_NAME(api_hosts.get_host_by_region(region), target_summoner_name, api_key),
        app_rate_limits,
        request_history_timestamps,
        'Requesting Summoner by-name "{}" . . . '.format(target_summoner_name)
    )
    summoner = summoner_r.json()
    account_id = summoner['accountId']

    # (GET) Matchlist => matches
    matchlist_r = request_riotapi(
        r_endpoints.MATCHLIST_BY_ACCOUNT_ID(api_hosts.get_host_by_region(region), account_id, api_key),
        app_rate_limits,
        request_history_timestamps,
        'Requesting Matchlist of account "{}" (with filter QueueType=420) . . . '.format(account_id)
    )
    matches = matchlist_r.json()['matches']

    # Calculate wins/losses/%
    wins = 0
    losses = 0
    known_game_versions = list(GameVersion.objects.all())
    for match_preview in matches:
        # If RiotApi errors - break loop
        try:
            # Check if match's region exists in database - else add it
            match_region = api_hosts.get_region_by_platform(match_preview['platformId'])
            try:
                matching_region = Region.objects.get(name=match_region)
            except ObjectDoesNotExist:
                matching_region = Region(name=match_region)
                matching_region.save()

            # Check if match details (results + timeline) exists in database - else add it
            try:
                match = HistoricalMatch.objects.get(match_id=match_preview['gameId'], region=matching_region)
                match_result = json.loads(match.match_result_json)
                print('Match #{} existed in database, using existing dataset'.format(match_preview['gameId']))
            except ObjectDoesNotExist:
                match_r = request_riotapi(
                    r_endpoints.MATCH_BY_MATCH_ID(
                        api_hosts.get_host_by_platform(match_preview['platformId']),
                        match_preview['gameId'],
                        api_key),
                    app_rate_limits,
                    request_history_timestamps,
                    'Requesting results for match #{} . . . '.format(match_preview['gameId'])
                )
                match_result = match_r.json()
                # Parse match's version (major.minor , split-by-. [:2] join-by-.) - if below 7.22 then skip match
                if (int(match_result['gameVersion'].split('.')[0]) <= 7
                        and int(match_result['gameVersion'].split('.')[1]) <= 22):
                    break
                match_version_id = '.'.join(match_result['gameVersion'].split('.')[0:2])
                # Confirm match's version exists in known versions - get first (earliest) match
                matching_known_version = next(
                    filter(lambda ver: '.'.join(ver.id.split('.')[0:2]) == match_version_id, known_game_versions),
                    None
                )
                # If match's version didn't exist amongst known versions - update them, and refresh known_game_versions
                if not matching_known_version:
                    updated_game_versions = requests.get(d_endpoints.VERSIONS).json()
                    new_game_version_ids = [ver for ver in updated_game_versions if ver not in known_game_versions]
                    for version_id in new_game_version_ids:
                        print('Saving new game version {}'.format(version_id))
                        new_ver = GameVersion(id=version_id)
                        new_ver.save()
                    known_game_versions = list(GameVersion.objects.all())
                    matching_known_version = next(
                        filter(lambda ver: '.'.join(ver.id.split('.')[0:2]) == match_version_id, known_game_versions),
                        None
                    )
                # If found a matching version (else never mind) - check it's static data exists
                if matching_known_version:
                    try:
                        # Try to query (if it'd exist)
                        StaticGameData.objects.get(game_version=matching_known_version)
                    except ObjectDoesNotExist:
                        print('Found no matching static data set, for version {}'.format(matching_known_version.id))
                        # If any of the requests to DataDragon fails, don't save partial static data
                        with transaction.atomic():
                            profile_icons = requests.get(d_endpoints.PROFILE_ICONS(matching_known_version.id)).json()
                            champions_list = requests.get(d_endpoints.CHAMPIONS_LIST(matching_known_version.id)).json()
                            champion_gamedata_models = []
                            for key, c in champions_list['data'].items():
                                print('Requesting {} for version {}'.format(c['id'], matching_known_version.id))
                                gamedata = requests.get(d_endpoints.CHAMPION(matching_known_version.id, c['id'])).json()
                                try:
                                    champion_model = Champion.objects.get(name=c['name'])
                                except ObjectDoesNotExist:
                                    champion_model = Champion(name=c['name'])
                                    champion_model.save()
                                champion_gamedata_model = ChampionGameData(
                                    game_version=matching_known_version,
                                    champion=champion_model,
                                    data_json=json.dumps(gamedata)
                                )
                                champion_gamedata_model.save()
                                champion_gamedata_models.append(champion_gamedata_model)
                            items = requests.get(d_endpoints.ITEMS(matching_known_version.id)).json()
                            summonerspells = requests.get(d_endpoints.SUMMONERSPELLS(matching_known_version.id)).json()
                            runes = requests.get(d_endpoints.RUNES(matching_known_version.id)).json()
                            matching_static_data = StaticGameData(
                                game_version=matching_known_version,
                                profile_icons_data_json=json.dumps(profile_icons),
                                items_data_json=json.dumps(items),
                                summonerspells_data_json=json.dumps(summonerspells),
                                runes_data_json=json.dumps(runes),
                            )
                            matching_static_data.champions_data.set(champion_gamedata_models)
                            matching_static_data.save()
                timeline_r = request_riotapi(
                    r_endpoints.TIMELINE_BY_MATCH_ID(
                        api_hosts.get_host_by_platform(match_preview['platformId']),
                        match_preview['gameId'],
                        api_key),
                    app_rate_limits,
                    request_history_timestamps,
                    'Requesting timeline for match #{} . . . '.format(match_preview['gameId'])
                )
                match_timeline = timeline_r.json()
                new_match = HistoricalMatch(
                    match_id=match_preview['gameId'],
                    region=matching_region,
                    game_version=matching_known_version,
                    match_result_json=json.dumps(match_result),
                    match_timeline_json=json.dumps(match_timeline)
                )
                new_match.save()

            # Seek target data-set
            target_identity = next(
                filter(
                    lambda identity: identity['player']['accountId'] == account_id,
                    match_result['participantIdentities']),
                None
            )
            if target_identity is None:
                print('Could not find Summoner <=> Participant connection (account_id inconsistency, happens)')
                continue

            target_participant = next(
                filter(lambda participant: participant['participantId'] == target_identity['participantId'],
                       match_result['participants'])
            )

            # Calculations
            if target_participant['stats']['win']:
                wins += 1
            else:
                losses += 1

        except RiotApiError as e:
            print(e)

        except RatelimitMismatchError as e:
            print(e, end='')
            print('. . . Exiting.')
            sys.exit(1)

    print("{} wins ({}%), {} losses ({}%)"
          .format(wins, round(wins/(wins+losses)*100), losses, round(losses/(wins+losses)*100)))


class ApiKeyContainer:
    """Container for API-key and respective app-rate-limit(s); Encapsulates and aggregates them together"""

    def __init__(self, api_key, app_rate_limits):
        self.__api_key = api_key
        self.__app_rate_limits = app_rate_limits

    def get_api_key(self):
        return self.__api_key

    def get_app_rate_limits(self):
        return self.__app_rate_limits

    def change_key(self, new_api_key, new_app_rate_limits):
        self.__api_key = new_api_key
        self.__app_rate_limits = new_app_rate_limits


class RiotApi:

    def __init__(self, api_key_container, regional_endpoints):
        self.__api_key_container = api_key_container
        self.__regional_endpoints = regional_endpoints
        pass


def request_riotapi(url, app_rate_limits, request_history_timestamps, pre_request_print=None):

    # Check rate-limit quotas, catches first full quota
    ok, wait_seconds = check_rate_limits(app_rate_limits, request_history_timestamps)
    while not ok:
        time.sleep(wait_seconds)
        # Re-check in case if multiple quotas full simultaneously
        ok, wait_seconds = check_rate_limits(app_rate_limits, request_history_timestamps)

    # Update request history
    request_history_timestamps.append(int(time.time()))

    # (GET)
    if pre_request_print:
        print(pre_request_print, end='')
    response = requests.get(url)

    # Check response status
    if response.status_code == 200:
        print('200 - OK')
    else:
        raise RiotApiError(response)

    # Confirm app-rate-limit(s); Received format e.g. "100:1,1000:10,60000:600,360000:3600" => transform to [[n,s], ..]
    received_app_rate_limits = [l.split(':') for l in response.headers['X-App-Rate-Limit'].split(',')]
    validate_rate_limits("APP_RATE_LIMIT", app_rate_limits, received_app_rate_limits)

    return response


def validate_rate_limits(limit_name, configured_limits, received_limits):
    # Compare length
    if len(configured_limits) != len(received_limits):
        msg = 'Misconfiguration (number of limits) in {}: defined {}, received from API {}'.format(
            limit_name,
            json.dumps(configured_limits),
            json.dumps(received_limits))
        raise RatelimitMismatchError(msg)

    # Compare contents (sorted per seconds-interval-limit)
    for idx, limit in enumerate(sorted(received_limits, key=itemgetter(1))):
        if configured_limits[idx][1] != int(limit[1]):
            msg = 'Misconfiguration (interval mismatch) in {}: defined {}, received from API {}'.format(
                limit_name,
                json.dumps(configured_limits),
                json.dumps(received_limits))
            raise RatelimitMismatchError(msg)

        if configured_limits[idx][0] != int(limit[0]):
            msg = 'Misconfiguration (max-requests mismatch) in {}: defined {}, received from API {}'.format(
                limit_name,
                json.dumps(configured_limits),
                json.dumps(received_limits))
            raise RatelimitMismatchError(msg)


def check_rate_limits(limits, request_history):
    epoch_now = int(time.time())
    for limit in limits:
        max_requests_in_timeframe, timeframe_size = limit
        timeframe_start = epoch_now - timeframe_size
        requests_done_in_timeframe = list(filter(lambda timestamp: timestamp >= timeframe_start, request_history))
        print("[{}/{}, in {} second timeframe]".format(len(requests_done_in_timeframe), max_requests_in_timeframe, timeframe_size))
        if len(requests_done_in_timeframe) >= max_requests_in_timeframe:
            return False, (timeframe_size - (epoch_now - requests_done_in_timeframe[-1]))
    return True, None


class RegionalRiotapiHosts:
    """Region <=references=> Platform <=references=> Host; Platforms are multiple for NA1/NA"""
    __hosts = {
        "br1.api.riotgames.com":  {'platforms': ["BR1"],       'region': "BR"},
        "eun1.api.riotgames.com": {'platforms': ["EUN1"],      'region': "EUNE"},
        "euw1.api.riotgames.com": {'platforms': ["EUW1"],      'region': "EUW"},
        "jp1.api.riotgames.com":  {'platforms': ["JP1"],       'region': "JP"},
        "kr.api.riotgames.com":   {'platforms': ["KR"],        'region': "KR"},
        "la1.api.riotgames.com":  {'platforms': ["LA1"],       'region': "LAN"},
        "la2.api.riotgames.com":  {'platforms': ["LA2"],       'region': "LAS"},
        "na1.api.riotgames.com":  {'platforms': ["NA1", "NA"], 'region': "NA"},
        "oc1.api.riotgames.com":  {'platforms': ["OC1"],       'region': "OCE"},
        "tr1.api.riotgames.com":  {'platforms': ["TR1"],       'region': "TR"},
        "ru.api.riotgames.com":   {'platforms': ["RU"],        'region': "RU"},
        "pbe1.api.riotgames.com": {'platforms': ["PBE1"],      'region': "PBE"}
    }

    def get_host_by_platform(self, platform):
        """This could be one-liner (using next's default argument), but more explicit using StopIteration instead"""
        try:
            matching_host = next(host for host, ref in self.__hosts.items() if (platform in ref['platforms']))
            return matching_host
        except StopIteration:
            return None

    def get_host_by_region(self, region):
        """This could be one-liner (using next's default argument), but more explicit using StopIteration instead"""
        try:
            matching_host = next(host for host, ref in self.__hosts.items() if ref['region'] == region)
            return matching_host
        except StopIteration:
            return None

    def get_region_by_platform(self, platform):
        """This could be one-liner (using next's default argument), but more explicit using StopIteration instead"""
        try:
            matching_region = next(ref['region'] for h, ref in self.__hosts.items() if (platform in ref['platforms']))
            return matching_region
        except StopIteration:
            return None


# API-response HTTP exceptions
##
class RiotApiError(Exception):
    """<base class> Raise when RiotGames API returns non-2xx response"""
    def __init__(self, api_response):
        msg = "HTTP Error {}".format(api_response.status_code)
        self.message = msg
        self.response = api_response
        super(RiotApiError, self).__init__(msg)


# Exceptions that indicate "something requires re-configuring"
##
class ConfigurationError(Exception):
    """<base class> Raise when something wrongly configured, presumably fatal."""
    pass


class RatelimitMismatchError(ConfigurationError):
    """Raise when validating ratelimit (configured <=> api_response.headers) fails."""
    pass


if __name__ == "__main__":
    main()
