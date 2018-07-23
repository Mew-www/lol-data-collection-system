#!/usr/bin/env python
import os
import sys
import json
import itertools
import argparse

import lolapi.app_lib.riotapi_endpoints as riotapi_endpoints
from lolapi.app_lib.regional_riotapi_hosts import RegionalRiotapiHosts
from lolapi.app_lib.riot_api import RiotApi
from lolapi.app_lib.api_key_container import ApiKeyContainer, MethodRateLimits
from lolapi.app_lib.exceptions import RiotApiError, ConfigurationError, RatelimitMismatchError, MatchTakenError

import django
os.environ['DJANGO_SETTINGS_MODULE'] = 'dj_lol_dcs.settings'
django.setup()
from lolapi.models import HistoricalMatch
from lolapi.app_lib.mysql_requesthistory_checking import MysqlRequestHistory
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError
from django.db.models import Q
from lolapi.app_lib.utils import get_or_create_game_version, get_or_create_region, get_existing_summoner_or_none
from lolapi.app_lib.utils import request_and_link_timeline_to_match, request_and_return_ongoing_match_or_none


def main(args):
    tiers = args.tiers
    semver = args.semver
    start_index = args.start_index
    total_matches = args.total_matches
    total_parsed = args.total_parsed
    ratelimit_logfile_location = './{}'.format(args.ratelimit_logfile_location.lower()) if args.ratelimit_logfile_location else None

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

    def get_matches(tiers, semver, start_idx, stop_idx):
        all_matches = HistoricalMatch.objects.all()
        tier_queries = [Q(regional_tier_avg__contains=t) for t in tiers]
        tier_filter = tier_queries.pop()
        for q in tier_queries:
            tier_filter |= q
        return itertools.islice(
            all_matches.filter(tier_filter).filter(game_version__semver=semver).values('region__name',
                                                                                       'match_result_json'),
            start_idx,
            stop_idx
        )

    def parse_stats(participant_stats_dict):
        return {
            'kills': participant_stats_dict['kills'],
            'deaths': participant_stats_dict['deaths'],
            'assists': participant_stats_dict['assists'],
        }

    statistics_with_aggregates = []
    num_matches = 0
    for m in get_matches(tiers, semver, start_index, start_index+total_matches):
        m_data = json.loads(m['match_result_json'])
        m_region = m['region__name']
        region_obj = get_or_create_region(m['region__name'])
        for p_identity in m_data['participantIdentities']:
            p_id = p_identity['participantId']
            p_account_id = p_identity['player']['accountId']
            p_data = next(filter(lambda a_p: a_p['participantId'] == p_id, m_data['participants']))
            p_champion = p_data['championId']
            p_lane_role = '{}_{}'.format(p_data['timeline']['lane'], p_data['timeline']['role'])
            print('{} playing champ {}, fetching matchlists'.format(p_identity['player']['summonerName'], p_champion))
            p_historical_statistics = {}
            p_historical_aggregates = {}
            ms_then = m_data['gameCreation']-1000  # Offset by 1s to prevent loading initial (comparison) match
            week_ms = 7*24*60*60*1000
            num_parsed = 0
            for i in range(1, 3+1):
                p_matchlist = None
                try:
                    p_matchlist = riotapi.get_matchlist(m_region,
                                                        p_account_id,
                                                        end_time=ms_then-((i-1)*week_ms),
                                                        begin_time=ms_then-(i*week_ms))
                except RiotApiError as err:
                    if err.response.status_code == 429:
                        print('Received 429 (may be interface, not necessary ratelimit). Exiting.')
                        sys.exit(0)
                    elif err.response.status_code == 404:
                        pass
                if p_matchlist is None:
                    break  # Skip this participant
                for p_m_ref in p_matchlist.json()['matches']:
                    if p_m_ref['champion'] == p_champion:
                        p_m_obj = None
                        try:
                            p_m_obj = HistoricalMatch.objects.get(match_id=p_m_ref['gameId'], region=region_obj)
                            print('Fetched game {} from db'.format(p_m_ref['gameId']), end=' ')
                        except ObjectDoesNotExist:
                            try:
                                p_m_obj = HistoricalMatch(
                                    match_id=p_m_ref['gameId'],
                                    region=region_obj
                                )
                                result_dict = riotapi.get_match_result(p_m_ref['platformId'], p_m_ref['gameId']).json()
                                p_m_obj.game_version = get_or_create_game_version(result_dict)
                                p_m_obj.game_duration = result_dict['gameDuration']
                                p_m_obj.match_result_json = json.dumps(result_dict)
                                request_and_link_timeline_to_match(p_m_obj, riotapi, p_m_ref['platformId'], retries=2)
                                p_m_obj.save()
                                print('Saved game {} result and timeline'.format(p_m_ref['gameId']), end=' ')
                            except IntegrityError:
                                # If match was created by another process, fetch it
                                p_m_obj = HistoricalMatch.objects.get(match_id=p_m_ref['gameId'], region=m_region)
                        # A bit redundant but doesn't matter too much
                        result_dict = json.loads(p_m_obj.match_result_json)
                        p_m_p_data = next(filter(lambda a_p: a_p['participantId'] == p_id, result_dict['participants']))
                        lane_role = '{}_{}'.format(p_m_ref['lane'], p_m_ref['role'])
                        if lane_role not in p_historical_statistics:
                            p_historical_statistics[lane_role] = []
                        p_historical_statistics[lane_role].append(parse_stats(p_m_p_data['stats']))
                        num_parsed += 1
                        print('[{}/{}]'.format(num_parsed, total_parsed))
                    if num_parsed == total_parsed:
                        break
                if num_parsed == total_parsed:
                    break
            if len(p_historical_statistics) > 0:
                for lane_role in p_historical_statistics:
                    lane_role_target_and_deltas = []
                    for idx, statistics in enumerate(p_historical_statistics[lane_role]):
                        target_and_deltas = {'match': statistics}
                        if idx - 2 >= 0:
                            target_and_deltas['delta2'] = {
                                'kills': sum(p_historical_statistics[lane_role][idx - i]['kills'] for i in range(2)) / 2,
                                'deaths': sum(p_historical_statistics[lane_role][idx - i]['deaths'] for i in range(2)) / 2,
                                'assists': sum(p_historical_statistics[lane_role][idx - i]['assists'] for i in range(2)) / 2
                            }
                        if idx - 3 >= 0:
                            target_and_deltas['delta3'] = {
                                'kills': sum(p_historical_statistics[lane_role][idx - i]['kills'] for i in range(3)) / 3,
                                'deaths': sum(p_historical_statistics[lane_role][idx - i]['deaths'] for i in range(3)) / 3,
                                'assists': sum(p_historical_statistics[lane_role][idx - i]['assists'] for i in range(3)) / 3
                            }
                        if idx - 4 >= 0:
                            target_and_deltas['delta4'] = {
                                'kills': sum(p_historical_statistics[lane_role][idx - i]['kills'] for i in range(4)) / 4,
                                'deaths': sum(p_historical_statistics[lane_role][idx - i]['deaths'] for i in range(4)) / 4,
                                'assists': sum(p_historical_statistics[lane_role][idx - i]['assists'] for i in range(4)) / 4
                            }
                        lane_role_target_and_deltas.append(target_and_deltas)
                    p_historical_aggregates[lane_role] = lane_role_target_and_deltas
                identifier = 'match {} statistics for {} on champ {} {}'.format(m_data['gameId'],
                                                                                p_identity['player']['summonerName'],
                                                                                p_champion,
                                                                                p_lane_role)
                statistics_with_aggregates.append([identifier, p_historical_aggregates])
        num_matches += 1
        print('{} / {} matches processed'.format(num_matches, total_matches))
    json.dump(statistics_with_aggregates, open('deltas.json', 'w'))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Fetches k & d & a deltas over a specificed number of games\' all participants.')
    parser.add_argument('--tier', action='append',
                        dest='tiers',
                        default=['MASTER', 'CHALLENGER'],
                        help='Add repeated instances of argument to target tiers.')
    parser.add_argument('--semver', action='store',
                        dest='semver',
                        required=True,
                        help='Target semver of target tiers\' games.')
    parser.add_argument('--start-index', action='store',
                        dest='start_index', type=int,
                        default=0,
                        help='Limiter for target games [start_index, start_index+total_matches].')
    parser.add_argument('--total-matches', action='store',
                        dest='total_matches', type=int,
                        default=2,
                        help='Limiter for target games [start_index, start_index+total_matches].')
    parser.add_argument('--total-parsed', action='store',
                        dest='total_parsed', type=int,
                        default=0,
                        help='Limiter for history of one (of total ten) participants\' past k & d & a.')
    parser.add_argument('--ratelimit-logfile', action='store',
                        dest='ratelimit_logfile_location',
                        default=None,
                        help='Ratelimit logfile location')
    main(parser.parse_args())

