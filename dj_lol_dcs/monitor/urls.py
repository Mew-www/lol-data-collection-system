from django.urls import re_path
from django.shortcuts import render


urlpatterns = [
    re_path(r'^/?$', lambda request: render(request, 'monitor.html')),

]
