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
from django.db import IntegrityError

from sqlalchemy import create_engine
import pandas as pd


def get_incomplete_records():
    """
        Returns (pandas) DataFrame containing:
        ['match_id'] => int64
        ['region_name'] => string (Region.name)
        ['version_missing'] => boolean
        ['result_missing'] => boolean
        ['timeline_missing'] => boolean
    """
    db_engine = create_engine('postgresql://{}:{}@localhost/{}'.format(os.environ['DJ_PG_USERNAME'],
                                                                       os.environ['DJ_PG_PASSWORD'],
                                                                       os.environ['DJ_PG_DBNAME']))
    with db_engine.connect() as conn:
        # Create queries
        sql = """
                SELECT 
                    match_id,
                    lolapi_region.name as region_name,
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
                    END as timeline_missing
                FROM lolapi_historicalmatch 
                INNER JOIN lolapi_region ON lolapi_historicalmatch.region_id = lolapi_region.id 
                WHERE 
                    match_result_json IS NULL 
                    OR match_timeline_json IS NULL 
                    OR game_version_id IS NULL
                """
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


def main():
    # Arguments / configure
    if len(sys.argv) < 2:
        print("Usage: python periodical_data_repair.py RatelimitLogfile")
        sys.exit(1)
    ratelimit_logfile_location = './{}'.format(sys.argv[1].lower())
    api_key = os.environ['RIOT_API_KEY']
    app_rate_limits = json.loads(os.environ['RIOT_APP_RATE_LIMITS_JSON'])  # [[num-requests, within-seconds], ..]
    method_rate_limits = {
        '/lol/match/v3/[matches,timelines]': [[500, 10]]
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
    incomplete_matches_df = get_incomplete_records()

    # Start repairing
    for row in incomplete_matches_df.itertuples(index=False):

        # Get respective match as Django ORM object
        match_object = HistoricalMatch.objects.get(match_id=getattr(row, 'match_id'),
                                                   region=Region.objects.get(name=getattr(row, 'region_name')))

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
    main()
