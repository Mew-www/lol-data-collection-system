#!/usr/bin/env python
import os
import sys
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
from lolapi.models import GameVersion
from lolapi.models import Region
from lolapi.models import HistoricalMatch
from lolapi.app_lib.mysql_requesthistory_checking import MysqlRequestHistory
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError

from sqlalchemy import create_engine
import pandas as pd
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


def get_stats_history(current_account_id, account_id, champion_id, lane, role, summonerspells_set,
                      match_time, riotapi, region,
                      max_weeks_lookback, max_games_lookback):

    lanes = {
        'TOP': 0,
        'MID': 0,
        'JUNGLE': 0,
        'NONE': 0,
        'BOTTOM': 0
    }
    roles = {
        'NONE': 0,  # "Roaming"
        'SOLO': 0,
        'DUO': 0,  # Shared cs
        'DUO_CARRY': 0,  # Dedicated cs
        'DUO_SUPPORT': 0  # No cs
    }
    laneroles = {}  # For auto-fill detection
    num_games = 0  # For inactive detection
    num_games_on_the_champion = 0  # For rusty detection
    summonerspells_on_the_champion = []  # For "unusual summonerspells" detection
    gamedatas_on_the_champion = []

    def parse_stats_one_game(participant_dict):
        participant_stats_dict = participant_dict['stats']
        participant_timeline_dict = participant_dict['timeline']
        return {
            'kills': participant_stats_dict['kills'],
            'deaths': participant_stats_dict['deaths'],
            'assists': participant_stats_dict['assists'],
            'cs10': None if 'creepsPerMinDeltas' not in participant_timeline_dict or '0-10' not in participant_timeline_dict['creepsPerMinDeltas'] else participant_timeline_dict['creepsPerMinDeltas']['0-10'],
            'cs': participant_stats_dict['totalMinionsKilled'],
            'gold10': None if 'goldPerMinDeltas' not in participant_timeline_dict or '0-10' not in participant_timeline_dict['goldPerMinDeltas'] else participant_timeline_dict['goldPerMinDeltas']['0-10'],
            'gold': participant_stats_dict['goldEarned'],
            'champ_damage': participant_stats_dict['totalDamageDealtToChampions'],
            'tower_damage': participant_stats_dict['damageDealtToTurrets'],
            'healing': participant_stats_dict['totalHeal'],
            'double_kills': participant_stats_dict['doubleKills']
        }

    # Normalize lane name from match_result_json to same as here match references
    if lane == "MIDDLE":
        lane = "MID"
    week_in_ms = 7*24*60*60*1000
    for week_i in range(max_weeks_lookback):
        end_time = match_time - 1000 - (week_i * week_in_ms)  # Offset by 1s
        start_time = end_time - week_in_ms
        try:
            week_matchlist = riotapi.get_matchlist(region.name,
                                                   current_account_id,
                                                   end_time=end_time,
                                                   begin_time=start_time)
            for m_ref in week_matchlist.json()['matches']:
                # Local vars for faster lookup since used multiple times
                m_ref_lane = m_ref['lane']
                m_ref_role = m_ref['role']

                # Increment counters
                num_games += 1
                lanes[m_ref_lane] += 1
                roles[m_ref_role] += 1
                if m_ref_lane+m_ref_role not in laneroles:
                    laneroles[m_ref_lane+m_ref_role] = 0
                laneroles[m_ref_lane + m_ref_role] += 1

                if m_ref['champion'] == champion_id:
                    num_games_on_the_champion += 1
                    if len(gamedatas_on_the_champion) < max_games_lookback:
                        try:
                            m_obj = HistoricalMatch.objects.get(match_id=m_ref['gameId'], region=region)
                            if m_obj.match_result_json is not None:
                                result_dict = json.loads(m_obj.match_result_json)
                            else:
                                result_dict = riotapi.get_match_result(m_ref['platformId'], m_ref['gameId']).json()
                                m_obj.game_version = get_or_create_game_version(result_dict)
                                m_obj.game_duration = result_dict['gameDuration']
                                m_obj.match_result_json = json.dumps(result_dict)
                                # Don't bother checking for timeline here
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
                                request_and_link_timeline_to_match(m_obj, riotapi, m_ref['platformId'], retries=2)
                                m_obj.save()
                            except IntegrityError:
                                # If match was created by another process, fetch it
                                m_obj = HistoricalMatch.objects.get(match_id=m_ref['gameId'], region=region)
                                if m_obj.match_result_json is not None:
                                    result_dict = json.loads(m_obj.match_result_json)
                                else:
                                    result_dict = riotapi.get_match_result(m_ref['platformId'], m_ref['gameId']).json()
                        # Check if it is remake
                        if result_dict['gameDuration'] < 300:
                            continue
                        historical_p_identity = next(filter(lambda p: p['player']['accountId'] == account_id,
                                                            result_dict['participantIdentities']))
                        historical_p_id = historical_p_identity['participantId']
                        historical_p_data = next(filter(lambda p: p['participantId'] == historical_p_id,
                                                        result_dict['participants']))
                        historical_summonerspells = set([historical_p_data['spell1Id'], historical_p_data['spell2Id']])
                        if historical_summonerspells not in summonerspells_on_the_champion:
                            summonerspells_on_the_champion.append(historical_summonerspells)
                        historical_record = parse_stats_one_game(historical_p_data)
                        historical_p_teammembers_champdamage = sorted(
                            list(map(lambda p_data: p_data['stats']['totalDamageDealtToChampions'],
                                     filter(lambda p: p['teamId'] == historical_p_data['teamId'],
                                            result_dict['participants']))),
                            reverse=True)
                        historical_record['nr_carry'] = historical_p_teammembers_champdamage.index(
                            historical_record['champ_damage']
                        )
                        gamedatas_on_the_champion.append(historical_record)
        except RiotApiError as err:
            if err.response.status_code == 429:
                raise RiotApiError(err.response) from None
            elif err.response.status_code == 404:
                continue  # No matches found {week_i} weeks in past, keep checking since the timeframe is explicit

    # Calculate historic booleans, averages, and aggregates
    history = {
        # If playing not the most common lane
        'is_offlane': lane != sorted(lanes.keys(), reverse=True, key=lambda k: lanes[k])[0] if num_games > 0 else False,
        # If playing not the most common role
        'is_offrole': role != sorted(roles.keys(), reverse=True, key=lambda k: roles[k])[0] if num_games > 0 else False,
        # If playing neither of two most common lane+role combinations
        'is_autofill': (lane+role) not in sorted(laneroles.keys(), reverse=True, key=lambda k: laneroles[k])[0:2] if num_games > 0 else False,
        'is_rusty': num_games_on_the_champion == 0,
        'is_inactive': num_games == 0,
        'is_unusual_summonerspells': summonerspells_set not in summonerspells_on_the_champion if num_games_on_the_champion > 0 else False
        # No time correlation, moving on to result data
    }
    if len(gamedatas_on_the_champion) > 0:
        for attribute in gamedatas_on_the_champion[0].keys():
            non_none_values = [d[attribute] for d in gamedatas_on_the_champion if d[attribute] is not None]
            if len(non_none_values) > 0:
                average = sum(non_none_values) / len(non_none_values)
            else:
                average = 0
            history['avg_{}'.format(attribute)] = average
    return history


