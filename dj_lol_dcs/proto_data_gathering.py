#!/usr/bin/env python
import os
import sys
import requests
import json
from operator import itemgetter
import time

import lolapi.app_lib.riotapi_endpoints as r_endpoints
import lolapi.app_lib.datadragon_endpoints as d_endpoints


def main():
    # Arguments
    if len(sys.argv) < 2:
        print('Usage: python proto_data_gathering.py SummonerNameWithoutSpaces')
        sys.exit(1)
    api_key = os.environ['RIOT_API_KEY']
    target_summoner_name = sys.argv[1]
    app_rate_limits = [[20, 1], [100, 120]]  # [[num-requests, within-seconds], ..]
    request_history_timestamps = []

    # API init
    api_hosts = RegionalRiotapiHosts()

    # (GET) Summoner data => account_id
    summoner_r = request_riotapi(
        r_endpoints.SUMMONER_BY_NAME(api_hosts.get_host_by_region('EUW'), target_summoner_name, api_key),
        app_rate_limits,
        request_history_timestamps,
        'Requesting Summoner by-name "{}" . . . '.format(target_summoner_name)
    )
    account_id = summoner_r.json()['accountId']

    # (GET) Matchlist => matches
    matchlist_r = request_riotapi(
        r_endpoints.MATCHLIST_BY_ACCOUNT_ID(api_hosts.get_host_by_region('EUW'), account_id, api_key),
        app_rate_limits,
        request_history_timestamps,
        'Requesting Matchlist of account "{}" (with filter QueueType=420) . . . '.format(account_id)
    )
    matches = matchlist_r.json()['matches']

    # Calculate wins/losses/%
    wins = 0
    losses = 0
    for match_preview in matches:
        try:
            # Check if match already exists in database
            pass
            # If exists - fetch it
            pass
            # If has static data already - skip that - else load new
            pass
            # If has timeline data already - skip that - else load new
            pass
            # If didn't exist - create one
            match_r = request_riotapi(
                r_endpoints.MATCH_BY_MATCH_ID(api_hosts.get_host_by_region('EUW'), match_preview['gameId'], api_key),
                app_rate_limits,
                request_history_timestamps,
                'Requesting match #{} . . . '.format(match_preview['gameId'])
            )
            match = match_r.json()
            # Parse match's version (major.minor , split-by-. [:2] join-by-.)
            match_version = '.'.join(match['gameVersion'].split('.')[0:2])
            print(match_version)
            # Load known versions
            pass
            # Confirm match's version exists in known versions - get first (earliest) match - check if static data in db
            pass
            # If static data not in db - load it from this version if available
            pass
            # If doesn't exist - update known versions - load static data from this version if available
            pass
            # If fails - never mind - (unable to load static data, may leave uncertainties)
            pass
            # Set match version to parsed one
            pass
            # Save
            pass

            # Seek target data-set
            target_identity = next(
                filter(lambda identity: identity['player']['accountId'] == account_id, match['participantIdentities']),
                None
            )
            if target_identity is None:
                print('Could not find Summoner <=> Participant connection (account_id inconsistency, happens)')
                continue

            target_participant = next(
                filter(lambda participant: participant['participantId'] == target_identity['participantId'],
                       match['participants'])
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
