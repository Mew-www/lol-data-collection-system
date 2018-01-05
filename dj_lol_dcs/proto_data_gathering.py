#!/usr/bin/env python

import os
import sys
import requests
import json
from operator import itemgetter
import time


def main():
    if len(sys.argv) < 2:
        print('Usage: python proto_data_gathering.py SummonerNameWithoutSpaces')
        sys.exit(1)

    api_key = os.environ['RIOT_API_KEY']
    target_summoner_name = sys.argv[1]

    # Limits in format [num-requests, seconds]
    app_rate_limits = [[20, 1], [100, 120]]
    request_history_timestamps = []

    # (GET) Summoner data
    request_history_timestamps.append(int(time.time()))
    summoner_r = requests.get("https://{}/lol/summoner/v3/summoners/by-name/{}?api_key={}"
                              .format('euw1.api.riotgames.com', target_summoner_name, api_key))

    # Confirm app-rate-limit(s); Received format e.g. "100:1,1000:10,60000:600,360000:3600" => transform to [[n,s], ..]
    received_app_rate_limits = [l.split(':') for l in summoner_r.headers['X-App-Rate-Limit'].split(',')]
    validate_rate_limits("APP_RATE_LIMIT", app_rate_limits, received_app_rate_limits)

    summoner = summoner_r.json()
    account_id = summoner['accountId']

    # (GET) Matchlist
    request_history_timestamps.append(int(time.time()))
    matchlist_r = requests.get("https://{}/lol/match/v3/matchlists/by-account/{}?queue=420&api_key={}"
                               .format('euw1.api.riotgames.com', account_id, api_key))

    # Confirm app-rate-limit(s); Received format e.g. "100:1,1000:10,60000:600,360000:3600" => transform to [[n,s], ..]
    received_app_rate_limits = [l.split(':') for l in matchlist_r.headers['X-App-Rate-Limit'].split(',')]
    validate_rate_limits("APP_RATE_LIMIT", app_rate_limits, received_app_rate_limits)

    matchlist = matchlist_r.json()
    matches = matchlist['matches']

    wins = 0
    losses = 0
    for match in matches:
        # Check rate-limit quota
        ok, wait_seconds = check_rate_limits(app_rate_limits, request_history_timestamps)
        while not ok:
            time.sleep(wait_seconds)
            # Re-check in case if multiple quotas full simultaneously
            ok, wait_seconds = check_rate_limits(app_rate_limits, request_history_timestamps)

        # (GET) Match
        request_history_timestamps.append(int(time.time()))
        print('Requesting match #{} . . .'.format(match['gameId']))
        match_r = requests.get("https://{}/lol/match/v3/matches/{}?api_key={}"
                               .format('euw1.api.riotgames.com', match['gameId'], api_key))

        # Check response status
        if match_r.status_code != 200:
            print('Match request failed with status code {}'.format(match_r.status_code))

            if match_r.status_code == 500:
                print(json.dumps(match_r.json()))
                continue
            else:
                print('Exiting')
                sys.exit(1)

        # Confirm app-rate-limit(s)
        received_app_rate_limits = [l.split(':') for l in match_r.headers['X-App-Rate-Limit'].split(',')]
        validate_rate_limits("APP_RATE_LIMIT", app_rate_limits, received_app_rate_limits)

        match = match_r.json()
        target_participant = next(filter(lambda participant: participant['player']['accountId'] == account_id,
                                         match['participantIdentities']),
                                  None)
        if target_participant is None:
            print('Could not find Summoner <=> Participant connection (account_id inconsistency, happens)')
            continue

        details = next(filter(lambda p_details: p_details['participantId'] == target_participant['participantId'],
                              match['participants']))
        if details['stats']['win']:
            wins += 1
        else:
            losses += 1

    print("{} wins ({}%), {} losses ({}%)"
          .format(wins, round(wins/(wins+losses)*100), losses, round(losses/(wins+losses)*100)))


def validate_rate_limits(limit_name, configured_limits, received_limits):
    # Compare length
    if len(configured_limits) != len(received_limits):
        print('Misconfiguration (number of limits) in {}: defined {}, received from API {}'
              .format(limit_name, json.dumps(configured_limits), json.dumps(received_limits)))
        sys.exit(1)

    # Compare contents (sorted per seconds-interval-limit)
    for idx, limit in enumerate(sorted(received_limits, key=itemgetter(1))):
        if configured_limits[idx][1] != int(limit[1]):
            print('Misconfiguration (interval mismatch) in {}: defined {}, received from API {}'
                  .format(limit_name, json.dumps(configured_limits), json.dumps(received_limits)))
            sys.exit(1)

        if configured_limits[idx][0] != int(limit[0]):
            print('Misconfiguration (max-requests mismatch) in {}: defined {}, received from API {}'
                  .format(limit_name, json.dumps(configured_limits), json.dumps(received_limits)))
            sys.exit(1)


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


if __name__ == "__main__":
    main()
