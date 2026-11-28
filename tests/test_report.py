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
import time
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

    def test_episode_lengths(self):
        """samples = secs // 2 + 1 in ep(): 0.668 has 10, kraken 0.537 has 1 (a flash),
        BTC 0.130 has 4 and 0.060 has 2. Dust (< $0.05) is left out."""
        rows = report.episode_lengths(self.conn, 0, 0.05)
        self.assertEqual([r[:2] for r in rows], [("ETH", "dex>dex"), ("ETH", "kraken>dex"), ("BTC", "dex>dex")])
        eth, kraken, btc = rows
        self.assertEqual((eth[2], eth[4]), (0, 1))
        self.assertAlmostEqual(eth[5], 0.668)
        self.assertEqual((kraken[2], kraken[4]), (1, 0))
        self.assertAlmostEqual(kraken[3], 0.537)
        self.assertAlmostEqual(btc[5], 0.190)
        self.assertAlmostEqual(btc[6], 0.190)                 # no Aero leg: no haircut

    def test_episode_lengths_aero_haircut(self):
        e = Episode("dex>dex", "SOL", 1, 5000.0, 3, 5004.0, 3, 0.40, 0.40, "aerov3-ts10", "aero3-ts10")
        db.insert_episodes(self.conn, [e])
        (sol,) = [r for r in report.episode_lengths(self.conn, 4000.0, 0.05)]
        self.assertAlmostEqual(sol[5], 0.40)
        self.assertAlmostEqual(sol[6], 0.20)

    def test_parse_when(self):
        self.assertEqual(report.parse_when("1764281400"), 1764281400.0)
        self.assertEqual(report.parse_when("2026-11-27 17:10"),
                         time.mktime(time.strptime("2026-11-27 17:10", "%Y-%m-%d %H:%M")))
        self.assertEqual(report.parse_when("2026-11-27"),
                         time.mktime(time.strptime("2026-11-27", "%Y-%m-%d")))
        with self.assertRaises(Exception):
            report.parse_when("last tuesday")

    def test_since_limits_the_window(self):
        buf = io.StringIO()
        with mock.patch.object(report.config, "DB_PATH", self.path), redirect_stdout(buf):
            report.main(["--since", "1250"])
        out = buf.getvalue()
        self.assertIn("EPISODES BY LENGTH", out)
        lengths = out.split("EPISODES BY LENGTH")[1].split("\n\n")[0]
        self.assertNotIn("ETH dex>dex", lengths)              # its episodes start before 1250
        self.assertIn("BTC dex>dex", lengths)

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

    def test_long_venue_names_keep_their_columns(self):
        # Nov 17 report showed "@1,825.76pcsv3-100": the buy price ran into the sell column.
        db.insert_episodes(self.conn, [Episode(
            "kraken_reduced_maker>dex", "ETH", 1, 5000.0, 1, 5082.0, 42, 0.9, 0.9,
            "kraken_reduced_maker", "pcsv3-100",
            best_mid=1829.38, best_buy_px=1825.76, best_sell_px=1831.24)])
        buf = io.StringIO()
        with mock.patch.object(report.config, "DB_PATH", self.path), redirect_stdout(buf):
            report.main(["--top", "10"])
        line = next(l for l in buf.getvalue().splitlines() if "@1,825.76" in l)
        self.assertRegex(line, r"kraken_reduced_maker @1,825\.76 +pcsv3-100 @1,831\.24 +1,829\.38")

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

    def test_cap_refusals_counted(self):
        db.insert_probe_skip(self.conn, 2500.0, 5, "dex_arb_probe", "ETH a>b", 100.0, 0.30)
        db.insert_probe_skip(self.conn, 2600.0, 6, "dex_arb_probe", "BTC c>d", 100.0, 0.45)
        db.insert_probe_skip(self.conn, 2700.0, 7, "tri_arb_probe", "USDC>WETH>cbBTC>USDC", 100.0, 0.05)
        db.insert_probe_skip(self.conn, 100.0, 1, "dex_arb_probe", "old", 100.0, 9.0)   # before window
        n, best = report.skip_count(self.conn, 1000.0, "dex_arb_probe")
        self.assertEqual(n, 2)
        self.assertAlmostEqual(best, 0.45)
        buf = io.StringIO()
        with mock.patch.object(report.config, "DB_PATH", self.path), redirect_stdout(buf):
            report.main([])
        self.assertIn("hourly cap refused", buf.getvalue())

    def test_no_skip_line_without_refusals(self):
        self.assertEqual(report.skip_count(self.conn, 0, "dex_arb_probe"), (0, None))

    def test_database_without_probe_table(self):
        old = os.path.join(self.tmp.name, "old.sqlite")
        c = sqlite3.connect(old)
        c.execute("CREATE TABLE x (a)")
        self.assertEqual(report.probe_summary(c, 0), [])
        self.assertEqual(report.top_probes(c, 0, 5), [])
        c.close()

