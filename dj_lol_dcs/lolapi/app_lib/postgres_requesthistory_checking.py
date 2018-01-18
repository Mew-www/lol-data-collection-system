import psycopg2


class PostgresRequestHistory:

    def __init__(self, postgres_dsn):
        self.dbh = psycopg2.connect(postgres_dsn)
        self.cursor = self.dbh.cursor()
        self.cursor.execute('CREATE TABLE IF NOT EXISTS RequestHistory ('
                            + 'id SERIAL PRIMARY KEY, '
                            + 'at_time Timestamp DEFAULT CURRENT_TIMESTAMP, '
                            + 'api_key Varchar(255) NOT NULL, '
                            + 'region_name Varchar(255) NOT NULL, '
                            + 'method_name Varchar(255) NOT NULL, '
                            + 'request_uri Varchar(510) NOT NULL '
                            + ');')
        self.dbh.commit()

