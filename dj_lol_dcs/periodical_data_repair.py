#!/usr/bin/env python
import os
import requests
import json
import time

import lolapi.app_lib.riotapi_endpoints as riotapi_endpoints
import lolapi.app_lib.datadragon_endpoints as d_endpoints
from lolapi.app_lib.regional_riotapi_hosts import RegionalRiotapiHosts
from lolapi.app_lib.riot_api import RiotApi
from lolapi.app_lib.api_key_container import ApiKeyContainer, MethodRateLimits
from lolapi.app_lib.exceptions import RiotApiError, ConfigurationError, RatelimitMismatchError, MatchTakenError

import django
os.environ['DJANGO_SETTINGS_MODULE'] = 'dj_lol_dcs.settings'
django.setup()
from lolapi.models import GameVersion, StaticGameData
from lolapi.models import Region
from lolapi.models import HistoricalMatch
from lolapi.app_lib.mysql_requesthistory_checking import MysqlRequestHistory
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError

from sqlalchemy import create_engine
import pandas as pd
from lolapi.app_lib.utils import create_champion_lane_mapping, get_stats_history, get_participant_summoners, get_stats_availability
import argparse


def get_incomplete_records(region_name, semver):
    """
        Returns (pandas) DataFrame containing:
        ['match_id'] => int64
        ['version_missing'] => boolean
        ['result_missing'] => boolean
        ['timeline_missing'] => boolean
        ['history_missing'] => boolean
    """
    db_engine = create_engine('postgresql://{}:{}@localhost/{}'.format(os.environ['DJ_PG_USERNAME'],
                                                                       os.environ['DJ_PG_PASSWORD'],
                                                                       os.environ['DJ_PG_DBNAME']))
    with db_engine.connect() as conn:
        # Create queries, optionally filtering version
        if semver is not None:
            sql = """
                    SELECT 
                        match_id,
                        CASE WHEN game_version_id IS NULL 
                            THEN TRUE 
                            ELSE FALSE 
                        END as version_missing,
                        CASE WHEN match_result_json IS NULL 
                            THEN TRUE 
                            ELSE FALSE 
                        END as result_missing,
                        CASE WHEN match_timeline_json IS NULL 
                            THEN TRUE 
                            ELSE FALSE 
                        END as timeline_missing,
                        CASE WHEN match_participants_histories_json IS NULL 
                            THEN TRUE 
                            ELSE FALSE 
                        END as history_missing
                    FROM lolapi_historicalmatch 
                    INNER JOIN lolapi_gameversion ON lolapi_historicalmatch.game_version_id = lolapi_gameversion.id
                    INNER JOIN lolapi_region ON lolapi_historicalmatch.region_id = lolapi_region.id 
                    WHERE                    
                        (match_result_json IS NULL
                        OR match_timeline_json IS NULL
                        OR match_participants_histories_json IS NULL)
                        AND regional_tier_avg IS NOT NULL
                        AND game_duration > (5*60)
                        AND lolapi_region.name = '{}'
                        AND lolapi_gameversion.semver = '{}'
                    """.format(region_name, semver)
        else:
            sql = """
                    SELECT 
                        match_id,
                        CASE WHEN game_version_id IS NULL 
                            THEN TRUE 
                            ELSE FALSE 
                        END as version_missing,
                        CASE WHEN match_result_json IS NULL 
                            THEN TRUE 
                            ELSE FALSE 
                        END as result_missing,
                        CASE WHEN match_timeline_json IS NULL 
                            THEN TRUE 
                            ELSE FALSE 
                        END as timeline_missing,
                        CASE WHEN match_participants_histories_json IS NULL 
                            THEN TRUE 
                            ELSE FALSE 
                        END as history_missing
                    FROM lolapi_historicalmatch 
                    INNER JOIN lolapi_gameversion ON lolapi_historicalmatch.game_version_id = lolapi_gameversion.id
                    INNER JOIN lolapi_region ON lolapi_historicalmatch.region_id = lolapi_region.id 
                    WHERE                    
                        (match_result_json IS NULL
                        OR match_timeline_json IS NULL
                        OR match_participants_histories_json IS NULL)
                        AND regional_tier_avg IS NOT NULL
                        AND game_duration > (5*60)
                        AND lolapi_region.name = '{}'
                    """.format(region_name)
        incomplete_matches_df = pd.read_sql(sql, conn)
        return incomplete_matches_df


