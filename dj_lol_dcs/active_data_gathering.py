#!/usr/bin/env python
import os
import sys
import json
import time
import math

import lolapi.app_lib.riotapi_endpoints as riotapi_endpoints
from lolapi.app_lib.regional_riotapi_hosts import RegionalRiotapiHosts
from lolapi.app_lib.riot_api import RiotApi
from lolapi.app_lib.api_key_container import ApiKeyContainer, MethodRateLimits
from lolapi.app_lib.exceptions import RiotApiError, ConfigurationError, RatelimitMismatchError, MatchTakenError

import django
os.environ['DJANGO_SETTINGS_MODULE'] = 'dj_lol_dcs.settings'
django.setup()
from lolapi.models import HistoricalMatch
from lolapi.app_lib.enumerations import Tiers
from lolapi.app_lib.mysql_requesthistory_checking import MysqlRequestHistory
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError
from lolapi.app_lib.utils import get_or_create_game_version, get_or_create_region, get_existing_summoner_or_none
from lolapi.app_lib.utils import update_or_create_summoner
from lolapi.app_lib.utils import request_and_link_timeline_to_match, request_and_return_ongoing_match_or_none
from lolapi.app_lib.utils import create_champion_lane_mapping, get_stats_history


def persist_ongoing_match_and_get_participant_summoners(riotapi, known_tiers, region, ongoing_match_dict, items_dictionaries):
    """
        # Get tiers of the participants and average match tier (10+10 requests)
        # Save preliminary match data since avg_tier and meta_tier aren't obtainable post-game
        # Wait 5 minutes at a time, starting from 20 minutes, for match to finish (1 request per check)
        # Return participant summoners
    """

    # Check if match is already being observed, if so then early return via Exception. (If happens parallel, let it be.)
    try:
        HistoricalMatch.objects.get(match_id=ongoing_match_dict['gameId'], region=region)
        raise MatchTakenError()
    except ObjectDoesNotExist:
        pass

    # Get identities, tiers of the participants (20 requests)
    # then calculate the average match tier
    teams_tiers = {}
    participant_summoners = []
    # Gather all tiers in a dict {team_key: [tier_and_misc, ..], ..}
    for p in ongoing_match_dict['participants']:
        api_p_summoner_dict = request_and_return_summoner(region.name, p['summonerName'], riotapi, retries=2)
        p_summoner = update_or_create_summoner(region, api_p_summoner_dict)
        participant_summoners.append(p_summoner)
        api_tiers_list = request_and_return_summoner_tiers(region.name, p_summoner.summoner_id, riotapi, retries=2)
        participant_tier_milestone = update_summoner_tier_history(p_summoner, api_tiers_list)
        if p['teamId'] not in teams_tiers:
            teams_tiers[p['teamId']] = []
        teams_tiers[p['teamId']].append({'champion_id': p['championId'], 'tier': participant_tier_milestone.tier})
    # Calculate avg tier per team
    teams_avg_tiers = []
    for team_id, team in teams_tiers.items():
        teams_avg_tiers.append(known_tiers.get_average(map(lambda x: x['tier'], team)))
    # Calculate total match avg tier
    match_avg_tier = known_tiers.get_average(teams_avg_tiers)
    for team_key in teams_tiers:
        print("Tiers of team {}: {}".format(team_key, ', '.join(map(lambda t: t['tier'], teams_tiers[team_key]))))
    print("Average tier for match is: {}".format(match_avg_tier))

    # Save preliminary match data since avg_tier and meta_tier aren't obtainable post-game
    try:
        HistoricalMatch.objects.get(match_id=ongoing_match_dict['gameId'], region=region)
    except ObjectDoesNotExist:
        try:
            new_match = HistoricalMatch(
                match_id=ongoing_match_dict['gameId'],
                region=region,
                regional_tier_avg=match_avg_tier,
                regional_tier_meta=json.dumps(teams_tiers)
            )
            new_match.save()
        except IntegrityError:
            # If match was created by another process, keep going
            pass

    # Wait 5 minutes at a time, starting from 20 minutes, for match to finish (1 request per check)
    game_has_been_on_minutes = 0
    if ongoing_match_dict['gameStartTime'] != 0:
        game_has_been_on_minutes = math.floor((time.time() * 1000 - ongoing_match_dict['gameStartTime']) / 1000 / 60)
    print("Game has been on for {} minutes. ".format(game_has_been_on_minutes), end='')
    if game_has_been_on_minutes < 20:
        print("Wait for {} minutes to re-check if match done.".format(20 - game_has_been_on_minutes))
        time.sleep((20 - game_has_been_on_minutes) * 60)
    else:
        print("Check if match is done.")

    match_result = request_and_return_match_results(
        ongoing_match_dict['gameId'],
        ongoing_match_dict['gameStartTime'],
        riotapi,
        ongoing_match_dict['platformId'],
        non_404_retries=2)

    # Update match data with result and timeline (1 request, for timeline)
    try:
        match = HistoricalMatch.objects.get(match_id=ongoing_match_dict['gameId'], region=region)
    except ObjectDoesNotExist:
        print("Match {} wasn't saved while it was ongoing, why is this?".format(ongoing_match_dict['gameId']))
        raise ObjectDoesNotExist()
    result_dict = match_result.json()
    match_game_version = get_or_create_game_version(result_dict)
    match.game_version = match_game_version
    match.game_duration = result_dict['gameDuration']
    match.match_result_json = json.dumps(result_dict)
    print('Requesting match {} timeline'.format(ongoing_match_dict['gameId']))
    request_and_link_timeline_to_match(match, riotapi, result_dict['platformId'], retries=2)
    match.save()
    try:
        print('Requesting match {} participants\' histories'.format(ongoing_match_dict['gameId']))
        request_and_link_histories_to_match(match, riotapi, region, items_dictionaries)
    except ObjectDoesNotExist:
        print("Missing static game data for game version {}. Unable to retrieve histories.".format(match_game_version.semver))
        pass
    match.save()
    print("Saved match {} successfully in two phases (pre for avg_tier, post for result/timeline[/histories])".format(
        result_dict['gameId']
    ))
    return participant_summoners


