"""report.py against a small hand-built database.

Episodes (pair, route, best $):
    ETH dex>dex        0.004  (dust)
    ETH dex>dex        0.020  (dust)
    ETH dex>dex        0.668  18s   <- best
    ETH kraken>dex     0.537   1s
    BTC dex>dex        0.130   6s
    BTC dex>dex        0.060   2s   (old row, no prices)
With the default $0.05 filter: 2 dust, 4 kept.

Pegged recorder rows (cbBTC/WBTC, two minutes):
    ts 2000: pool A bid 0.9997 ask 0.9999, pool B bid 1.0002 ask 1.0004
             best bid 1.0002, best ask 0.9999 -> mid +0.5 bp, gap +3.0 bp
    ts 2060: one pool bid 0.9996 ask 0.9998
             mid 0.9997 -> -3.0 bp, gap 0.9996/0.9998 - 1 = -2.0 bp
    -> 2 minutes, avg mid -1.25, min -3.0, max +0.5, std 1.75, 1 minute with gap > 0, best +3.0
    A one-sided minute (ts 2120, ask missing) is not counted.
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
from recorder import PegQuote
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
        db.insert_pegged(self.conn, 2000.0, 1, [PegQuote("cbBTC/WBTC", "A", 0.9997, 0.9999),
                                                PegQuote("cbBTC/WBTC", "B", 1.0002, 1.0004)])
        db.insert_pegged(self.conn, 2060.0, 2, [PegQuote("cbBTC/WBTC", "A", 0.9996, 0.9998)])
        db.insert_pegged(self.conn, 2120.0, 3, [PegQuote("cbBTC/WBTC", "A", 0.9996, None)])

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

    def test_pegged_summary_hand_worked(self):
        (row,) = report.pegged_summary(self.conn, 0)
        pair, n, avg, lo, hi, std, pos, best = row
        self.assertEqual((pair, n, pos), ("cbBTC/WBTC", 2, 1))
        self.assertAlmostEqual(avg, -1.25, places=6)
        self.assertAlmostEqual(lo, -3.0, places=6)
        self.assertAlmostEqual(hi, 0.5, places=6)
        self.assertAlmostEqual(std, 1.75, places=6)
        self.assertAlmostEqual(best, 3.0, delta=0.001)       # 1.0002/0.9999 - 1 = 3.0003 bp
        self.assertEqual(report.pegged_summary(self.conn, 2050.0)[0][1], 1)   # time filter

    def test_main_prints_and_handles_old_rows(self):
        buf = io.StringIO()
        with mock.patch.object(report.config, "DB_PATH", self.path), redirect_stdout(buf):
            report.main(["--top", "10"])
        out = buf.getvalue()
        self.assertIn("EPISODES >= $0.05  (2 smaller ones skipped as dust)", out)
        self.assertIn("sellvenue @2,134.20", out)
        self.assertIn("buyvenue @?", out)             # old row without prices
        self.assertNotIn("0.004", out)
        self.assertIn("PEGGED PAIRS", out)
        self.assertIn("cbBTC/WBTC", out)

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


class TestProbeReport(unittest.TestCase):
    """Two probes, sizes $100/$1,000 (net %):
        ts 2000  ETH a>b   +0.60 / +0.20
        ts 3000  BTC c>d   +0.10 / can't fill
    $100: 2 probes, 2 filled, 2 > 0, median +0.35, best +0.60
    $1,000: 2 probes, 1 filled, 1 > 0, median +0.20, best +0.20
    top order by the $100 net: ETH first."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "p.sqlite")
        self.conn = db.connect(self.path)
        from probe import ProbeResult
        def pr(pair, b, s, size, net):
            usd_back = None if net is None else size * (1 + net / 100)
            return ProbeResult(pair, b, s, size, 0.05, usd_back, 0.0)
        db.insert_probes(self.conn, 3000.0, 2, "dex>dex", [pr("BTC", "c", "d", 100, 0.10), pr("BTC", "c", "d", 1000, None)])
        db.insert_probes(self.conn, 2000.0, 1, "dex>dex", [pr("ETH", "a", "b", 100, 0.60), pr("ETH", "a", "b", 1000, 0.20)])

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_probe_summary(self):
        (s100, s1000) = report.probe_summary(self.conn, 0)
        self.assertEqual(s100[:4], (100.0, 2, 2, 2))
        self.assertAlmostEqual(s100[4], 0.35)
        self.assertAlmostEqual(s100[5], 0.60)
        self.assertEqual(s1000[:4], (1000.0, 2, 1, 1))
        self.assertAlmostEqual(s1000[4], 0.20)

    def test_top_probes_order_and_cant_fill(self):
        top = report.top_probes(self.conn, 0, 10)
        self.assertEqual([t[1] for t in top], ["ETH a>b", "BTC c>d"])
        self.assertIsNone(top[1][2][1000.0])

    def test_main_prints_probe_section(self):
        buf = io.StringIO()
        with mock.patch.object(report.config, "DB_PATH", self.path), redirect_stdout(buf):
            report.main([])
        out = buf.getvalue()
        self.assertIn("SIZE PROBES", out)
        self.assertIn("ETH a>b", out)
        self.assertIn("can't fill", out)

    def test_database_without_probe_table(self):
        old = os.path.join(self.tmp.name, "old.sqlite")
        c = sqlite3.connect(old)
        c.execute("CREATE TABLE x (a)")
        self.assertEqual(report.probe_summary(c, 0), [])
        self.assertEqual(report.top_probes(c, 0, 5), [])
        c.close()

if __name__ == "__main__":
    unittest.main()
