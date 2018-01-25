from django.urls import re_path
from django.shortcuts import render
from .views.ratelimit import ratelimit_endpoints, ratelimit_quota_graph


urlpatterns = [
    re_path(r'^/?$', lambda request: render(request, 'monitor.html')),
    re_path(r'^/ratelimit_endpoints$', ratelimit_endpoints),
    re_path(r'^/(?P<ratelimit_endpoint>\w+)/ratelimit_quota.png', ratelimit_quota_graph),
]