def request_and_link_histories_to_match(match, riotapi, region, items_dictionaries, retries=0):
    """
        If loading histories fails:
        - IF HTTP STATUS CODE 429 [ = rate-limiting ] and not Service-429 => something wrong with rate-limiting so exit
        - else retry up to N times
        - if still no, exit gracefully (leaving partial match data that can be filled later)
        If loading static data fails:
        - cancel loading histories, move on without the histories
    """
    error_retries_done = 0
    tries_permitted = 1 + retries
    while error_retries_done < tries_permitted:
        try:
            m_result = json.loads(match.match_result_json)
            m_timeline = json.loads(match.match_timeline_json)
            stats_histories = {}
            for i, p_identity in enumerate(m_result['participantIdentities']):
                print('Requesting history {} / 10'.format(i+1))
                p_id = p_identity['participantId']
                p_data = next(filter(lambda a_p: a_p['participantId'] == p_id, m_result['participants']))
                p_history = get_stats_history(p_identity['player']['currentAccountId'],
                                              p_data['championId'],
                                              create_champion_lane_mapping(m_result, m_timeline)[p_data['championId']],
                                              set([p_data['spell1Id'], p_data['spell2Id']]),
                                              m_result['gameCreation'],
                                              riotapi, region, items_dictionaries,
                                              max_weeks_lookback=3, max_games_lookback=40)
                stats_histories[p_data['championId']] = p_history
            match.match_participants_histories_json = json.dumps(stats_histories)
            break
        except RiotApiError as err:
            if err.response.status_code == 429:
                # if service rate limit from underlying service with unknown rate limit mechanism, wait 5s
                # https://developer.riotgames.com/rate-limiting.html
                if 'X-Rate-Limit-Type' not in err.response.headers:
                    time.sleep(5)
                    continue  # Try again (without counting this as a retry because it is the service being crowded)
                # if a service rate limit error, wait the time returned in header, and retry without counting it
                if err.response.headers['X-Rate-Limit-Type'] == 'service':
                    time.sleep(int(err.response.headers['Retry-After']))
                    continue  # Try again (without counting this as a retry because it is the service being crowded)
                # else it is application or method rate limit error, something badly wrong in our rate limiting
                else:
                    print("Really bad. Received {} rate limit error".format(err.response.headers['X-Rate-Limit-Type']))
                    raise RiotApiError(err.response) from None
            else:
                print("Failed to load a historical match (HTTP Error {}) - retry in 1,2,..".format(
                    err.response.status_code))
                # One, two
                time.sleep(2)
                error_retries_done += 1
    if error_retries_done == tries_permitted:
        print("Retried maximum of {} times - Riot API still returning errors so skipping this history for now".format(
            retries
        ))


