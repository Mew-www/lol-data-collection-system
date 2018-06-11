#!/usr/bin/env python
import os
import sys
import requests
import json
import time
import math

import lolapi.app_lib.riotapi_endpoints as riotapi_endpoints
import lolapi.app_lib.datadragon_endpoints as d_endpoints
from lolapi.app_lib.regional_riotapi_hosts import RegionalRiotapiHosts
from lolapi.app_lib.riot_api import RiotApi
from lolapi.app_lib.api_key_container import ApiKeyContainer, MethodRateLimits
from lolapi.app_lib.exceptions import RiotApiError, ConfigurationError, RatelimitMismatchError, MatchTakenError

import django
os.environ['DJANGO_SETTINGS_MODULE'] = 'dj_lol_dcs.settings'
django.setup()
from lolapi.models import GameVersion, Champion, ChampionGameData, StaticGameData
from lolapi.models import Region, Summoner, SummonerTierHistory
from lolapi.models import HistoricalMatch
from lolapi.app_lib.enumerations import Tiers
from lolapi.app_lib.mysql_requesthistory_checking import MysqlRequestHistory
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError


def get_existing_summoner_or_none(riotapi, region, summoner_name):
    try:
        api_summoner_dict = riotapi.get_summoner(region.name, summoner_name).json()
    except RiotApiError as err:
        if err.response.status_code == 404:
            print("Summoner with name '{}' not found.".format(summoner_name))
            return None
        else:
            raise RiotApiError(err.response)
    return api_summoner_dict


def request_and_return_ongoing_match_or_none(riotapi, region, summoner, non_404_retries=0):
    """
        If loading ongoing match fails:
        - IF HTTP STATUS CODE 429 [ = rate-limiting ] and not Service-429 => something wrong with rate-limiting so exit
        - if http status code 404, return None
        - else retry up to N times
        - if still no, exit gracefully (re-trying sometime later in next iteration) returning None
    """
    error_retries_done = 0
    tries_permitted = 1 + non_404_retries
    while error_retries_done < tries_permitted:
        try:
            ongoing_match_dict = riotapi.get_active_match(region.name, summoner.summoner_id).json()
            if 'gameQueueConfigId' not in ongoing_match_dict or ongoing_match_dict['gameQueueConfigId'] != 420:
                print("Summoner '{}' is in different game/queue mode.".format(summoner.latest_name))
                return None
            return ongoing_match_dict
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
            elif err.response.status_code == 404:
                print("Summoner '{}' is not in active match.".format(summoner.latest_name))
                return None
            else:
                print("Failed to load ongoing match data for summoner '{}' (HTTP Error {}) - retry in 1,2,..".format(
                    summoner.latest_name,
                    err.response.status_code))
                # One, two
                time.sleep(2)
                error_retries_done += 1
    if error_retries_done == tries_permitted:
        print("Retried maximum of {} times - Riot API still returning errors so skipping this summoner for now".format(
            non_404_retries
        ))


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


def request_and_return_summoner(region_name, summoner_name, riotapi, retries=0):
    """
        If loading summoner fails:
        - IF HTTP STATUS CODE 429 [ = rate-limiting ] and not Service-429 => something wrong with rate-limiting so exit
        - else retry up to N times
        - if still no, we cannot really continue (not having the summoner data) so re-raise the RiotApiError
    """
    error_retries_done = 0
    tries_permitted = 1 + retries
    while error_retries_done < tries_permitted:
        try:
            api_p_summoner_dict = riotapi.get_summoner(region_name, summoner_name).json()
            return api_p_summoner_dict
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
                print("Failed to load summoner data for {} ({}) (HTTP Error {}) - retry in 1,2,..".format(
                    summoner_name,
                    region_name,
                    err.response.status_code))
                # One, two
                time.sleep(2)
                error_retries_done += 1
                if error_retries_done == tries_permitted:
                    print("Retried the maximum {} times to load summoner data for {} ({}).".format(
                        retries,
                        summoner_name,
                        region_name
                    ))
                    raise RiotApiError(err.response) from None


