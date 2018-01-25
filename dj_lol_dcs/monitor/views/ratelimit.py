from django.http import HttpResponse, HttpResponseNotFound
from django.views.decorators.http import require_http_methods

import hashlib
import json
from django.conf import settings
import os
import csv
import datetime
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import textwrap


@require_http_methods(['GET'])
def ratelimit_endpoints(request):
    """Returns endpoint(hash)s to use as url"""

    def hash_ratelimit_type(a_str_with_whitespace_and_stuff):
        """Just normalize the few ratelimit types there are, using md5 hash. Hash <=> endpoint. Not about security."""
        return hashlib.md5(a_str_with_whitespace_and_stuff).hexdigest()

    if not os.path.exists(settings.RATELIMIT_LOG_PATH):
        return HttpResponse(json.dumps({}))

    # Find each method+region -key, that translates to an endpoint
    existing_ratelimit_keys = []
    for f in os.listdir(settings.RATELIMIT_LOG_PATH):
        with open(os.path.join(settings.RATELIMIT_LOG_PATH, f), 'r') as fh:
            csv_reader = csv.reader(fh, delimiter=',', quotechar='"')
            for row in csv_reader:
                rate_limit_method_and_region = "{} {}".format((row[2] if row[2] != '' else "App ratelimit"), row[1])
                rate_limit_timeframe = int(row[3])
                key = "{} per {}s".format(rate_limit_method_and_region, rate_limit_timeframe)
                if key not in existing_ratelimit_keys:
                    existing_ratelimit_keys.append(key)

    ratelimit_types_to_hash = {k: hash_ratelimit_type(k.encode('utf8')) for k in existing_ratelimit_keys}

    return HttpResponse(json.dumps(ratelimit_types_to_hash))


@require_http_methods(['GET'])
def ratelimit_quota_graph(request, ratelimit_endpoint):
    """Returns .png"""

    def hash_ratelimit_type(a_str_with_whitespace_and_stuff):
        """Just normalize the few ratelimit types there are, using md5 hash. Hash <=> endpoint. Not about security."""
        return hashlib.md5(a_str_with_whitespace_and_stuff).hexdigest()

    if not os.path.exists(settings.RATELIMIT_LOG_PATH):
        return HttpResponseNotFound("No monitored target endpoints overall")

    # Find each method+region -key, that translates to an endpoint
    existing_ratelimit_keys = []
    for f in os.listdir(settings.RATELIMIT_LOG_PATH):
        with open(os.path.join(settings.RATELIMIT_LOG_PATH, f), 'r') as fh:
            csv_reader = csv.reader(fh, delimiter=',', quotechar='"')
            for row in csv_reader:
                rate_limit_method_and_region = "{} {}".format((row[2] if row[2] != '' else "App ratelimit"), row[1])
                rate_limit_timeframe = int(row[3])
                key = "{} per {}s".format(rate_limit_method_and_region, rate_limit_timeframe)
                if key not in existing_ratelimit_keys:
                    existing_ratelimit_keys.append(key)

    # Check the target exists amongst log lines
    ratelimit_types_to_hash = {k: hash_ratelimit_type(k.encode('utf8')) for k in existing_ratelimit_keys}
    if ratelimit_endpoint not in ratelimit_types_to_hash.values():
        return HttpResponseNotFound("No such monitored target endpoint {}".format(ratelimit_endpoint))
    real_key = next(filter(lambda k: ratelimit_types_to_hash[k] == ratelimit_endpoint, ratelimit_types_to_hash.keys()))

    # Save those lines, for each ratelimit log file
    meaningful_lines = []
    for f in os.listdir(settings.RATELIMIT_LOG_PATH):
        with open(os.path.join(settings.RATELIMIT_LOG_PATH, f), 'r') as fh:
            csv_reader = csv.reader(fh, delimiter=',', quotechar='"')
            for row in csv_reader:
                rate_limit_method_and_region = "{} {}".format((row[2] if row[2] != '' else "App ratelimit"), row[1])
                rate_limit_timeframe = int(row[3])
                key = "{} per {}s".format(rate_limit_method_and_region, rate_limit_timeframe)
                if real_key == key:
                    meaningful_lines.append(row)

    # Sort log lines per timestamp
    meaningful_lines.sort(key=lambda l: float(l[0]))

    # Transform lines to x/y/limit_y
    graph_data = {'y': [], 'x': [], 'y_limit': meaningful_lines[0][5]}  # row[5] is the ratelimit_max
    for line in meaningful_lines:
        # Unpack line data
        data_timestamp = datetime.datetime.utcfromtimestamp(float(line[0]))
        rate_limit_count = int(line[4])
        graph_data['x'].append(data_timestamp)
        graph_data['y'].append(rate_limit_count)

    df = mdates.DateFormatter('%b %d')
    plt.stackplot(graph_data['x'], graph_data['y'], color='#ff1493')
    index_of_peak = graph_data['y'].index(max(graph_data['y']))
    plt.title('\n'.join(textwrap.wrap(
        "{} (peaked {}/{} on {})".format(
            real_key,
            graph_data['y'][index_of_peak],
            graph_data['y_limit'],
            graph_data['x'][index_of_peak]),
        60)))
    axes = plt.gca()
    axes.set_ylim([0, int(graph_data['y_limit'])])
    axes.xaxis.set_major_formatter(df)
    res = HttpResponse(content_type='text/plain')
    plt.savefig(res)
    plt.close()
    return res
