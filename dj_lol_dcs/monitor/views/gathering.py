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

    all_matches = HistoricalMatch.objects.all()
    spanned_regions = list(all_matches.values_list('region__name', flat=True).distinct())
    matches_per_region = {
        r: {
            'total': int(all_matches.filter(region__name=r).count()),
            'master': int(all_matches.filter(region__name=r).filter(regional_tier_avg__contains='MASTER').count()),
            'diamond': int(all_matches.filter(region__name=r).filter(regional_tier_avg__contains='DIAMOND').count()),
            'platinum': int(all_matches.filter(region__name=r).filter(regional_tier_avg__contains='PLATINUM').count()),
            'gold': int(all_matches.filter(region__name=r).filter(regional_tier_avg__contains='GOLD').count()),
            'silver': int(all_matches.filter(region__name=r).filter(regional_tier_avg__contains='SILVER').count()),
            'bronze': int(all_matches.filter(region__name=r).filter(regional_tier_avg__contains='BRONZE').count())
        } for
        r in
        spanned_regions}

    return HttpResponse(json.dumps({
        'matches_total': int(all_matches.count()),
        'matches_per_region': matches_per_region,
    }))