def request_and_return_summoner_tiers(region_name, summoner_id, riotapi, retries=0):
    """
        If loading summoner tiers fails:
        - IF HTTP STATUS CODE 429 [ = rate-limiting ] and not Service-429 => something wrong with rate-limiting so exit
        - else retry up to N times
        - if still no, we cannot really continue (unable to calculate match's avg tier) so re-raise the RiotApiError
    """
    error_retries_done = 0
    tries_permitted = 1 + retries
    while error_retries_done < tries_permitted:
        try:
            api_tiers_list = riotapi.get_tiers(region_name, summoner_id).json()
            return api_tiers_list
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
                print("Failed to load summoner tiers for summoner id #{} ({}) (HTTP Error {}) - retry in 1,2,..".format(
                    summoner_id,
                    region_name,
                    err.response.status_code))
                # One, two
                time.sleep(2)
                error_retries_done += 1
                if error_retries_done == tries_permitted:
                    print("Retried the maximum {} times to load summoner tiers for summoner id #{} ({}).".format(
                        retries,
                        summoner_id,
                        region_name
                    ))
                    raise RiotApiError(err.response) from None


def request_and_link_timeline_to_match(match, riotapi, platform_id, retries=0):
    """
        If loading timeline fails:
        - IF HTTP STATUS CODE 429 [ = rate-limiting ] and not Service-429 => something wrong with rate-limiting so exit
        - else retry up to N times
        - if still no, exit gracefully (leaving partial match data that can be filled later)
    """
    error_retries_done = 0
    tries_permitted = 1 + retries
    while error_retries_done < tries_permitted:
        try:
            timeline_dict = riotapi.get_match_timeline(platform_id, match.match_id).json()
            match.match_timeline_json = json.dumps(timeline_dict)
            return timeline_dict
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
                print("Failed to load timeline for match {} (HTTP Error {}) - retry in 1,2,..".format(
                    match.match_id,
                    err.response.status_code))
                # One, two
                time.sleep(2)
                error_retries_done += 1
    if error_retries_done == tries_permitted:
        print("Retried maximum of {} times - Riot API still returning errors so skipping this timeline for now".format(
            retries
        ))


def create_champion_lane_mapping(result, timeline):

    def is_topside(x, y):
        return y >= 4880 and x <= 9880 and y >= (x+3000)

    def is_bottomside(x, y):
        return y <= 9880 and x >= 4880 and y <= (x-5000)

    champion_lane_mapping = {}
    for team_id in [100, 200]:

        top = None
        jungle = None
        mid = None
        bottom = None
        support = None

        # Remember participantId is an integer here (as opposed to timeline keys)
        remaining_candidates = [p for p in result['participants'] if p['teamId'] == team_id]

        # Determine (1..6) minutely positions e.g. {'6': [(x,y), (x,y), (x,y), (x,y), (x,y), (x,y)], '7': ...}
        team_positions_min1_min6 = {}
        for match_frame in timeline['frames'][1:7]:
            for participant_id, participant_frame in match_frame['participantFrames'].items():
                if participant_id in [str(p['participantId']) for p in remaining_candidates]:
                    if participant_id not in team_positions_min1_min6:
                        team_positions_min1_min6[participant_id] = []
                    x = participant_frame['position']['x'] if 'position' in participant_frame else -120
                    y = participant_frame['position']['y'] if 'position' in participant_frame else -120
                    team_positions_min1_min6[participant_id].append((x, y))

        # Determine jungler (cannot technically be left as None)
        jungler_candidates = list(remaining_candidates)
        candidates_with_smite = [p for p in jungler_candidates if 11 in (p['spell1Id'], p['spell2Id'])]
        if len(candidates_with_smite) == 0:
            candidates_with_smite = jungler_candidates
        jungle = max(candidates_with_smite, key=lambda p: p['stats']['neutralMinionsKilled'])
        remaining_candidates = [p for p in remaining_candidates if p['participantId'] != jungle['participantId']]

        # Determine support (cannot technically be left as None)
        support = min(remaining_candidates, key=lambda p: p['stats']['totalMinionsKilled'])
        remaining_candidates = [p for p in remaining_candidates if p['participantId'] != support['participantId']]

        # Determine toplaner (who is the most in toplane area)
        top = max(remaining_candidates, key=lambda p: sum(is_topside(loc[0], loc[1]) for loc
                                                          in team_positions_min1_min6[str(p['participantId'])]))
        remaining_candidates = [p for p in remaining_candidates if p['participantId'] != top['participantId']]

        # Determine carry (who is the most in bottomlane area)
        bottom = max(remaining_candidates, key=lambda p: sum(is_bottomside(loc[0], loc[1]) for loc
                                                             in team_positions_min1_min6[str(p['participantId'])]))

        # Midlaner remains
        mid = next(p for p in remaining_candidates if p['participantId'] != bottom['participantId'])

        # Create mapping
        champion_lane_mapping[top['championId']] = 'TOP'
        champion_lane_mapping[jungle['championId']] = 'JUNGLE'
        champion_lane_mapping[mid['championId']] = 'MID'
        champion_lane_mapping[bottom['championId']] = 'BOTTOM'
        champion_lane_mapping[support['championId']] = 'SUPPORT'
    return champion_lane_mapping


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


