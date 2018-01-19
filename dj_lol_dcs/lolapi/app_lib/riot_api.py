from .exceptions import RiotApiError, RatelimitMismatchError

from operator import itemgetter

import requests
import json
import time


class RiotApi:

    def __init__(self, api_key_container, requesthistory, api_hosts, regional_endpoints):
        self.__api_key_container = api_key_container
        self.__api_hosts = api_hosts
        self.__endpoints = regional_endpoints
        self.__request_history = []
        self.__db_request_history = requesthistory

    def __check_app_rate_limits(self):
        configured_limits = self.__api_key_container.get_app_rate_limits()
        epoch_now = int(time.time())
        for limit in configured_limits:
            max_requests_in_timeframe, timeframe_size = limit
            timeframe_start = epoch_now - timeframe_size
            requests_done_in_timeframe = list(filter(lambda timestamp: timestamp >= timeframe_start,
                                                     self.__request_history))
            print("[RATE-LIMIT][{}/{}, in {} second timeframe]".format(
                len(requests_done_in_timeframe),
                max_requests_in_timeframe,
                timeframe_size))
            if len(requests_done_in_timeframe) >= max_requests_in_timeframe:
                return False, (timeframe_size - (epoch_now - requests_done_in_timeframe[-1]))
        return True, None

    def __validate_app_rate_limits(self, received_limits):
        configured_limits = self.__api_key_container.get_app_rate_limits()

        # Compare length
        if len(configured_limits) != len(received_limits):
            msg = 'Misconfiguration (number of limits) in {}: defined {}, received from API {}'.format(
                "app-rate-limits",
                json.dumps(configured_limits),
                json.dumps(received_limits))
            raise RatelimitMismatchError(msg)

        # Compare contents (sorted per seconds-interval-limit)
        for idx, limit in enumerate(sorted(received_limits, key=itemgetter(1))):
            if configured_limits[idx][1] != int(limit[1]):
                msg = 'Misconfiguration (interval mismatch) in {}: defined {}, received from API {}'.format(
                    "app-rate-limits",
                    json.dumps(configured_limits),
                    json.dumps(received_limits))
                raise RatelimitMismatchError(msg)

            if configured_limits[idx][0] != int(limit[0]):
                msg = 'Misconfiguration (max-requests mismatch) in {}: defined {}, received from API {}'.format(
                    "app-rate-limits",
                    json.dumps(configured_limits),
                    json.dumps(received_limits))
                raise RatelimitMismatchError(msg)

    def __get(self, url, api_key_container, region, method):
        # Check rate-limit quotas, catches first full quota
        ok, wait_seconds = self.__check_app_rate_limits()
        while not ok:
            time.sleep(wait_seconds)
            # Re-check in case if multiple quotas full simultaneously
            ok, wait_seconds = self.__check_app_rate_limits()

        # Update request history and do request
        self.__request_history.append(int(time.time()))
        self.__db_request_history.try_request(api_key_container, region, method, url)
        response = requests.get(url)

        # Check response status
        if response.status_code != 200:
            raise RiotApiError(response)

        # Confirm app-rate-limit(s); Received format e.g. "10:1,100:10,6000:600,36000:3600" => transform to [[n,s], ..]
        received_app_rate_limits = [l.split(':') for l in response.headers['X-App-Rate-Limit'].split(',')]
        self.__validate_app_rate_limits(received_app_rate_limits)

        return response

    def get_summoner(self, region_name, name):
        return self.__get(self.__endpoints.SUMMONER_BY_NAME(self.__api_hosts.get_host_by_region(region_name),
                                                            name,
                                                            self.__api_key_container.get_api_key()),
                          self.__api_key_container,
                          region_name,
                          '/lol/summoner/v3/summoners/by-name/{summonerName}')

    def get_tiers(self, region_name, summoner_id):
        return self.__get(self.__endpoints.TIERS_BY_SUMMONER_ID(self.__api_hosts.get_host_by_region(region_name),
                                                                summoner_id,
                                                                self.__api_key_container.get_api_key()),
                          self.__api_key_container,
                          region_name,
                          'leagues-v3 endpoints')

    def get_active_match(self, region_name, summoner_id):
        return self.__get(self.__endpoints.SPECTATOR_BY_SUMMONER_ID(self.__api_hosts.get_host_by_region(region_name),
                                                                    summoner_id,
                                                                    self.__api_key_container.get_api_key()),
                          self.__api_key_container,
                          region_name,
                          'All other endpoints')

    def get_matchlist(self, region_name, account_id):
        return self.__get(self.__endpoints.MATCHLIST_BY_ACCOUNT_ID(self.__api_hosts.get_host_by_region(region_name),
                                                                   account_id,
                                                                   self.__api_key_container.get_api_key()),
                          self.__api_key_container,
                          region_name,
                          '/lol/match/v3/matchlists/by-account/{accountId}')

    def get_match_result(self, platform_name, match_id):
        return self.__get(self.__endpoints.MATCH_BY_MATCH_ID(self.__api_hosts.get_host_by_platform(platform_name),
                                                             match_id,
                                                             self.__api_key_container.get_api_key()),
                          self.__api_key_container,
                          self.__api_hosts.get_region_by_platform(platform_name),
                          '/lol/match/v3/[matches,timelines]')

    def get_match_timeline(self, platform_name, match_id):
        return self.__get(self.__endpoints.TIMELINE_BY_MATCH_ID(self.__api_hosts.get_host_by_platform(platform_name),
                                                                match_id,
                                                                self.__api_key_container.get_api_key()),
                          self.__api_key_container,
                          self.__api_hosts.get_region_by_platform(platform_name),
                          '/lol/match/v3/[matches,timelines]')
