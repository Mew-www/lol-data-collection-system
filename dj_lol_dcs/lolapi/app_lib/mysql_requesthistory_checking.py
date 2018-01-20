import MySQLdb as MDB
from warnings import filterwarnings
import time
import csv
import os


# Don't print warnings (i.e. "TABLE ALREADY EXISTS" at the beginning)
filterwarnings('ignore', category=MDB.Warning)


class MysqlRequestHistory:

    def __init__(self, user, passwd, db, logfile_location=None):
        """Saves requests as-per rate limit groups (region+method combination) and prints or logs them (as csv)"""
        self.logfile_location = logfile_location
        self.dbh = MDB.connect(
            host="localhost",
            user=user,
            passwd=passwd,
            db=db,
            use_unicode=True,
            charset="utf8"
        )
        self.cursor = self.dbh.cursor()
        self.dbh.set_character_set('utf8')
        self.cursor.execute('SET NAMES utf8;')
        self.cursor.execute('SET CHARACTER SET utf8;')
        self.cursor.execute('SET character_set_connection=utf8;')

        self.cursor.execute('CREATE TABLE IF NOT EXISTS RequestHistory ('
                            + 'id Integer NOT NULL AUTO_INCREMENT, '
                            + 'at_time Datetime NOT NULL DEFAULT CURRENT_TIMESTAMP, '
                            + 'api_key Varchar(255) NOT NULL, '
                            + 'region_name Varchar(255) NOT NULL, '
                            + 'method_name Varchar(255) NOT NULL, '
                            + 'request_uri Varchar(510) NOT NULL, '
                            + 'PRIMARY KEY (id)'
                            + ');')
        self.dbh.commit()

    def __lock(self):
        self.cursor.execute("LOCK TABLES RequestHistory WRITE")
        self.dbh.commit()

    def __check_rate_limits(self, applied_rate_limits_with_region_and_method):
        """applied_rate_limits_with_region_and_method is modified
               from standard [[max-requests, timeframe-size], ..]
               to structure [[max-requests, timeframe-size, region, method], ..]
           allowing us to retrieve requests from db to highest timeframe size, reducing db queries
        """

        # Find longest timeframe/period (and therefore the period containing all sub-periods) reducing DB queries to 1
        highest_timeframe_size = max(map(lambda ratelimit: ratelimit[1], applied_rate_limits_with_region_and_method))

        # DB query the requests within the longest rate-limited period
        self.cursor.execute("SELECT UNIX_TIMESTAMP(at_time), region_name, method_name FROM RequestHistory"
                            + " WHERE UNIX_TIMESTAMP(at_time) > (UNIX_TIMESTAMP()-%s)"
                            + " ORDER BY UNIX_TIMESTAMP(at_time) DESC", (highest_timeframe_size,))
        relevant_request_history = list(map(lambda row: {'time': row[0], 'region': row[1], 'method': row[2]},
                                            self.cursor.fetchall()))

        # Make comparisons
        epoch_now = int(time.time())
        for limit in applied_rate_limits_with_region_and_method:
            max_requests_in_timeframe = int(limit[0])
            timeframe_size            = int(limit[1])
            region                    = str(limit[2])
            method                    = str(limit[3]) if limit[3] is not None else None
            timeframe_start = epoch_now - timeframe_size
            if method is None:
                requests_done_in_timeframe = list(
                    filter(lambda r: r['time'] >= timeframe_start and r['region'] == region,
                           relevant_request_history)
                )
            else:
                requests_done_in_timeframe = list(
                    filter(lambda r: r['time'] >= timeframe_start and r['region'] == region and r['method'] == method,
                           relevant_request_history)
                )
            if self.logfile_location is None:
                print("[RATE-LIMIT][{}][{}][{}/{}, in {} second timeframe]".format(
                    region,
                    method,
                    len(requests_done_in_timeframe),
                    max_requests_in_timeframe,
                    timeframe_size))
            else:
                os.makedirs(os.path.dirname(self.logfile_location), exist_ok=True)
                with open(self.logfile_location, 'a', newline='') as fh:
                    csv_writer = csv.writer(fh, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
                    csv_writer.writerow([time.time(),
                                         region,
                                         method,
                                         timeframe_size,
                                         len(requests_done_in_timeframe),
                                         max_requests_in_timeframe])
            if len(requests_done_in_timeframe) >= max_requests_in_timeframe:
                return False, (timeframe_size - (epoch_now - requests_done_in_timeframe[-1]['time']))
        return True, None

    def __add_request_to_db(self, api_key, region, method, request_uri):
        escaped_sqlstr = ('INSERT INTO RequestHistory ('
                          + 'api_key, '
                          + 'region_name, '
                          + 'method_name, '
                          + 'request_uri'
                          + ") VALUES (%s, %s, %s, %s)")
        self.cursor.execute(escaped_sqlstr, (api_key, region, method, request_uri))
        self.dbh.commit()

    def __unlock(self):
        self.cursor.execute("UNLOCK TABLES")
        self.dbh.commit()

    def permit_request(self, api_key_container, region, method, request_uri):
        api_key = api_key_container.get_api_key()
        # from standard [[max-requests, timeframe-size], ..]
        # to structure [[max-requests, timeframe-size, region, method], ..]
        app_rate_limits = [
            rl + [region, None] for
            rl in
            api_key_container.get_app_rate_limits()
        ]
        request_specific_method_rate_limits = [
            rl + [region, method] for
            rl in
            api_key_container.get_method_rate_limits().get_rate_limit(method, region)
        ]
        # Lock tables to prevent a race condition between multiple active scripts
        self.__lock()
        # Check rate-limit quotas, catches first full quota
        ok, wait_seconds = self.__check_rate_limits(app_rate_limits + request_specific_method_rate_limits)
        while not ok:
            time.sleep(wait_seconds)
            # Re-check in case if multiple quotas full simultaneously
            ok, wait_seconds = self.__check_rate_limits(app_rate_limits + request_specific_method_rate_limits)
        self.__add_request_to_db(api_key, region, method, request_uri)
        self.__unlock()