def update_and_get_versions():
    known_game_versions = list(GameVersion.objects.all())
    fresh_game_versions = requests.get(d_endpoints.VERSIONS).json()
    # Compare known <=> fresh version_ids
    known_game_version_ids = list(map(lambda gv: gv.semver, known_game_versions))
    new_game_version_ids = [ver for ver in fresh_game_versions if ver not in known_game_version_ids]
    for version_id in new_game_version_ids:
        print("Saving new game version {}".format(version_id))
        try:
            new_ver = GameVersion(semver=version_id)
            new_ver.save()
        except IntegrityError:
            # If another process created the version, keep going
            pass
    # Return most recent objects from database (including older versions)
    return list(GameVersion.objects.all())


def parse_fights_one_game(result, timeline, items_dictionary, participant_id):

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


def main(args):
    ratelimit_logfile_location = './{}'.format(args.logfile) if args.logfile else None
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
    riotapi_hosts = RegionalRiotapiHosts()
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
        riotapi_hosts,
        riotapi_endpoints)

    game_versions = update_and_get_versions()
    items_dictionaries = {}

    # Get "incomplete" records as per arguments
    incomplete_matches_df = get_incomplete_records(args.region_name, args.semver)
    region = Region.objects.get(name=args.region_name)

    # Start repairing
    for row in incomplete_matches_df.itertuples(index=False):

        # Get respective match
        match_object = HistoricalMatch.objects.get(match_id=getattr(row, 'match_id'), region=region)

        # Fix if timeline is missing, standalone
        if getattr(row, 'timeline_missing'):
            error_retries_done = 0
            tries_permitted = 2
            while error_retries_done < tries_permitted:
                try:
                    timeline_dict = riotapi.get_match_timeline(
                        riotapi_hosts.get_platform_by_region(match_object.region.name),
                        match_object.match_id
                    ).json()
                    match_object.match_timeline_json = json.dumps(timeline_dict)
                    match_object.save()
                    print('Recovered match#{} timeline'.format(match_object.match_id))
                    break
                except RiotApiError as err:
                    if err.response.status_code == 429:
                        # if service rate limit from underlying service with unknown rate limit mechanism, wait 5s
                        # https://developer.riotgames.com/rate-limiting.html
                        if 'X-Rate-Limit-Type' not in err.response.headers:
                            time.sleep(5)
                            continue  # Try again (without counting this as a retry because service-rate)
                        # if a service rate limit error, wait the time returned in header, and retry without counting it
                        if err.response.headers['X-Rate-Limit-Type'] == 'service':
                            time.sleep(int(err.response.headers['Retry-After']))
                            continue  # Try again (without counting this as a retry because service-rate)
                        # else it is application or method rate limit error, something badly wrong in our rate limiting
                        else:
                            print("Really bad. Received {} rate limit error".format(
                                err.response.headers['X-Rate-Limit-Type']))
                            raise RiotApiError(err.response) from None
                    else:
                        print("Failed to load timeline for match {} (HTTP Error {}) - retry in 1,2,..".format(
                            match_object.match_id,
                            err.response.status_code))
                        # One, two
                        time.sleep(2)
                        error_retries_done += 1
            if error_retries_done == tries_permitted:
                print(
                    "Retried maximum of {} times - Riot API still returning errors, skipping this timeline".format(
                        tries_permitted
                    ))

        # Fix if result is missing, dependency for version
        if getattr(row, 'result_missing'):
            error_retries_done = 0
            tries_permitted = 2
            while error_retries_done < tries_permitted:
                try:
                    result_dict = riotapi.get_match_result(
                        riotapi_hosts.get_platform_by_region(match_object.region.name),
                        match_object.match_id
                    ).json()
                    match_object.match_result_json = json.dumps(result_dict)
                    match_object.save()
                    print('Recovered match#{} result'.format(match_object.match_id))
                    break
                except RiotApiError as err:
                    if err.response.status_code == 429:
                        # if service rate limit from underlying service with unknown rate limit mechanism, wait 5s
                        # https://developer.riotgames.com/rate-limiting.html
                        if 'X-Rate-Limit-Type' not in err.response.headers:
                            time.sleep(5)
                            continue  # Try again (without counting this as a retry because service-rate)
                        # if a service rate limit error, wait the time returned in header, and retry without counting it
                        if err.response.headers['X-Rate-Limit-Type'] == 'service':
                            time.sleep(int(err.response.headers['Retry-After']))
                            continue  # Try again (without counting this as a retry because service-rate)
                        # else it is application or method rate limit error, something badly wrong in our rate limiting
                        else:
                            print("Really bad. Received {} rate limit error".format(
                                err.response.headers['X-Rate-Limit-Type']))
                            raise RiotApiError(err.response) from None
                    else:
                        print("Failed to load result for match {} (HTTP Error {}) - retry in 1,2,..".format(
                            match_object.match_id,
                            err.response.status_code))
                        # One, two
                        time.sleep(2)
                        error_retries_done += 1
            if error_retries_done == tries_permitted:
                print(
                    "Retried maximum of {} times - Riot API still returning errors, skipping this result".format(
                        tries_permitted
                    ))
                # This also means we are unable to get version, so skip that too
                continue

        # Fix if history is missing, relies on result_json
        if getattr(row, 'history_missing'):
            error_retries_done = 0
            tries_permitted = 2
            while error_retries_done < tries_permitted:
                try:
                    m_result = json.loads(match_object.match_result_json)
                    m_timeline = json.loads(match_object.match_timeline_json)
                    try:
                        stats_histories = {}
                        for p_identity in m_result['participantIdentities']:
                            p_id = p_identity['participantId']
                            p_data = next(filter(lambda p_d: p_d['participantId'] == p_id, m_result['participants']))
                            p_history = get_stats_availability(p_identity['player']['currentAccountId'],
                                                               p_data['championId'],
                                                               create_champion_lane_mapping(m_result, m_timeline)[p_data['championId']],
                                                               {p_data['spell1Id'], p_data['spell2Id']},
                                                               {p_data['stats']['perk0'], p_data['stats']['perk1'],
                                                                p_data['stats']['perk2'], p_data['stats']['perk3'],
                                                                p_data['stats']['perk4'], p_data['stats']['perk5']},
                                                               m_result['gameCreation'],
                                                               riotapi, match_object.region,
                                                               max_weeks_lookback=3, max_games_lookback=50)
                            stats_histories[p_data['championId']] = p_history
                        match_object.match_participants_histories_json = json.dumps(stats_histories)
                    except ObjectDoesNotExist:
                        print('Missing static data (items namely) for a historical game version')
                        pass
                    match_object.save()
                    print('Recovered match#{} history'.format(match_object.match_id))
                    break
                except RiotApiError as err:
                    if err.response.status_code == 429:
                        # if service rate limit from underlying service with unknown rate limit mechanism, wait 5s
                        # https://developer.riotgames.com/rate-limiting.html
                        if 'X-Rate-Limit-Type' not in err.response.headers:
                            time.sleep(5)
                            continue  # Try again (without counting this as a retry because service-rate)
                        # if a service rate limit error, wait the time returned in header, and retry without counting it
                        if err.response.headers['X-Rate-Limit-Type'] == 'service':
                            time.sleep(int(err.response.headers['Retry-After']))
                            continue  # Try again (without counting this as a retry because service-rate)
                        # else it is application or method rate limit error, something badly wrong in our rate limiting
                        else:
                            print("Really bad. Received {} rate limit error".format(
                                err.response.headers['X-Rate-Limit-Type']))
                            raise RiotApiError(err.response) from None
                    else:
                        print("Failed to load a historical match (HTTP Error {}) - retry in 1,2,..".format(
                            err.response.status_code))
                        # One, two
                        time.sleep(2)
                        error_retries_done += 1
            if error_retries_done == tries_permitted:
                print(
                    "Retried maximum of {} times - Riot API still returning errors, skipping this history".format(
                        tries_permitted
                    ))

        # Fix if version is missing, relies on result_json
        if getattr(row, 'version_missing'):
            match_version_id = '.'.join(json.loads(match_object.match_result_json)['gameVersion'].split('.')[0:2])
            matching_known_version = next(
                filter(lambda gv: '.'.join(gv.semver.split('.')[0:2]) == match_version_id,
                       game_versions),
                None
            )
            if matching_known_version:
                match_object.game_version = matching_known_version
                match_object.save()
                print('Recovered match#{}\'s semantic game version'.format(match_object.match_id))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Repair missing result/timeline/history in games with known tier')
    parser.add_argument('--region', dest='region_name', required=True, help='Region name of target games')
    parser.add_argument('--semver', dest='semver', default=None, help='Optionally limit repairs to specific version')
    parser.add_argument('--logfile', dest='logfile', default=None, help='Logfile location')
    main(parser.parse_args())
