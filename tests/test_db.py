import os
import tempfile
import unittest

import db
from tracker import Episode, MinuteRow


class TestDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def test_insert_and_read_back(self):
        conn = db.connect(self.path)
        ep = Episode("dex>dex", "ETH", 10, 1000.0, 12, 1004.0, 3, 0.5, 0.5, "A", "B")
        db.insert_episodes(conn, [ep])
        row = conn.execute("SELECT pair, route, blocks, seconds, max_net_usd, best_buy FROM episodes").fetchone()
        self.assertEqual(row, ("ETH", "dex>dex", 3, 4.0, 0.5, "A"))

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


if __name__ == "__main__":
    unittest.main()
