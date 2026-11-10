"""report.py against a small hand-built database.

Episodes (pair, route, best $):
    ETH dex>dex        0.004  (dust)
    ETH dex>dex        0.020  (dust)
    ETH dex>dex        0.668  18s   <- best
    ETH kraken>dex     0.537   1s
    BTC dex>dex        0.130   6s
    BTC dex>dex        0.060   2s   (old row, no prices)
With the default $0.05 filter: 2 dust, 4 kept.
"""
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import db
import report
from tracker import Episode, MinuteRow


def ep(pair, route, usd, secs, ts, **px):
    return Episode(route, pair, 1, ts, 1, ts + secs, max(1, int(secs // 2) + 1), usd, usd,
                   "buyvenue", "sellvenue", **px)


class TestReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "r.sqlite")
        self.conn = db.connect(self.path)
        db.insert_episodes(self.conn, [
            ep("ETH", "dex>dex", 0.004, 0, 1000.0),
            ep("ETH", "dex>dex", 0.020, 0, 1100.0),
            ep("ETH", "dex>dex", 0.668, 18, 1200.0, best_mid=2130.0, best_buy_px=2120.0, best_sell_px=2134.2),
            ep("ETH", "kraken>dex", 0.537, 1, 1300.0, best_mid=2130.0, best_buy_px=2131.0, best_sell_px=2150.5),
            ep("BTC", "dex>dex", 0.130, 6, 1400.0, best_mid=74900.0, best_buy_px=74890.0, best_sell_px=75000.0),
            ep("BTC", "dex>dex", 0.060, 2, 1500.0),
        ])
        db.insert_minutes(self.conn, [MinuteRow(960, "ETH", "dex>dex", 30, 3, 0.668, -0.5)])

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_dust_count(self):
        self.assertEqual(report.dust_count(self.conn, 0, 0.05), 2)
        self.assertEqual(report.dust_count(self.conn, 0, 0.0), 0)
        self.assertEqual(report.dust_count(self.conn, 1050.0, 0.05), 1)   # time filter

    def test_episode_summary_skips_dust(self):
        rows = {(p, r): (n, med, mx, tot) for p, r, n, med, mx, tot in report.episode_summary(self.conn, 0, 0.05)}
        n, med, mx, tot = rows[("ETH", "dex>dex")]
        self.assertEqual((n, med, mx), (1, 18, 18))          # the two dust episodes are gone
        self.assertAlmostEqual(tot, 0.668)
        n, med, mx, tot = rows[("BTC", "dex>dex")]
        self.assertEqual((n, med, mx), (2, 4, 6))            # median of 2s and 6s
        self.assertAlmostEqual(tot, 0.19)

    def test_episode_summary_sorted_by_total(self):
        totals = [r[5] for r in report.episode_summary(self.conn, 0, 0.05)]
        self.assertEqual(totals, sorted(totals, reverse=True))

    def test_top_episodes_order_limit_and_prices(self):
        top = report.top_episodes(self.conn, 0, 0.05, 2)
        self.assertEqual([round(r[5], 3) for r in top], [0.668, 0.537])
        self.assertEqual(top[0][8], 2120.0)           # best_buy_px
        self.assertEqual(top[0][12], 2130.0)          # best_mid

    def test_main_prints_and_handles_old_rows(self):
        buf = io.StringIO()
        with mock.patch.object(report.config, "DB_PATH", self.path), redirect_stdout(buf):
            report.main(["--top", "10"])
        out = buf.getvalue()
        self.assertIn("EPISODES >= $0.05  (2 smaller ones skipped as dust)", out)
        self.assertIn("sellvenue @2,134.20", out)
        self.assertIn("buyvenue @?", out)             # old row without prices
        self.assertNotIn("0.004", out)

    def test_main_closes_the_database(self):
        # Windows refuses to delete an open file, so a leaked connection breaks cleanup there.
        opened = []
        real_connect = report.sqlite3.connect

        def spy(path):
            conn = real_connect(path)
            opened.append(conn)
            return conn

        with mock.patch.object(report.config, "DB_PATH", self.path), \
                mock.patch.object(report.sqlite3, "connect", spy), redirect_stdout(io.StringIO()):
            report.main([])
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):   # closed connections refuse queries
            opened[0].execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