def main():
    # Arguments / configure
    if len(sys.argv) < 2:
        print("Usage: python active_data_gathering.py Region OptionalRatelimitLogfile")
        sys.exit(1)
    region_name = sys.argv[1].upper()
    ratelimit_logfile_location = './{}'.format(sys.argv[2].lower()) if len(sys.argv) > 2 else None
    api_key = os.environ['RIOT_API_KEY']
    app_rate_limits = json.loads(os.environ['RIOT_APP_RATE_LIMITS_JSON'])  # [[num-requests, within-seconds], ..]
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

    # API init
    tiers = Tiers()
    riotapi = RiotApi(
        ApiKeyContainer(
            api_key,
            app_rate_limits,
            MethodRateLimits(method_rate_limits)),
        MysqlRequestHistory(
            os.environ['MYSQL_REQUESTHISTORY_USERNAME'],
            os.environ['MYSQL_REQUESTHISTORY_PASSWORD'],
            os.environ['MYSQL_REQUESTHISTORY_DBNAME'],
            ratelimit_logfile_location
        ),
        RegionalRiotapiHosts(),
        riotapi_endpoints)
    cached_items_dictionaries = {}

    target_summoners = []
    while True:
        # Do input loop if no existing target summoners (from automated loop or so)
        if len(target_summoners) == 0:
            start = False
            while not start:
                # Input loop (2 requests)
                target_name = input("\nPlease input summoner on {} to definitely-not-stalk:\n".format(region_name))
                while len(target_name) == 0:
                    target_name = input("Input summoner on {} to definitely-not-stalk (✿◉‿◉)🗡:\n".format(region_name))
                    if len(target_name) > 0:
                        print("Thank you. (סּ‿סּ✿)")
                region = get_or_create_region(region_name)
                api_summoner_dict = get_existing_summoner_or_none(riotapi, region, target_name)
                if api_summoner_dict:
                    summoner = update_or_create_summoner(region, api_summoner_dict)
                    target_summoners.append(summoner)
                else:
                    if len(target_summoners) == 0:
                        print("Try another summoner.")
                        continue
                    else:
                        print("Try another summoner, or start.")
                print("Current targets: {}".format(', '.join(map(lambda s: s.latest_name, target_summoners))))
                yesok = input("Type 'Yes'/'OK' to start; anything else will prompt for adding another summoner:\n")
                if 'yes' in yesok.lower() or 'ok' in yesok.lower():
                    start = True

        ongoing_match = None  # Will be set to a _dict
        attempt_count = 0
        stalk_threshold = 5  # 6min times 5... is 30min of checking "is one of targets in game"
        summoner_with_match = None
        while not ongoing_match and attempt_count < stalk_threshold:
            # If repeated attempt, wait a little (6 min? Typical time between matches for someone continuing soloQ-ing?)
            if attempt_count > 0:
                print("None of targets were in ongoing match, wait 6 minutes and re-check all.")
                time.sleep(360)
            attempt_count += 1
            for target_summoner in target_summoners:
                ongoing_match = request_and_return_ongoing_match_or_none(riotapi, region, target_summoner)
                # If found, stop looping targets, we have what we need, also mark the summoner whose match it is
                if ongoing_match:
                    print("Found out summoner {} is in an ongoing match.".format(target_summoner.latest_name))
                    summoner_with_match = target_summoner
                    break

        # If we couldn't find a target in that 30min (6 times) of definitely-not-stalking, switch to manual input loop
        if not ongoing_match:
            print("None of current targets ({}) have entered a game in past 30 minutes.".format(
                ', '.join(map(lambda s: s.latest_name, target_summoners))
            ))
            print("Switching to the manual control, please specify targets in the following prompt.")
            target_summoners = []
            continue

        # Else continue to the ongoing match
        try:
            target_summoners = persist_ongoing_match_and_get_participant_summoners(riotapi, tiers, region, ongoing_match, cached_items_dictionaries)
            # Continue the 'while True' -loop with these new cute interesting target summoners (ʘ‿ʘ✿)
            print("New targets: {}".format(', '.join(map(lambda s: s.latest_name, target_summoners))))
        except RiotApiError as err:
            # if it is application or method rate limit error, something badly wrong in our rate limiting
            if (
                err.response.status_code == 429
                and 'X-Rate-Limit-Type' not in err.response.headers
                and err.response.headers['X-Rate-Limit-Type'] != 'service'
            ):
                print("Quitting 'cause Riot said ({}) rate limit full. (◕‸ ◕✿)".format(
                    err.response.headers['X-Rate-Limit-Type']
                ))
                sys.exit(1)
            # else it is another error the subroutine couldn't handle, so find another.. target (✿◉‿◉)
            else:
                # not the one whose match caused an error, though (◕__◕✿)
                target_summoners = list(filter(lambda s: s.summoner_id != summoner_with_match.summoner_id,
                                               target_summoners))
        except MatchTakenError:
            # That target was taken, so find another
            print('Match #{} taken already by another process. ;_; Searching new one..'.format(ongoing_match['gameId']))
            target_summoners = list(filter(lambda s: s.summoner_id != summoner_with_match.summoner_id,
                                           target_summoners))


if __name__ == "__main__":
    main()
