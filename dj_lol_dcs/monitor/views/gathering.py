from django.http import HttpResponse, HttpResponseNotFound
from django.views.decorators.http import require_http_methods

from django.conf import settings
import os
import json
from lolapi.models import HistoricalMatch


@require_http_methods(['GET'])
def gatherers_activity_timestamps(request):
    """Returns gatherers' logged names with their modified timestamps. As JSON object {name1: epoch_timestamp1, ..}."""

    if not os.path.exists(settings.RATELIMIT_LOG_PATH) or len(os.listdir(settings.RATELIMIT_LOG_PATH)) == 0:
        return HttpResponse(json.dumps({}))

    return HttpResponse(json.dumps({
        filename.split('.')[0]: os.path.getmtime(os.path.join(settings.RATELIMIT_LOG_PATH, filename)) for
        filename in
        os.listdir(settings.RATELIMIT_LOG_PATH)}))


@require_http_methods(['GET'])
def gathered_data_summary(request):
    """Returns common data summary information. As JSON object {games_total: .., etc}"""

    all_saved_matches = HistoricalMatch.objects.all()

    return HttpResponse(json.dumps({
        'matches': {
            'regions': list(all_saved_matches.values_list('region__name', flat=True).distinct()),
            'total': int(all_saved_matches.count()),
            'master': int(all_saved_matches.filter(regional_tier_avg__contains='MASTER').count()),
            'diamond': int(all_saved_matches.filter(regional_tier_avg__contains='DIAMOND').count()),
            'platinum': int(all_saved_matches.filter(regional_tier_avg__contains='PLATINUM').count()),
            'gold': int(all_saved_matches.filter(regional_tier_avg__contains='GOLD').count()),
            'silver': int(all_saved_matches.filter(regional_tier_avg__contains='SILVER').count()),
            'bronze': int(all_saved_matches.filter(regional_tier_avg__contains='BRONZE').count())
        }
    }))
