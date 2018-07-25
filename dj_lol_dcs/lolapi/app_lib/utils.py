from lolapi.app_lib.exceptions import RiotApiError, ConfigurationError, RatelimitMismatchError, MatchTakenError
from lolapi.models import GameVersion, Champion, ChampionGameData, StaticGameData
from lolapi.models import HistoricalMatch
from lolapi.models import Region, Summoner, SummonerTierHistory
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError
import lolapi.app_lib.datadragon_endpoints as d_endpoints
import json
import time
import requests
import math


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


def get_participant_summoners(riotapi, known_tiers, region, ongoing_match_dict):

    # Get identities, tiers of the participants (20 requests)
    # then calculate the average match tier
    teams_tiers = {}
    participant_summoners = []
    participants = []
    # Gather all tiers in a dict {team_key: [tier_and_misc, ..], ..}
    for p in ongoing_match_dict['participants']:
        api_p_summoner_dict = request_and_return_summoner(region.name, p['summonerName'], riotapi, retries=2)
        p_summoner = update_or_create_summoner(region, api_p_summoner_dict)
        participant_summoners.append(p_summoner)
        participants.append(p)
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

    return participant_summoners, participants


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


def request_history(game_start_time, summoner, champion_id, summonerspells, reallane, riotapi, region, retries=0):
    """
        If loading histories fails:
        - IF HTTP STATUS CODE 429 [ = rate-limiting ] and not Service-429 => something wrong with rate-limiting so exit
        - else retry up to N times
        - if still no, exit gracefully (leaving partial match data that can be filled later)
    """
    # Fix an API error with "just-started" games
    if not int(game_start_time):
        game_start_time = time.time()*1000
    error_retries_done = 0
    tries_permitted = 1 + retries
    while error_retries_done < tries_permitted:
        try:
            p_history = get_stats_history(summoner.account_id,
                                          champion_id,
                                          reallane,
                                          summonerspells,
                                          game_start_time,
                                          riotapi, region,
                                          max_weeks_lookback=3, max_games_lookback=40)
            return p_history
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


def calc_participant_aggressiveness_and_judgment(past_games):
    aggressiveness_and_judgment = {
        'solo': {'ratio': 0, 'aggro': 0},
        'skirmish': {'ratio': 0, 'aggro': 0},
        'team': {'ratio': 0, 'aggro': 0}
    }
    if len(past_games) == 0:
        return aggressiveness_and_judgment
    solo = {
        'win': {
            'x': [],
            'y': []
        },
        'neutral': {
            'x': [],
            'y': []
        },
        'loss': {
            'x': [],
            'y': []
        }
    }
    skirmish = {
        'win': {
            'x': [],
            'y': []
        },
        'neutral': {
            'x': [],
            'y': []
        },
        'loss': {
            'x': [],
            'y': []
        }
    }
    team = {
        'win': {
            'x': [],
            'y': []
        },
        'neutral': {
            'x': [],
            'y': []
        },
        'loss': {
            'x': [],
            'y': []
        }
    }
    for past_game in past_games:
        for e in past_game:
            outcome = len([v for v in e['victims'] if v in e['enemies']])-len([v for v in e['victims'] if v in e['allies']])
            if len(e['allies']) == 1:
                if outcome > 0:
                    solo['win']['x'].append(e['timestamp']/1000)
                    solo['win']['y'].append(e['effective_gold'])
                elif outcome == 0:
                    solo['neutral']['x'].append(e['timestamp']/1000)
                    solo['neutral']['y'].append(e['effective_gold'])
                else:
                    solo['loss']['x'].append(e['timestamp']/1000)
                    solo['loss']['y'].append(e['effective_gold'])
            elif len(e['allies']) < 4:
                if outcome > 0:
                    skirmish['win']['x'].append(e['timestamp']/1000)
                    skirmish['win']['y'].append(e['effective_gold'])
                elif outcome == 0:
                    skirmish['neutral']['x'].append(e['timestamp']/1000)
                    skirmish['neutral']['y'].append(e['effective_gold'])
                else:
                    skirmish['loss']['x'].append(e['timestamp']/1000)
                    skirmish['loss']['y'].append(e['effective_gold'])
            else:
                if outcome > 0:
                    team['win']['x'].append(e['timestamp']/1000)
                    team['win']['y'].append(e['effective_gold'])
                elif outcome == 0:
                    team['neutral']['x'].append(e['timestamp']/1000)
                    team['neutral']['y'].append(e['effective_gold'])
                else:
                    team['loss']['x'].append(e['timestamp']/1000)
                    team['loss']['y'].append(e['effective_gold'])
    for fight_type, fight_data in [('solo', solo), ('skirmish', skirmish), ('team', team)]:
        aggressiveness_and_judgment[fight_type]['ratio'] = (
            (len(fight_data['win']['x'])-len(fight_data['loss']['x']))/len(past_games)
        )
        aggressiveness_and_judgment[fight_type]['aggro'] = (
            (len(fight_data['win']['x'])+len(fight_data['neutral']['x'])+len(fight_data['loss']['x']))/len(past_games)
        )
    return aggressiveness_and_judgment


