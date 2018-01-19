from .exceptions import ConfigurationError


class MethodRateLimits:
    """A mapping for [[],[],..] ratelimit <=> region + method"""

    def __init__(self, method_to_ratelimit_or_method_to_region_to_ratelimit_dict):
        """Either {'method': [ratelimit], or 'method': {'regionA': [ratelimit], 'regionB': [], ...}}"""
        self.__methods = method_to_ratelimit_or_method_to_region_to_ratelimit_dict

    def get_rate_limit(self, method, region):
        if method not in self.__methods:
            raise ConfigurationError('Non-configured method {}'.format(method))
        if isinstance(self.__methods[method], list):
            return self.__methods[method]
        else:
            if region not in self.__methods[method]:
                raise ConfigurationError('Non-configured region {} for method {}'.format(region, method))
            return self.__methods[method][region]


class ApiKeyContainer:
    """Container for API-key and respective app-rate-limit(s); Encapsulates and aggregates them together"""

    def __init__(self, api_key, app_rate_limits, method_rate_limits):
        self.__api_key = api_key
        self.__app_rate_limits = app_rate_limits
        self.__method_rate_limits = method_rate_limits

    def get_api_key(self):
        return self.__api_key

    def get_app_rate_limits(self):
        return self.__app_rate_limits

    def get_method_rate_limits(self):
        return self.__method_rate_limits

    def change_key(self, new_api_key, new_app_rate_limits, new_method_rate_limits):
        self.__api_key = new_api_key
        self.__app_rate_limits = new_app_rate_limits
        self.__method_rate_limits = new_method_rate_limits