def parse_stats_one_game(result, timeline, items_dictionary, participant_id):

    def get_item_worth(item_id):
        if item_id == 0:
            return 0
        # Rest in peace banner of command
        if item_id == 1018:
            return 2200
        return items_dictionary['data'][str(item_id)]['gold']['total']

    def get_participant_champion(p_id):
        # Killer may be a tower, participant_id 0
        if p_id == 0:
            return 0
        return next(filter(lambda p: p['participantId'] == p_id, result['participants']))['championId']

    effective_gold_spent = 0
    kills = []
    deaths = []
    fight_events = []
    for match_frame in timeline['frames']:
        for event in match_frame['events']:
            # Events are in chronological order so gold spent is calculated before/after respectively to match events
            if event['type'] == 'ITEM_PURCHASED' and event['participantId'] == participant_id:
                effective_gold_spent += get_item_worth(event['itemId'])
            elif event['type'] == 'ITEM_DESTROYED' and event['participantId'] == participant_id:
                effective_gold_spent -= get_item_worth(event['itemId'])
            elif event['type'] == 'ITEM_SOLD' and event['participantId'] == participant_id:
                effective_gold_spent -= get_item_worth(event['itemId'])
            elif event['type'] == 'ITEM_UNDO' and event['participantId'] == participant_id:
                effective_gold_spent -= get_item_worth(event['beforeId'])
                effective_gold_spent += get_item_worth(event['afterId'])
            elif event['type'] == 'CHAMPION_KILL':
                contributors = [event['killerId']]+event['assistingParticipantIds']
                if participant_id in contributors:
                    kills.append({
                        'timestamp': event['timestamp'],
                        'position': event['position'],
                        'effective_gold': effective_gold_spent,
                        'allies': contributors,
                        'enemies': [event['victimId']],
                        # Initial outcome, to be fixed depending if adjacent fight events
                        'victims': [event['victimId']]
                    })
                elif participant_id == event['victimId']:
                    deaths.append({
                        'timestamp': event['timestamp'],
                        'position': event['position'],
                        'effective_gold': effective_gold_spent,
                        'allies': [event['victimId']],
                        'enemies': contributors,
                        # Initial outcome, to be fixed depending if adjacent fight events
                        'victims': [event['victimId']]
                    })
                fight_events.append(event)  # For determining ratio of people involved without iterating all events
    # Add enemies involved
    for kill_event in kills:
        t = kill_event['timestamp']
        events_within_15s = filter(lambda e: (t - 15000) <= e['timestamp'] <= (t + 15000), fight_events)
        for event in events_within_15s:
            contributors = [event['killerId']]+event['assistingParticipantIds']
            for ally in kill_event['allies']:
                if ally in contributors:
                    # Means they (allies) scored an other kill event within 15s -> enemy
                    if event['victimId'] not in kill_event['enemies']:
                        kill_event['enemies'].append(event['victimId'])
                    if event['victimId'] not in kill_event['victims']:
                        kill_event['victims'].append(event['victimId'])
                elif ally == event['victimId']:
                    # Means both teams scored some within 15s
                    for enemy in contributors:
                        if enemy not in kill_event['enemies']:
                            kill_event['enemies'].append(enemy)
                        if event['victimId'] not in kill_event['victims']:
                            kill_event['victims'].append(event['victimId'])
    # Reversed setting (compared to kills)
    for death_event in deaths:
        t = death_event['timestamp']
        events_within_15s = filter(lambda e: (t - 15000) <= e['timestamp'] <= (t + 15000), fight_events)
        for event in events_within_15s:
            contributors = [event['killerId']]+event['assistingParticipantIds']
            for enemy in death_event['enemies']:
                if enemy in contributors:
                    # Means they (enemies) scored an other kill event within 15s -> ally
                    if event['victimId'] not in death_event['allies']:
                        death_event['allies'].append(event['victimId'])
                    if event['victimId'] not in death_event['victims']:
                        death_event['victims'].append(event['victimId'])
                elif enemy == event['victimId']:
                    # Means both teams scored some within 15s
                    for ally in contributors:
                        if ally not in death_event['allies']:
                            death_event['allies'].append(ally)
                        if event['victimId'] not in death_event['victims']:
                            death_event['victims'].append(event['victimId'])
    # Join, and sort them by timestamp, and group presumably duplicate events
    sorted_fight_events = sorted(kills+deaths, key=lambda e: e['timestamp'])
    # Replace participant ids with champion ids
    for event in sorted_fight_events:
        event['allies'] = [get_participant_champion(p_id) for p_id in event['allies']]
        event['enemies'] = [get_participant_champion(p_id) for p_id in event['enemies']]
        event['victims'] = [get_participant_champion(p_id) for p_id in event['victims']]
    # Mark duplicates to leave only "full" fights to remain (max 30s)
    for idx, event in enumerate(sorted_fight_events):
        # Skip events that are already fully cleared (all and any bring problems if subjected to empty [])
        if not len(event['victims']):
            continue
        t = event['timestamp']
        events_up_to_30s = filter(lambda e: e['timestamp'] <= (t + 30000), sorted_fight_events[(idx+1):])
        for consecutive_event in events_up_to_30s:
            # Skip consecutive events that are fully cleared (all and any bring problems if subjected to empty [])
            if not len(consecutive_event['victims']):
                continue
            if all((victim in event['victims']) for victim in consecutive_event['victims']):
                # Means the consecutive event is a subset of the current event
                for ally in consecutive_event['allies']:
                    # Move any new participants to current event
                    if ally not in event['allies']:
                        event['allies'].append(ally)
                for enemy in consecutive_event['enemies']:
                    # Move any new enemy to current event
                    if enemy not in event['enemies']:
                        event['enemies'].append(enemy)
                # Clear the victims list to indicate the (consecutive) event is a subset (and redundant, filtered later)
                consecutive_event['victims'] = []
            elif all((victim in consecutive_event['victims']) for victim in event['victims']):
                # Means the current event is a subset of the consecutive event
                for ally in event['allies']:
                    # Move any new participants to consecutive event
                    if ally not in consecutive_event['allies']:
                        consecutive_event['allies'].append(ally)
                for enemy in event['enemies']:
                    # Move any new enemy to consecutive event
                    if enemy not in consecutive_event['enemies']:
                        consecutive_event['enemies'].append(enemy)
                # Clear the victims list to indicate the (current) event is a subset (and redundant, filtered later)
                event['victims'] = []
                # Break the 30s event loop, since we emptied the current event. Continue from the next event's 30s
                break
            elif any((victim in event['victims']) for victim in consecutive_event['victims']):
                # Means the events contain partially same fight, remove those in current event, leave the off-spin
                consecutive_event['victims'] = [v for v in consecutive_event['victims'] if (v not in event['victims'])]
    # Remove duplicates
    sorted_fight_events = list(filter(lambda e: len(e['victims']) > 0, sorted_fight_events))
    return sorted_fight_events


