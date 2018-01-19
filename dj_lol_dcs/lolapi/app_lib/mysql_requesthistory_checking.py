import MySQLdb as MDB


class MysqlRequestHistory:

    def __init__(self, user, passwd, db):
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

    def try_request(self, api_key, region, method, request_uri):
        self.__lock()
        self.__add_request_to_db(api_key, region, method, request_uri)
        self.__unlock()
