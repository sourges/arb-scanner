import os
import sqlite3
import tempfile
import unittest

import db
from tracker import Episode, MinuteRow


V1_EPISODES = """CREATE TABLE episodes (id INTEGER PRIMARY KEY, pair TEXT, route TEXT,
    start_block INTEGER, end_block INTEGER, blocks INTEGER, samples INTEGER, start_ts REAL,
    seconds REAL, max_net_usd REAL, max_net_pct REAL, best_buy TEXT, best_sell TEXT);"""


class TestDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def test_insert_and_read_back(self):
        conn = db.connect(self.path)
        ep = Episode("dex>dex", "ETH", 10, 1000.0, 12, 1004.0, 3, 0.5, 0.5, "A", "B",
                     start_mid=2000.0, best_mid=1990.0, best_buy_px=1980.0, best_sell_px=2010.0)
        db.insert_episodes(conn, [ep])
        row = conn.execute("SELECT pair, route, blocks, seconds, max_net_usd, best_buy,"
                           " start_mid, best_mid, best_buy_px, best_sell_px FROM episodes").fetchone()
        self.assertEqual(row, ("ETH", "dex>dex", 3, 4.0, 0.5, "A", 2000.0, 1990.0, 1980.0, 2010.0))

        m = MinuteRow(60, "ETH", "dex>dex", 4, 1, 0.2, -0.4)
        db.insert_minutes(conn, [m])
        db.insert_minutes(conn, [m])               # same minute again replaces, no duplicate
        rows = conn.execute("SELECT samples, positives, avg_net_pct FROM minute_stats").fetchall()
        self.assertEqual(rows, [(4, 1, -0.1)])
        conn.close()

    def test_reconnect_keeps_data(self):
        conn = db.connect(self.path)
        db.insert_minutes(conn, [MinuteRow(60, "ETH", "k", 1, 0, -1.0, -1.0)])
        conn.close()
        conn = db.connect(self.path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM minute_stats").fetchone(), (1,))
        conn.close()


    def test_upgrades_v1_database_and_keeps_old_rows(self):
        # build a database the way v1 did (no price columns) with one episode in it
        old = sqlite3.connect(self.path)
        old.executescript(V1_EPISODES)
        old.execute("INSERT INTO episodes (pair, route, max_net_usd) VALUES ('ETH', 'dex>dex', 0.3)")
        old.commit()
        old.close()

        conn = db.connect(self.path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(episodes)")}
        self.assertTrue(set(db.EPISODE_PRICE_COLUMNS) <= cols)
        self.assertEqual(conn.execute("SELECT max_net_usd, best_mid FROM episodes").fetchone(), (0.3, None))
        db.insert_episodes(conn, [Episode("k", "BTC", 1, 0.0, 1, 0.0, 1, 0.1, 0.1, "A", "B", best_mid=70000.0)])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM episodes").fetchone(), (2,))
        conn.close()
        db.connect(self.path).close()          # connecting again must not try to re-add columns

    def test_file_database_uses_wal(self):
        conn = db.connect(self.path)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        conn.close()

    def test_open_reader_does_not_block_writes(self):
        writer = db.connect(self.path)
        reader = sqlite3.connect(self.path, timeout=0.2)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM episodes").fetchall()     # read transaction left open
        try:
            db.insert_episodes(writer, [Episode("k", "ETH", 1, 0.0, 1, 0.0, 1, 0.1, 0.1, "A", "B")])
        finally:
            reader.close()
        self.assertEqual(writer.execute("SELECT COUNT(*) FROM episodes").fetchone(), (1,))
        writer.close()

if __name__ == "__main__":
    unittest.main()
