from sqlalchemy import create_engine
from sqlalchemy.sql import text
import os
import pandas as pd
import numpy as np
import json


def db_query_matches_into_df(partial_tier_name):
    """
        Returns tuple:
        [0] pandas DataFrame containing matches of given tier, of latest patch
        [1] string SemVer of the latest patch
    """
    db_engine = create_engine('postgresql://{}:{}@localhost/{}'.format(os.environ['DJ_PG_USERNAME'],
                                                                       os.environ['DJ_PG_PASSWORD'],
                                                                       os.environ['DJ_PG_DBNAME']))
    with db_engine.connect() as conn:
        # Create queries
        matches_sql = """
            SELECT regional_tier_avg, match_result_json, match_timeline_json 
            FROM lolapi_historicalmatch
            INNER JOIN lolapi_gameversion AS game_version ON lolapi_historicalmatch.game_version_id = game_version.id
            WHERE
                game_version.semver = (
                    SELECT MAX(game_version.semver)
                    FROM lolapi_historicalmatch
                    INNER JOIN lolapi_gameversion AS game_version ON lolapi_historicalmatch.game_version_id = game_version.id
                )
                AND game_duration > (5*60) -- Filter out games that were remade using /remake command. 
                                           -- Those do not modify participants' ELO rating.
                AND regional_tier_avg LIKE '%%{}%%'
            """.format(partial_tier_name)
        game_version_sql = text("""
            SELECT MAX(game_version.semver)
            FROM lolapi_historicalmatch
            INNER JOIN lolapi_gameversion AS game_version ON lolapi_historicalmatch.game_version_id = game_version.id
            """)
        # Get and transform-where-necessary
        matches_df = pd.read_sql(matches_sql, conn)
        semver_str = conn.execute(game_version_sql).scalar()
        matches_df['match_result'] = np.vectorize(lambda r: json.loads(r))(matches_df['match_result_json'])
        del matches_df['match_result_json']
        matches_df['match_timeline'] = np.vectorize(lambda t: json.loads(t))(matches_df['match_timeline_json'])
        del matches_df['match_timeline_json']
        return matches_df, semver_str


def main():
    pass


if __name__ == "__main__":
    main()