def get_stats_history(account_id, champion_id, reallane, summonerspells_set,
                      match_time, riotapi, region, items_dictionaries,
                      max_weeks_lookback, max_games_lookback):

    # Currently NOT checking lane since an accurate check would require loading both result + timeline for each game_ref
    # lanes = {
    #    'TOP': 0,
    #    'JUNGLE': 0,
    #    'MID': 0,
    #    'BOTTOM': 0,
    #    'SUPPORT': 0
    # }
    num_games = 0  # For inactive detection
    num_games_on_the_champion = 0  # For rusty detection
    summonerspells_on_the_champion = []  # For "unusual summonerspells" detection
    gamedatas_on_the_champion_on_the_lane = []

    # Normalize lane name from match_result_json to same as here match references
    week_in_ms = 7*24*60*60*1000
    for week_i in range(max_weeks_lookback):
        end_time = match_time - 1000 - (week_i * week_in_ms)  # Offset by 1s
        start_time = end_time - week_in_ms
        try:
            week_matchlist = riotapi.get_matchlist(region.name,
                                                   account_id,
                                                   end_time=end_time,
                                                   begin_time=start_time)
            for m_ref in week_matchlist.json()['matches']:
                num_games += 1
                if m_ref['champion'] == champion_id:
                    num_games_on_the_champion += 1
                    if len(gamedatas_on_the_champion_on_the_lane) < max_games_lookback:
                        try:
                            m_obj = HistoricalMatch.objects.get(match_id=m_ref['gameId'], region=region)
                            if m_obj.match_result_json is not None:
                                result_dict = json.loads(m_obj.match_result_json)
                            else:
                                result_dict = riotapi.get_match_result(m_ref['platformId'], m_ref['gameId']).json()
                                m_obj.game_version = get_or_create_game_version(result_dict)
                                m_obj.game_duration = result_dict['gameDuration']
                                m_obj.match_result_json = json.dumps(result_dict)
                                m_obj.save()
                            if m_obj.match_timeline_json is not None:
                                timeline_dict = json.loads(m_obj.match_timeline_json)
                            else:
                                timeline_dict = request_and_link_timeline_to_match(m_obj, riotapi, m_ref['platformId'], retries=2)
                                m_obj.save()
                        except ObjectDoesNotExist:
                            try:
                                m_obj = HistoricalMatch(
                                    match_id=m_ref['gameId'],
                                    region=region
                                )
                                result_dict = riotapi.get_match_result(m_ref['platformId'], m_ref['gameId']).json()
                                m_obj.game_version = get_or_create_game_version(result_dict)
                                m_obj.game_duration = result_dict['gameDuration']
                                m_obj.match_result_json = json.dumps(result_dict)
                                timeline_dict = request_and_link_timeline_to_match(m_obj, riotapi, m_ref['platformId'], retries=2)
                                m_obj.save()
                            except IntegrityError:
                                # If match was created by another process, fetch it
                                m_obj = HistoricalMatch.objects.get(match_id=m_ref['gameId'], region=region)
                                if m_obj.match_result_json is not None:
                                    result_dict = json.loads(m_obj.match_result_json)
                                else:
                                    result_dict = riotapi.get_match_result(m_ref['platformId'], m_ref['gameId']).json()
                                if m_obj.match_timeline_json is not None:
                                    timeline_dict = json.loads(m_obj.match_timeline_json)
                                else:
                                    timeline_dict = request_and_link_timeline_to_match(m_obj, riotapi, m_ref['platformId'], retries=2)

                        # Check if it is remake
                        if result_dict['gameDuration'] < 300:
                            continue

                        # Check if lane is correct
                        lane_then = create_champion_lane_mapping(result_dict, timeline_dict)[champion_id]
                        if lane_then != reallane:
                            print('Incorrect lane, lane_then {} lane_now {}'.format(lane_then, reallane))
                            continue

                        # Ensure we have items_dictionary from static data or (preferably) cached in memory
                        historical_game_version = get_or_create_game_version(result_dict)
                        if historical_game_version.semver not in items_dictionaries:
                            # May throw ObjectDoesNotExist, in which case it bubbles up to request_history
                            static_data = StaticGameData.objects.get(game_version=historical_game_version)
                            items_dictionaries[historical_game_version.semver] = json.loads(static_data.items_data_json)

                        # Historically account ID may be different and UN-OBTAINABLE (pls riot) so we'll rely on champ
                        p_data = next(filter(lambda p: p['championId'] == champion_id, result_dict['participants']))

                        # Add summonerspells to "formerly (in 3w/40md) used" ones
                        historical_summonerspells = {p_data['spell1Id'], p_data['spell2Id']}
                        if historical_summonerspells not in summonerspells_on_the_champion:
                            summonerspells_on_the_champion.append(historical_summonerspells)

                        # Parse data
                        historical_record = parse_stats_one_game(result_dict,
                                                                 timeline_dict,
                                                                 items_dictionaries[historical_game_version.semver],
                                                                 p_data['participantId'])
                        gamedatas_on_the_champion_on_the_lane.append(historical_record)
        except RiotApiError as err:
            if err.response.status_code == 429:
                raise RiotApiError(err.response) from None
            elif err.response.status_code == 404:
                continue  # No matches found {week_i} weeks in past, keep checking since the timeframe is explicit
            else:
                print('Unexpected HTTP {} error when querying match history ({})'.format(err.response.status_code,
                                                                                         err.response.url.split('?')[0]))

    history = {
        'is_rusty': num_games_on_the_champion == 0,
        'is_inactive': num_games == 0,
        'is_unusual_summonerspells': summonerspells_set not in summonerspells_on_the_champion if num_games_on_the_champion > 0 else False,
        'fight_participation': gamedatas_on_the_champion_on_the_lane
    }
    return history


