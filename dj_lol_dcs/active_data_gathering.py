#!/usr/bin/env python
import os
import sys
import requests
import json
import time
import math

import lolapi.app_lib.riotapi_endpoints as r_endpoints
import lolapi.app_lib.datadragon_endpoints as d_endpoints
from lolapi.app_lib.regional_riotapi_hosts import RegionalRiotapiHosts
from lolapi.app_lib.riot_api import RiotApi
from lolapi.app_lib.api_key_container import ApiKeyContainer
from lolapi.app_lib.exceptions import RiotApiError, ConfigurationError, RatelimitMismatchError

import django
os.environ['DJANGO_SETTINGS_MODULE'] = 'dj_lol_dcs.settings'
django.setup()
from lolapi.models import GameVersion, Champion, ChampionGameData, StaticGameData
from lolapi.models import Region, Summoner, SummonerTierHistory
from lolapi.models import HistoricalMatch
from lolapi.app_lib.enumerations import Tiers
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction


def get_or_create_region(region_name):
    try:
        matching_region = Region.objects.get(name=region_name)
    except ObjectDoesNotExist:
        matching_region = Region(name=region_name)
        matching_region.save()
    return matching_region


def update_or_create_summoner(region, api_summoner_dict):
    try:
        matching_summoner = Summoner.objects.get(region=region, account_id=api_summoner_dict['accountId'])
        matching_summoner.latest_name = api_summoner_dict['name']
        matching_summoner.save()
    except ObjectDoesNotExist:
        matching_summoner = Summoner(
            region=region,
            account_id=api_summoner_dict['accountId'],
            summoner_id=api_summoner_dict['id'],
            latest_name=api_summoner_dict['name']
        )
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
        known_game_version_ids = map(lambda gv: gv.semver, known_game_versions)
        new_game_version_ids = [ver for ver in updated_game_versions if ver not in known_game_version_ids]
        for version_id in new_game_version_ids:
            print('Saving new game version {}'.format(version_id))
            new_ver = GameVersion(semver=version_id)
            new_ver.save()
        matching_known_version = next(
            filter(lambda gv: '.'.join(gv.semver.split('.')[0:2]) == match_version_id,
                   known_game_versions),
            None
        )
    return matching_known_version


def main():
    # Arguments
    if len(sys.argv) < 2:
        print('Usage: python active_data_gathering.py Region')
        sys.exit(1)
    region_name = sys.argv[1].upper()
    api_key = os.environ['RIOT_API_KEY']
    app_rate_limits = [[20, 1], [100, 120]]  # [[num-requests, within-seconds], ..]
    known_tiers = Tiers()

    # API init
    api_hosts = RegionalRiotapiHosts()
    riotapi = RiotApi(ApiKeyContainer(api_key, app_rate_limits), api_hosts, r_endpoints)

    target_summoner = None
    while not target_summoner:

        # Input loop
        target_summoner_name = input("\nPlease input summoner (from {}) who is in game:\n".format(region_name))
        api_summoner_dict = riotapi.get_summoner(region_name, target_summoner_name).json()
        region = get_or_create_region(region_name)
        summoner = update_or_create_summoner(region, api_summoner_dict)
        try:
            ongoing_match_dict = riotapi.get_active_match(region.name, summoner.summoner_id).json()
            if ongoing_match_dict['gameQueueConfigId'] != 420:
                print("Summoner '{}' is in different game/queue mode, try another".format(summoner.latest_name))
                continue
        except RiotApiError as err:
            if err.response.status_code == 404:
                print("Summoner '{}' is not in active match, try another.".format(summoner.latest_name))
                continue
            else:
                break

        # Get tiers of the participants and average match tier
        teams_tiers = {}
        for p in ongoing_match_dict['participants']:
            api_p_summoner_dict = riotapi.get_summoner(region_name, p['summonerName']).json()
            participant_summoner = update_or_create_summoner(region, api_p_summoner_dict)
            api_tiers_list = riotapi.get_tiers(region_name, participant_summoner.summoner_id).json()
            participant_tier_milestone = update_summoner_tier_history(participant_summoner, api_tiers_list)
            if p['teamId'] not in teams_tiers:
                teams_tiers[p['teamId']] = []
            teams_tiers[p['teamId']].append({'champion_id': p['championId'], 'tier': participant_tier_milestone.tier})
        teams_avg_tiers = []
        for team_id, team in teams_tiers.items():
            teams_avg_tiers.append(known_tiers.get_average(map(lambda x: x['tier'], team)))
        match_avg_tier = known_tiers.get_average(teams_avg_tiers)
        print(json.dumps(teams_tiers))
        print("Average tier for match is: {}".format(match_avg_tier))

        # Save preliminary match data since avg_tier and meta_tier aren't obtainable post-game
        try:
            HistoricalMatch.objects.get(match_id=ongoing_match_dict['gameId'], region=region)
        except ObjectDoesNotExist:
            new_match = HistoricalMatch(
                match_id=ongoing_match_dict['gameId'],
                region=region,
                regional_tier_avg=match_avg_tier,
                regional_tier_meta=json.dumps(teams_tiers)
            )
            new_match.save()

        # Wait 10 minutes at a time for match to finish
        game_has_been_on_minutes = 0
        if ongoing_match_dict['gameStartTime'] != 0:
            game_has_been_on_minutes = math.floor((time.time()*1000 - ongoing_match_dict['gameStartTime']) / 1000 / 60)
        print("Game has been on for {} minutes".format(game_has_been_on_minutes))
        print(ongoing_match_dict['gameId'])
        match_result = None
        match_finished = False
        while not match_finished:
            try:
                match_result = riotapi.get_match_result(ongoing_match_dict['platformId'], ongoing_match_dict['gameId'])
                match_finished = True
            except RiotApiError as err:
                if err.response.status_code == 404:
                    print("Match {} is still going on ({} minutes). Wait another 5 minutes".format(
                        ongoing_match_dict['gameId'],
                        math.floor((time.time() * 1000 - ongoing_match_dict['gameStartTime']) / 1000 / 60)
                    ))
                    # Wait another 5 minutes
                    time.sleep(300)
                else:
                    raise RiotApiError(err.response)

        # Update match data with result and timeline
        try:
            match = HistoricalMatch.objects.get(match_id=ongoing_match_dict['gameId'], region=region)
        except ObjectDoesNotExist:
            print("Match {} wasn't saved while it was ongoing, why is this?".format(ongoing_match_dict['gameId']))
            break
        result_dict = match_result.json()
        timeline_dict = riotapi.get_match_timeline(result_dict['platformId'], result_dict['gameId']).json()
        match.game_version = get_or_create_game_version(result_dict)
        match.game_duration = result_dict['gameDuration']
        match.match_result_json = json.dumps(result_dict)
        match.match_timeline_json = json.dumps(timeline_dict)
        match.save()
        print("Saved match {} successfully in two phases (pre for avg_tier, post for result/timeline)".format(
            result_dict['gameId']
        ))


if __name__ == "__main__":
    main()