def parse_participant_postgame_stats(participant_data, extraction_rules):
    participant_postgame_stats = {}
    for statname, extraction_fn in extraction_rules.items():
        participant_postgame_stats[statname] = extraction_fn(participant_data)
    return participant_postgame_stats


def get_stats_history(account_id, reallane,
                      match_time, riotapi, region, items_dictionaries,
                      max_weeks_lookback, max_games_lookback):
    """
        TL-DR: on average a LoL-player has a total <<38 .. 76>> past games (in 3 week span)
               of which <<6 .. 46>> belong to the current role
               while (of the total) <<2 .. 20>> on the current champion
    """

    # Whether the current reallane is (1.) primary lane, (2.) secondary lane, or the result of autofill
    lanes = {
        'TOP': 0,
        'JUNGLE': 0,
        'MID': 0,
        'BOTTOM': 0,
        'SUPPORT': 0
    }

    # Activity and motivation (overall / in-lane)
    num_games = 0
    num_games_in_current_lane = 0
    consecutive_wins = 0
    consecutive_losses = 0
    winning = None  # Track for consecutive'ness
    previous_game_won = 0  # loss => -1, win => +1, no-info => 0

    # Post-game statistics
    participant_postgame_extraction_rules = {
        'gold_earned': lambda participant: participant['stats']['goldEarned'],
        'gold_spent': lambda participant: participant['stats']['goldSpent'],
        'gold_per_min_0_to_10': lambda participant: 0 if 'goldPerMinDeltas' not in participant['timeline'] or '0-10' not in participant['timeline']['goldPerMinDeltas'] else participant['timeline']['goldPerMinDeltas']['0-10'],
        'gold_per_min_10_to_20': lambda participant: 0 if 'goldPerMinDeltas' not in participant['timeline'] or '10-20' not in participant['timeline']['goldPerMinDeltas'] else participant['timeline']['goldPerMinDeltas']['10-20'],
        'gold_per_min_20_to_30': lambda participant: 0 if 'goldPerMinDeltas' not in participant['timeline'] or '20-30' not in participant['timeline']['goldPerMinDeltas'] else participant['timeline']['goldPerMinDeltas']['20-30'],
        'gold_per_min_30_to_40': lambda participant: 0 if 'goldPerMinDeltas' not in participant['timeline'] or '30-40' not in participant['timeline']['goldPerMinDeltas'] else participant['timeline']['goldPerMinDeltas']['30-40'],
        'damage_to_champions_total': lambda participant: participant['stats']['totalDamageDealtToChampions'],
        'damage_to_champions_truetype': lambda participant: participant['stats']['trueDamageDealtToChampions'],
        'damage_to_champions_physical': lambda participant: participant['stats']['physicalDamageDealtToChampions'],
        'damage_to_champions_magical': lambda participant: participant['stats']['magicDamageDealtToChampions'],
        'kills': lambda participant: participant['stats']['kills'],
        'assists': lambda participant: participant['stats']['assists'],
        'double_kills': lambda participant: participant['stats']['doubleKills'],
        'triple_kills': lambda participant: participant['stats']['tripleKills'],
        'quadra_kills': lambda participant: participant['stats']['quadraKills'],
        'penta_kills': lambda participant: participant['stats']['pentaKills'],
        'hexa_kills': lambda participant: participant['stats']['unrealKills'],
        'max_kill_num_multikill': lambda participant: participant['stats']['largestMultiKill'],
        'killing_sprees': lambda participant: participant['stats']['killingSprees'],
        'max_kill_num_killingspree': lambda participant: participant['stats']['largestKillingSpree'],
        'damage_taken_total': lambda participant: participant['stats']['totalDamageTaken'],
        'damage_taken_truetype': lambda participant: participant['stats']['trueDamageTaken'],
        'damage_taken_physical': lambda participant: participant['stats']['physicalDamageTaken'],
        'damage_taken_magical': lambda participant: participant['stats']['magicalDamageTaken'],
        'damage_taken_mitigated': lambda participant: participant['stats']['damageSelfMitigated'],
        'damage_taken_per_min_0_to_10': lambda participant: 0 if 'damageTakenPerMinDeltas' not in participant['timeline'] or '0-10' not in participant['timeline']['damageTakenPerMinDeltas'] else participant['timeline']['damageTakenPerMinDeltas']['0-10'],
        'damage_taken_per_min_10_to_20': lambda participant: 0 if 'damageTakenPerMinDeltas' not in participant['timeline'] or '10-20' not in participant['timeline']['damageTakenPerMinDeltas'] else participant['timeline']['damageTakenPerMinDeltas']['10-20'],
        'damage_taken_per_min_20_to_30': lambda participant: 0 if 'damageTakenPerMinDeltas' not in participant['timeline'] or '20-30' not in participant['timeline']['damageTakenPerMinDeltas'] else participant['timeline']['damageTakenPerMinDeltas']['20-30'],
        'damage_taken_per_min_30_to_40': lambda participant: 0 if 'damageTakenPerMinDeltas' not in participant['timeline'] or '30-40' not in participant['timeline']['damageTakenPerMinDeltas'] else participant['timeline']['damageTakenPerMinDeltas']['30-40'],
        'longest_time_living': lambda participant: participant['stats']['longestTimeSpentLiving'],
        'damage_healed': lambda participant: participant['stats']['totalHeal'],
        'targets_healed': lambda participant: participant['stats']['totalUnitsHealed'],
        'deaths': lambda participant: participant['stats']['deaths'],
        'wards_placed': lambda participant: participant['stats']['wardsPlaced'],
        'wards_killed': lambda participant: participant['stats']['wardsKilled'],
        'normal_wards_bought': lambda participant: participant['stats']['sightWardsBoughtInGame'],
        'control_wards_bought': lambda participant: participant['stats']['visionWardsBoughtInGame'],
        'player_score_rank': lambda participant: participant['stats']['totalScoreRank'],
        'player_score_total': lambda participant: participant['stats']['totalPlayerScore'],
        'player_score_objective': lambda participant: participant['stats']['objectivePlayerScore'],
        'player_score_combat': lambda participant: participant['stats']['combatPlayerScore'],
        'player_score_vision': lambda participant: participant['stats']['visionScore'],
        'damage_to_turrets_total': lambda participant: participant['stats']['damageDealtToTurrets'],
        'damage_to_pit_monsters_total': lambda participant: participant['stats']['damageDealtToObjectives'] - participant['stats']['damageDealtToTurrets'],
        'damage_to_creeps_and_wards_total': lambda participant: participant['stats']['totalDamageDealt'] - participant['stats']['totalDamageDealtToChampions'] - participant['stats']['damageDealtToObjectives'],
        'turrets_killed': lambda participant: participant['stats']['turretKills'],
        'inhibitors_killed': lambda participant: participant['stats']['inhibitorKills'],
        'damage_largest_criticalstrike': lambda participant: participant['stats']['largestCriticalStrike'],
        'minions_killed_total': lambda participant: participant['stats']['totalMinionsKilled'],
        'minions_killed_jungle': lambda participant: participant['stats']['neutralMinionsKilled'],
        'minions_killed_jungle_allyside': lambda participant: participant['stats']['neutralMinionsKilledTeamJungle'],
        'minions_killed_jungle_enemyside': lambda participant: participant['stats']['neutralMinionsKilledEnemyJungle'],
        'minions_killed_per_min_0_to_10': lambda participant: 0 if 'creepsPerMinDeltas' not in participant['timeline'] or '0-10' not in participant['timeline']['creepsPerMinDeltas'] else participant['timeline']['creepsPerMinDeltas']['0-10'],
        'minions_killed_per_min_10_to_20': lambda participant: 0 if 'creepsPerMinDeltas' not in participant['timeline'] or '10-20' not in participant['timeline']['creepsPerMinDeltas'] else participant['timeline']['creepsPerMinDeltas']['10-20'],
        'minions_killed_per_min_20_to_30': lambda participant: 0 if 'creepsPerMinDeltas' not in participant['timeline'] or '20-30' not in participant['timeline']['creepsPerMinDeltas'] else participant['timeline']['creepsPerMinDeltas']['20-30'],
        'minions_killed_per_min_30_to_40': lambda participant: 0 if 'creepsPerMinDeltas' not in participant['timeline'] or '30-40' not in participant['timeline']['creepsPerMinDeltas'] else participant['timeline']['creepsPerMinDeltas']['30-40'],
        'xp_gained_per_min_0_to_10': lambda participant: 0 if 'xpPerMinDeltas' not in participant['timeline'] or '0-10' not in participant['timeline']['xpPerMinDeltas'] else participant['timeline']['xpPerMinDeltas']['0-10'],
        'xp_gained_per_min_10_to_20': lambda participant: 0 if 'xpPerMinDeltas' not in participant['timeline'] or '10-20' not in participant['timeline']['xpPerMinDeltas'] else participant['timeline']['xpPerMinDeltas']['10-20'],
        'xp_gained_per_min_20_to_30': lambda participant: 0 if 'xpPerMinDeltas' not in participant['timeline'] or '20-30' not in participant['timeline']['xpPerMinDeltas'] else participant['timeline']['xpPerMinDeltas']['20-30'],
        'xp_gained_per_min_30_to_40': lambda participant: 0 if 'xpPerMinDeltas' not in participant['timeline'] or '30-40' not in participant['timeline']['xpPerMinDeltas'] else participant['timeline']['xpPerMinDeltas']['30-40'],
        'cc_score_applied_pre_mitigation': lambda participant: participant['stats']['totalTimeCrowdControlDealt'],
        'cc_score_applied_post_mitigation': lambda participant: participant['stats']['timeCCingOthers'],
        'scored_first_blood_kill': lambda participant: False if 'firstBloodKill' not in participant['stats'] else participant['stats']['firstBloodKill'],
        'scored_first_blood_assist': lambda participant: False if 'firstBloodAssist' not in participant['stats'] else participant['stats']['firstBloodAssist'],
        'scored_first_tower_kill': lambda participant: False if 'firstTowerKill' not in participant['stats'] else participant['stats']['firstTowerKill'],
        'scored_first_tower_assist': lambda participant: False if 'firstTowerAssist' not in participant['stats'] else participant['stats']['firstTowerAssist'],
        'scored_first_inhibitor_kill': lambda participant: False if 'firstInhibitorKill' not in participant['stats'] else participant['stats']['firstInhibitorKill'],
        'scored_first_inhibitor_assist': lambda participant: False if 'firstInhibitorAssist' not in participant['stats'] else participant['stats']['firstInhibitorAssist'],
        'damage_taken_diff_per_min_0_to_10': lambda participant: 0 if 'damageTakenDiffPerMinDeltas' not in participant['timeline'] or '0-10' not in participant['timeline']['damageTakenDiffPerMinDeltas'] else participant['timeline']['damageTakenDiffPerMinDeltas']['0-10'],
        'damage_taken_diff_per_min_10_to_20': lambda participant: 0 if 'damageTakenDiffPerMinDeltas' not in participant['timeline'] or '10-20' not in participant['timeline']['damageTakenDiffPerMinDeltas'] else participant['timeline']['damageTakenDiffPerMinDeltas']['10-20'],
        'damage_taken_diff_per_min_20_to_30': lambda participant: 0 if 'damageTakenDiffPerMinDeltas' not in participant['timeline'] or '20-30' not in participant['timeline']['damageTakenDiffPerMinDeltas'] else participant['timeline']['damageTakenDiffPerMinDeltas']['20-30'],
        'damage_taken_diff_per_min_30_to_40': lambda participant: 0 if 'damageTakenDiffPerMinDeltas' not in participant['timeline'] or '30-40' not in participant['timeline']['damageTakenDiffPerMinDeltas'] else participant['timeline']['damageTakenDiffPerMinDeltas']['30-40'],
        'minions_killed_diff_per_min_0_to_10': lambda participant: 0 if 'csDiffPerMinDeltas' not in participant['timeline'] or '0-10' not in participant['timeline']['csDiffPerMinDeltas'] else participant['timeline']['csDiffPerMinDeltas']['0-10'],
        'minions_killed_diff_per_min_10_to_20': lambda participant: 0 if 'csDiffPerMinDeltas' not in participant['timeline'] or '10-20' not in participant['timeline']['csDiffPerMinDeltas'] else participant['timeline']['csDiffPerMinDeltas']['10-20'],
        'minions_killed_diff_per_min_20_to_30': lambda participant: 0 if 'csDiffPerMinDeltas' not in participant['timeline'] or '20-30' not in participant['timeline']['csDiffPerMinDeltas'] else participant['timeline']['csDiffPerMinDeltas']['20-30'],
        'minions_killed_diff_per_min_30_to_40': lambda participant: 0 if 'csDiffPerMinDeltas' not in participant['timeline'] or '30-40' not in participant['timeline']['csDiffPerMinDeltas'] else participant['timeline']['csDiffPerMinDeltas']['30-40'],
        'xp_gained_diff_per_min_0_to_10': lambda participant: 0 if 'xpDiffPerMinDeltas' not in participant['timeline'] or '0-10' not in participant['timeline']['xpDiffPerMinDeltas'] else participant['timeline']['xpDiffPerMinDeltas']['0-10'],
        'xp_gained_diff_per_min_10_to_20': lambda participant: 0 if 'xpDiffPerMinDeltas' not in participant['timeline'] or '10-20' not in participant['timeline']['xpDiffPerMinDeltas'] else participant['timeline']['xpDiffPerMinDeltas']['10-20'],
        'xp_gained_diff_per_min_20_to_30': lambda participant: 0 if 'xpDiffPerMinDeltas' not in participant['timeline'] or '20-30' not in participant['timeline']['xpDiffPerMinDeltas'] else participant['timeline']['xpDiffPerMinDeltas']['20-30'],
        'xp_gained_diff_per_min_30_to_40': lambda participant: 0 if 'xpDiffPerMinDeltas' not in participant['timeline'] or '30-40' not in participant['timeline']['xpDiffPerMinDeltas'] else participant['timeline']['xpDiffPerMinDeltas']['30-40'],
        'champion_level': lambda participant: participant['stats']['champLevel']
    }
    postgame_stats_total = {statname: [] for statname in participant_postgame_extraction_rules.keys()}
    postgame_stats_in_current_lane = {statname: [] for statname in participant_postgame_extraction_rules.keys()}
    games_with_fighting = []

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
                if num_games <= max_games_lookback:
                    # Fetch match (and any missing result or timeline) if does not already exist
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
                        num_games -= 1
                        continue

                    # Lookup lane
                    champion_then = m_ref['champion']
                    lane_then = create_champion_lane_mapping(result_dict, timeline_dict)[champion_then]
                    if lane_then == reallane:
                        num_games_in_current_lane += 1
                    lanes[lane_then] += 1

                    # Ensure we have items_dictionary from static data or (preferably) cached in memory
                    historical_game_version = get_or_create_game_version(result_dict)
                    if historical_game_version.semver not in items_dictionaries:
                        # May throw ObjectDoesNotExist, in which case it bubbles up to previous function
                        static_data = StaticGameData.objects.get(game_version=historical_game_version)
                        items_dictionaries[historical_game_version.semver] = json.loads(static_data.items_data_json)

                    # Historically account ID may be different and UN-OBTAINABLE (pls riot) so we'll rely on champ
                    p_data = next(filter(lambda p: p['championId'] == champion_then, result_dict['participants']))

                    # Parse fight data
                    participated_fights = parse_fights_one_game(result_dict,
                                                                timeline_dict,
                                                                items_dictionaries[historical_game_version.semver],
                                                                p_data['participantId'])
                    games_with_fighting.append(participated_fights)

                    # Parse post-game aggregate data for both all-games and current-lane-games
                    postgame_stats = parse_participant_postgame_stats(p_data, participant_postgame_extraction_rules)
                    for statname, statvalue in postgame_stats.items():
                        postgame_stats_total[statname].append(statvalue)
                    if lane_then == reallane:
                        for statname, statvalue in postgame_stats.items():
                            postgame_stats_in_current_lane[statname].append(statvalue)

                    # Draw conclusions based on win/loss
                    victory = p_data['stats']['win']
                    if previous_game_won == 0:
                        previous_game_won = 1 if victory else -1
                    if winning is None:
                        winning = victory
                    elif winning:
                        if victory:
                            consecutive_wins += 1
                        else:
                            winning = False
                            consecutive_wins = 0  # Reset
                    else:
                        if not victory:
                            consecutive_losses += 1
                        else:
                            winning = True
                            consecutive_losses = 0  # Reset

        except RiotApiError as err:
            if err.response.status_code == 429:
                raise RiotApiError(err.response) from None
            elif err.response.status_code == 404:
                continue  # No matches found {week_i} weeks in past, keep checking since the timeframe is explicit
            else:
                print('Unexpected HTTP {} error when querying match history ({})'.format(err.response.status_code,
                                                                                         err.response.url.split('?')[0]))
    primary_lane = max(lanes.keys(), key=lambda lane_name: lanes[lane_name])
    secondary_lane = max((l for l in lanes.keys() if l != primary_lane), key=lambda lane_name: lanes[lane_name])
    aggressiveness_and_judgment = calc_participant_aggressiveness_and_judgment(games_with_fighting)
    history = {
        'lane_priority': 'primary' if reallane == primary_lane else 'secondary' if reallane == secondary_lane else 'autofill',
        'solo_ratio': aggressiveness_and_judgment['solo']['ratio'],
        'solo_aggro': aggressiveness_and_judgment['solo']['aggro'],
        'skirmish_ratio': aggressiveness_and_judgment['skirmish']['ratio'],
        'skirmish_aggro': aggressiveness_and_judgment['skirmish']['aggro'],
        'team_ratio': aggressiveness_and_judgment['team']['ratio'],
        'team_aggro': aggressiveness_and_judgment['team']['aggro'],
        'num_games': num_games,
        'num_games_in_current_lane': num_games_in_current_lane,
        'previous_game_won': previous_game_won,
        'consecutive_wins': consecutive_wins,
        'consecutive_losses': consecutive_losses,
    }
    for statname, stat_aggregate in postgame_stats_total.items():
        history['total_{}'.format(statname)] = sum(stat_aggregate) / len(stat_aggregate) if len(stat_aggregate) > 0 else 0
    for statname, stat_aggregate in postgame_stats_in_current_lane.items():
        history['lane_{}'.format(statname)] = sum(stat_aggregate) / len(stat_aggregate) if len(stat_aggregate) > 0 else 0
    return history