def main(args):
    ratelimit_logfile_location = './{}'.format(sys.argv[1].lower()) if len(sys.argv) > 1 else None
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

    # Target data and up-to-date game versions
    game_versions = update_and_get_versions()
    incomplete_matches_df = get_incomplete_records(args.region_name, args.semver)

    # Start repairing
    for row in incomplete_matches_df.itertuples(index=False):

        # Get respective match as Django ORM object
        match_object = HistoricalMatch.objects.get(match_id=getattr(row, 'match_id'),
                                                   region=Region.objects.get(name=args.region_name))

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
                    m_data = json.loads(match_object.match_result_json)
                    stats_histories = {}
                    for p_identity in m_data['participantIdentities']:
                        p_id = p_identity['participantId']
                        p_data = next(filter(lambda p_d: p_d['participantId'] == p_id, m_data['participants']))
                        p_history = get_stats_history(p_identity['player']['currentAccountId'],
                                                      p_identity['player']['accountId'],
                                                      p_data['championId'],
                                                      p_data['timeline']['lane'],
                                                      p_data['timeline']['role'],
                                                      set([p_data['spell1Id'], p_data['spell2Id']]),
                                                      m_data['gameCreation'],
                                                      riotapi, match_object.region,
                                                      max_weeks_lookback=3, max_games_lookback=50)
                        stats_histories[p_data['championId']] = p_history
                    match_object.match_participants_histories_json = json.dumps(stats_histories)
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
    main(parser.parse_args())
