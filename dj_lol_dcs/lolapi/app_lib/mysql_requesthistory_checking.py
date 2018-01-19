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
        self.dbh.close()

