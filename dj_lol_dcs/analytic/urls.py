from django.urls import re_path
from django.shortcuts import render

# /monitor
urlpatterns = [
    re_path(r'^/?$', lambda request: render(request, 'query.html')),
]