def request_and_return_match_results(match_id, match_start_time, riotapi, platform_id, non_404_retries=0):
    """
        If loading results fails:
        - IF HTTP STATUS CODE 429 [ = rate-limiting ] and not Service-429 => something wrong with rate-limiting so exit
        - else retry up to N times
        - if still no, we cannot really continue (not knowing if match finished) so re-raise the RiotApiError
    """
    error_retries_done = 0
    tries_permitted = 1 + non_404_retries
    while error_retries_done < tries_permitted:
        try:
            match_result = riotapi.get_match_result(platform_id, match_id)
            return match_result
        except RiotApiError as err:
            if err.response.status_code == 404:
                print("Match {} is still going on ({} minutes). Wait another 5 minutes".format(
                    match_id,
                    math.floor((time.time() * 1000 - match_start_time) / 1000 / 60)
                ))
                # Wait another 5 minutes
                time.sleep(300)
                continue  # This is permitted (and expected at least once) (404 error)
            elif err.response.status_code == 429:
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
                    print("Really bad. Received {} rate limit error".format(
                        err.response.headers['X-Rate-Limit-Type']))
                    raise RiotApiError(err.response) from None
            else:
                print("Failed to load results for match {} (HTTP Error {}) - retry in 1,2,..".format(
                    match_id,
                    err.response.status_code))
                # One, two
                time.sleep(2)
                error_retries_done += 1
                if error_retries_done == tries_permitted:
                    print("Retried the maximum {} times to load results for match {}.".format(
                        non_404_retries,
                        match_id
                    ))
                    raise RiotApiError(err.response) from None


