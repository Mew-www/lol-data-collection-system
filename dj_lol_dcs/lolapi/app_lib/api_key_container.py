class ApiKeyContainer:
    """Container for API-key and respective app-rate-limit(s); Encapsulates and aggregates them together"""

    def __init__(self, api_key, app_rate_limits):
        self.__api_key = api_key
        self.__app_rate_limits = app_rate_limits

    def get_api_key(self):
        return self.__api_key

    def get_app_rate_limits(self):
        return self.__app_rate_limits

    def change_key(self, new_api_key, new_app_rate_limits):
        self.__api_key = new_api_key
        self.__app_rate_limits = new_app_rate_limits