if __name__ == "__main__":
    unittest.main()


class TestPeggedPools(unittest.TestCase):
    """PEGGED POOLS, hand-worked (cbBTC/WBTC, depth size $1,000):

    ts 2000: pool A bid 0.9997 ask 0.9999, $1,000: 0.9990 / 1.0006
             pool B bid 1.0002 ask 1.0004, $1,000: can't fill
             best bid B, best ask A
    ts 2060: pool A bid 0.9996 ask 0.9998, $1,000: 0.9990 / 1.0004 (best both sides)
    pool A: best bid 1 of 2 minutes = 50%, best ask 2 of 2 = 100%
            rt $100 = median(0.9999/0.9997 - 1, 0.9998/0.9996 - 1) = median(2.0006, 2.0008) = 2.0007 bp
            rt $1,000 = median(1.0006/0.9990 - 1, 1.0004/0.9990 - 1) = median(16.016, 14.014) = 15.015 bp
            filled 2 of 2 = 100%
    pool B: best bid 50%, best ask 0%, rt $100 = 1.0004/1.0002 - 1 = 1.9996 bp,
            rt $1,000 none, filled 0 of 1 = 0%
    """

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.insert_pegged(self.conn, 2000.0, 1, [
            PegQuote("cbBTC/WBTC", "A", 0.9997, 0.9999, 0.9990, 1.0006),
            PegQuote("cbBTC/WBTC", "B", 1.0002, 1.0004, None, None)], depth_usd=1000.0)
        db.insert_pegged(self.conn, 2060.0, 2, [
            PegQuote("cbBTC/WBTC", "A", 0.9996, 0.9998, 0.9990, 1.0004)], depth_usd=1000.0)

    def tearDown(self):
        self.conn.close()

    def test_hand_worked(self):
        rows = {r[1]: r for r in report.pegged_pools(self.conn, 0)}
        _, _, n, bid_w, ask_w, rt, rt_big, filled, size = rows["A"]
        self.assertEqual((n, size), (2, 1000.0))
        self.assertAlmostEqual(bid_w, 50.0)
        self.assertAlmostEqual(ask_w, 100.0)
        self.assertAlmostEqual(rt, 2.0007, places=3)
        self.assertAlmostEqual(rt_big, 15.015, places=3)
        self.assertAlmostEqual(filled, 100.0)
        _, _, n, bid_w, ask_w, rt, rt_big, filled, _ = rows["B"]
        self.assertEqual(n, 1)
        self.assertAlmostEqual(bid_w, 50.0)
        self.assertAlmostEqual(ask_w, 0.0)
        self.assertAlmostEqual(rt, 1.9996, places=3)
        self.assertIsNone(rt_big)
        self.assertAlmostEqual(filled, 0.0)

    def test_price_setting_pool_listed_first(self):
        self.assertEqual([r[1] for r in report.pegged_pools(self.conn, 0)], ["A", "B"])

    def test_old_table_without_depth_columns(self):
        raw = sqlite3.connect(":memory:")
        raw.execute("CREATE TABLE pegged_quotes (ts REAL, block INTEGER, pair TEXT,"
                    " pool TEXT, bid REAL, ask REAL)")
        raw.execute("INSERT INTO pegged_quotes VALUES (1.0, 1, 'USDT/USDC', 'x', 0.9999, 1.0001)")
        (row,) = report.pegged_pools(raw, 0)
        raw.close()
        self.assertEqual((row[6], row[7], row[8]), (None, None, None))
        self.assertAlmostEqual(row[5], 2.0002, places=3)

    def test_print_shows_section_and_dead_pools(self):
        db.insert_pegged(self.conn, 2060.0, 2, [
            PegQuote("cbBTC/WBTC", "dead", 0.03, 270.0, None, None)], depth_usd=1000.0)
        buf = io.StringIO()
        with redirect_stdout(buf):
            report.print_report(self.conn, 0, 0.05, 15)
        out = buf.getvalue()
        self.assertIn("PEGGED POOLS", out)
        self.assertIn("rt $1,000", out)
        line_a = next(x for x in out.splitlines() if x.startswith("cbBTC/WBTC") and " A " in x)
        self.assertEqual(line_a.split()[-3:], ["2.0", "15.0", "100%"])
        line_dead = next(x for x in out.splitlines() if " dead " in x)
        self.assertIn("dead", line_dead.split()[-3])