def get_stats_availability(account_id, champion_id, reallane, summonerspells_set, runes_set,
                           match_time, riotapi, region,
                           max_weeks_lookback, max_games_lookback):
    """
    player (i.e. no categorization, all historical matches)
    player in a specific role
    player as a specific character
    player using specific set of summonerspells
    player using specific set of runes
    player using specific set of items
    or any mixture of these
    """
    num_matches = 0
    num_matches_in_role = 0
    num_matches_as_champion = 0
    num_matches_with_summonerspells = 0
    num_matches_with_runes = 0

    week_in_ms = 7*24*60*60*1000
    for week_i in range(max_weeks_lookback):
        end_time = match_time - 1000 - (week_i * week_in_ms)  # Offset by 1s
        start_time = end_time - week_in_ms
        try:
            # Returns a maximum of 100 matches
            week_matchlist = riotapi.get_matchlist(region.name,
                                                   account_id,
                                                   end_time=end_time,
                                                   begin_time=start_time)
            for m_ref in week_matchlist.json()['matches']:
                num_matches += 1
                if m_ref['champion'] == champion_id:
                    num_matches_as_champion += 1
                # Request the match to know more (i.e. real-lane etc.)
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

                # Check if it is remake, don't count those
                if result_dict['gameDuration'] < 300:
                    continue

                # Check if lane is current one
                champion_then = m_ref['champion']
                lane_then = create_champion_lane_mapping(result_dict, timeline_dict)[champion_then]
                if lane_then != reallane:
                    num_matches_in_role += 1

                # Historically account ID may be different and UN-OBTAINABLE (pls riot) so we'll rely on champ
                p_data = next(filter(lambda p: p['championId'] == champion_then, result_dict['participants']))

                # Check if summoner-spells are current ones
                historical_summonerspells = {p_data['spell1Id'], p_data['spell2Id']}
                if historical_summonerspells == summonerspells_set:
                    num_matches_with_summonerspells += 1

                # Check if runes are current ones
                if {p_data['stats']['perk0'], p_data['stats']['perk1'], p_data['stats']['perk2'],
                    p_data['stats']['perk3'], p_data['stats']['perk4'], p_data['stats']['perk5']} == runes_set:
                    num_matches_with_runes += 1
        except RiotApiError as err:
            if err.response.status_code == 429:
                raise RiotApiError(err.response) from None
            elif err.response.status_code == 404:
                continue  # No matches found {week_i} weeks in past, keep checking since the timeframe is explicit
            else:
                print('Unexpected HTTP {} error when querying match history ({})'.format(err.response.status_code,
                                                                                         err.response.url.split('?')[0]))
    return {
        'num_matches': num_matches,
        'num_matches_in_role': num_matches_in_role,
        'num_matches_as_champion': num_matches_as_champion,
        'num_matches_with_summonerspells': num_matches_with_summonerspells,
        'num_matches_with_runes': num_matches_with_runes
    }
