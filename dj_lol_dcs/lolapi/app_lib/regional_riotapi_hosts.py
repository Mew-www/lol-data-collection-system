class RegionalRiotapiHosts:
    """Region <=references=> Platform <=references=> Host; Platforms are multiple for NA1/NA"""
    __hosts = {
        "br1.api.riotgames.com":  {'platforms': ["BR1"],       'region': "BR"},
        "eun1.api.riotgames.com": {'platforms': ["EUN1"],      'region': "EUNE"},
        "euw1.api.riotgames.com": {'platforms': ["EUW1"],      'region': "EUW"},
        "jp1.api.riotgames.com":  {'platforms': ["JP1"],       'region': "JP"},
        "kr.api.riotgames.com":   {'platforms': ["KR"],        'region': "KR"},
        "la1.api.riotgames.com":  {'platforms': ["LA1"],       'region': "LAN"},
        "la2.api.riotgames.com":  {'platforms': ["LA2"],       'region': "LAS"},
        "na1.api.riotgames.com":  {'platforms': ["NA1", "NA"], 'region': "NA"},
        "oc1.api.riotgames.com":  {'platforms': ["OC1"],       'region': "OCE"},
        "tr1.api.riotgames.com":  {'platforms': ["TR1"],       'region': "TR"},
        "ru.api.riotgames.com":   {'platforms': ["RU"],        'region': "RU"},
        "pbe1.api.riotgames.com": {'platforms': ["PBE1"],      'region': "PBE"}
    }

    def get_host_by_platform(self, platform):
        """This could be one-liner (using next's default argument), but more explicit using StopIteration instead"""
        try:
            matching_host = next(host for host, ref in self.__hosts.items() if (platform in ref['platforms']))
            return matching_host
        except StopIteration:
            return None

    def get_host_by_region(self, region):
        """This could be one-liner (using next's default argument), but more explicit using StopIteration instead"""
        try:
            matching_host = next(host for host, ref in self.__hosts.items() if ref['region'] == region)
            return matching_host
        except StopIteration:
            return None

    def get_region_by_platform(self, platform):
        """This could be one-liner (using next's default argument), but more explicit using StopIteration instead"""
        try:
            matching_region = next(ref['region'] for h, ref in self.__hosts.items() if (platform in ref['platforms']))
            return matching_region
        except StopIteration:
            return None
