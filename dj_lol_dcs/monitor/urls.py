from django.urls import re_path
from django.shortcuts import render
from .views.ratelimit import ratelimit_endpoints, ratelimit_quota_graph
from .views.gathering import gatherers_activity_timestamps, gathered_data_summary


# /monitor
urlpatterns = [
    re_path(r'^/?$', lambda request: render(request, 'monitor.html')),
    re_path(r'^/ratelimit/endpoints$', ratelimit_endpoints),
    re_path(r'^/ratelimit/(?P<ratelimit_endpoint>\w+)/quota.png$', ratelimit_quota_graph),
    re_path(r'^/gathering/activity$', gatherers_activity_timestamps),
    re_path(r'^/gathering/data/summary$', gathered_data_summary)
]
