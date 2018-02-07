

# API-response HTTP exceptions
##
class RiotApiError(Exception):
    """<base class> Raise when RiotGames API returns non-2xx response"""
    def __init__(self, requests_response):
        msg = "HTTP Error {}".format(requests_response.status_code)
        self.message = msg
        self.response = requests_response
        super(RiotApiError, self).__init__(msg)


# Exceptions that indicate "something requires re-configuring"
##
class ConfigurationError(Exception):
    """<base class> Raise when something wrongly configured, presumably fatal."""
    pass


class RatelimitMismatchError(ConfigurationError):
    """Raise when validating ratelimit (configured <=> api_response.headers) fails."""
    pass


# Miscellaneous exceptions
##
class MatchTakenError(Exception):
    """Raise when "match already being observed for gathering purposes by another process"""
    pass
