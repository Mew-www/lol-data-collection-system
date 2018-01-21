import sys
import os
import csv
import datetime
import re
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import textwrap


matplotlib.use("Agg")


def main():
    if len(sys.argv) < 3:
        print("Usage: python monitor_gathering.py LogFileFolder GraphOutputFolder")
        sys.exit(1)
    log_folder = sys.argv[1]
    output_folder = sys.argv[2]

    if not os.path.isdir(log_folder):
        print("Log folder was not found")

    all_lines = []
    for f in os.listdir(log_folder):
        with open(os.path.join(log_folder, f), 'r') as fh:
            csv_reader = csv.reader(fh, delimiter=',', quotechar='"')
            for row in csv_reader:
                all_lines.append(row)

    # Sort log lines per timestamp
    all_lines = sorted(all_lines, key=lambda line: float(line[0]))

    # Distribute lines per each method+region -key
    lines_per_key = {}
    for line in all_lines:
        # Unpack line data
        data_timestamp = datetime.datetime.utcfromtimestamp(float(line[0]))
        rate_limit_method_and_region = "{} {}".format((line[2] if line[2] != '' else "App ratelimit"), line[1])
        rate_limit_timeframe = int(line[3])
        rate_limit_count = int(line[4])
        rate_limit_max = int(line[5])
        # Reformat data per method+region -key
        key = "{} per {}s".format(rate_limit_method_and_region, rate_limit_timeframe)
        if key not in lines_per_key:
            lines_per_key[key] = {'y': [], 'x': [], 'y_limit': rate_limit_max}
        lines_per_key[key]['y'].append(rate_limit_count)
        lines_per_key[key]['x'].append(data_timestamp)

    clean_filename_regex = re.compile(r'[^\w\d]+')
    df = mdates.DateFormatter('%b %d')
    for key, data in lines_per_key.items():
        plt.stackplot(data['x'], data['y'], color='#ff1493')
        index_of_peak = data['y'].index(max(data['y']))
        plt.title('\n'.join(textwrap.wrap(
            "{} (peaked {}/{} on {})".format(key, data['y'][index_of_peak], data['y_limit'], data['x'][index_of_peak]),
            60)))
        axes = plt.gca()
        axes.set_ylim([0, data['y_limit']])
        axes.xaxis.set_major_formatter(df)
        plt.savefig(os.path.join(output_folder, clean_filename_regex.sub('_', key)))
        plt.close()


if __name__ == "__main__":
    main()
