#!/usr/bin/env python

import os
import sys
import requests
import json
from operator import itemgetter
import time


def main():
    # Arguments
    if len(sys.argv) < 2:
        print('Usage: python proto_data_gathering.py SummonerNameWithoutSpaces')
        sys.exit(1)
    api_key = os.environ['RIOT_API_KEY']
    target_summoner_name = sys.argv[1]
    app_rate_limits = [[20, 1], [100, 120]]  # [[num-requests, within-seconds], ..]
    request_history_timestamps = []

    # (GET) Summoner data => account_id
    summoner_r = request_riotapi(
        "https://{}/lol/summoner/v3/summoners/by-name/{}?api_key={}".format(
            'euw1.api.riotgames.com',
            target_summoner_name,
            api_key),
        app_rate_limits,
        request_history_timestamps,
        'Requesting Summoner by-name "{}" . . . '.format(target_summoner_name)
    )
    account_id = summoner_r.json()['accountId']

    # (GET) Matchlist => matches
    matchlist_r = request_riotapi(
        "https://{}/lol/match/v3/matchlists/by-account/{}?queue=420&api_key={}".format(
            'euw1.api.riotgames.com',
            account_id,
            api_key),
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
            # (GET) Match
            match_r = request_riotapi(
                "https://{}/lol/match/v3/matches/{}?api_key={}".format(
                    'euw1.api.riotgames.com',
                    match_preview['gameId'],
                    api_key),
                app_rate_limits,
                request_history_timestamps,
                'Requesting match #{} . . . '.format(match_preview['gameId'])
            )
            match = match_r.json()

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