def get_or_create_region(region_name):
    try:
        matching_region = Region.objects.get(name=region_name)
    except ObjectDoesNotExist:
        try:
            matching_region = Region(name=region_name)
            matching_region.save()
        except IntegrityError:
            # If region was created by another process, fetch that one
            matching_region = Region.objects.get(name=region_name)
    return matching_region


def update_or_create_summoner(region, api_summoner_dict):
    try:
        matching_summoner = Summoner.objects.get(region=region, account_id=api_summoner_dict['accountId'])
        matching_summoner.latest_name = api_summoner_dict['name']
        matching_summoner.save()
    except ObjectDoesNotExist:
        try:
            matching_summoner = Summoner(
                region=region,
                account_id=api_summoner_dict['accountId'],
                summoner_id=api_summoner_dict['id'],
                latest_name=api_summoner_dict['name']
            )
            matching_summoner.save()
        except IntegrityError:
            # If summoner was created by another process, update that one (although it may be exactly same)
            matching_summoner = Summoner.objects.get(region=region, account_id=api_summoner_dict['accountId'])
            matching_summoner.latest_name = api_summoner_dict['name']
            matching_summoner.save()
    return matching_summoner


def update_summoner_tier_history(summoner, api_tiers_list):
    soloqueue_tier_dict = next(filter(lambda t: t['queueType'] == 'RANKED_SOLO_5x5', api_tiers_list), None)
    soloqueue_tier = ("{} {}".format(soloqueue_tier_dict['tier'], soloqueue_tier_dict['rank'])
                      if soloqueue_tier_dict is not None
                      else "UNRANKED")
    summoner_tier_history = SummonerTierHistory(
        summoner=summoner,
        tier=soloqueue_tier,
        tiers_json=api_tiers_list
    )
    summoner_tier_history.save()
    return summoner_tier_history


def get_or_create_game_version(match_result):
    known_game_versions = list(GameVersion.objects.all())
    # Parse match's version (major.minor , split-by-. [:2] join-by-.)
    match_version_id = '.'.join(match_result['gameVersion'].split('.')[0:2])

    # Confirm match's version exists in known versions - get first (earliest) match
    matching_known_version = next(
        filter(lambda ver: '.'.join(ver.semver.split('.')[0:2]) == match_version_id, known_game_versions),
        None
    )

    # If match's version didn't exist amongst known versions - update them, and refresh known_game_versions
    if not matching_known_version:
        updated_game_versions = requests.get(d_endpoints.VERSIONS).json()
        known_game_version_ids = list(map(lambda gv: gv.semver, known_game_versions))
        new_game_version_ids = [ver for ver in updated_game_versions if ver not in known_game_version_ids]
        for version_id in new_game_version_ids:
            print("Saving new game version {}".format(version_id))
            try:
                new_ver = GameVersion(semver=version_id)
                new_ver.save()
            except IntegrityError:
                # If another process created the version, keep going
                pass
        matching_known_version = next(
            filter(lambda gv: '.'.join(gv.semver.split('.')[0:2]) == match_version_id,
                   known_game_versions),
            None
        )
    return matching_known_version


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
