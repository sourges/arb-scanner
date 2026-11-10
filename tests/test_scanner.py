"""End-to-end: startup + step against the fake chain and fake CEX books."""
import io
import unittest
from contextlib import redirect_stdout

import db
from scanner import Scanner, episode_line, print_table, status_line
from tests.fakes import WETH, FakeChain, FakeFetchers, make_config

AERO_QUOTER = "0x" + "b2" * 20


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def make_scanner():
    cfg = make_config()
    chain = FakeChain()
    clock = Clock()
    mids = {("kraken", "ETHUSD"): 2000.0, ("kraken", "XBTUSD"): 60000.0,
            ("coinbase", "ETH-USD"): 2000.0, ("coinbase", "BTC-USD"): 60000.0}
    fetchers = FakeFetchers(mids, clock)
    s = Scanner(cfg, chain, db.connect(":memory:"), fetchers=fetchers.as_dict(), clock=clock)
    s.startup()
    return s, chain, clock, fetchers


class TestScanner(unittest.TestCase):
    def test_startup_finds_pools_and_prices(self):
        s, *_ = make_scanner()
        self.assertEqual([p.label for p in s.pair_pools["ETH"][1]], ["uni-500", "aero-ts100"])
        self.assertAlmostEqual(s.ref_prices["ETH"], 2000.0)
        self.assertAlmostEqual(s.ref_prices["BTC"], 60000.0)
        s.shutdown()

    def test_reference_price_prefers_kraken(self):
        s, chain, clock, fetchers = make_scanner()
        fetchers.mids[("coinbase", "ETH-USD")] = 2010.0
        chain.block, clock.t = 101, clock.t + 2
        s.step()
        self.assertAlmostEqual(s.ref_prices["ETH"], 2000.0)
        s.shutdown()

    def test_startup_rejects_bad_token(self):
        cfg = make_config()
        chain = FakeChain()
        chain.tokens[WETH] = ("WETH", 6)
        s = Scanner(cfg, chain, db.connect(":memory:"), fetchers={}, clock=Clock())
        with self.assertRaises(SystemExit):
            s.startup()
        s.pool.shutdown(wait=False)

    def test_step_produces_all_routes_and_skips_same_block(self):
        s, *_ = make_scanner()
        block, results = s.step()
        self.assertEqual(block, 100)
        # 13 routes per pair (1 dex>dex + 3 schedules x maker/taker x 2 directions)
        self.assertEqual(len(results), 26)
        # identical prices everywhere -> nothing profitable
        self.assertTrue(all(r.net_usd < 0 for r in results.values()))
        self.assertIsNone(s.step())                             # same block
        self.assertIn("ETH best", status_line(block, results))
        s.shutdown()

    def test_profitable_gap_becomes_logged_episode(self):
        s, chain, clock, _ = make_scanner()
        chain.prices[(AERO_QUOTER, 100, WETH)] = (2020.0, 0.0005)   # Aerodrome sells ETH 1% higher
        for blk in (101, 102):
            chain.block, clock.t = blk, clock.t + 2
            _, results = s.step()
            r = results[("ETH", "dex>dex")]
            self.assertEqual((r.buy_venue, r.sell_venue), ("uni-500", "aero-ts100"))
            self.assertGreater(r.net_usd, 0.8)
        chain.prices[(AERO_QUOTER, 100, WETH)] = (2000.0, 0.0005)   # gap closes
        chain.block, clock.t = 103, clock.t + 2
        s.step()
        rows = s.conn.execute(
            "SELECT pair, route, samples, best_sell FROM episodes WHERE route='dex>dex'").fetchall()
        self.assertEqual(rows, [("ETH", "dex>dex", 2, "aero-ts100")])
        # prices at the best moment: bought on uni at 2000/0.9995, sold on aero at 2020*0.9995
        mid, buy_px, sell_px = s.conn.execute(
            "SELECT best_mid, best_buy_px, best_sell_px FROM episodes WHERE route='dex>dex'").fetchone()
        self.assertAlmostEqual(mid, 2000.0)
        self.assertAlmostEqual(buy_px, 2000 / 0.9995, delta=0.01)
        self.assertAlmostEqual(sell_px, 2020 * 0.9995, delta=0.01)
        s.shutdown()

    def test_kraken_outage_falls_back_to_coinbase_after_cache_expires(self):
        s, chain, clock, fetchers = make_scanner()
        fetchers.fail.add("kraken")
        fetchers.mids[("coinbase", "ETH-USD")] = 2010.0
        chain.block, clock.t = 101, clock.t + 10                   # older than BOOK_MAX_AGE_S
        _, results = s.step()
        self.assertFalse(any("kraken" in key for _, key in results))
        self.assertTrue(any("coinbase" in key for _, key in results))
        self.assertAlmostEqual(s.ref_prices["ETH"], 2010.0)
        s.shutdown()

    def test_shutdown_writes_open_episode(self):
        s, chain, clock, _ = make_scanner()
        chain.prices[(AERO_QUOTER, 100, WETH)] = (2020.0, 0.0005)
        chain.block = 101
        s.step()
        s.shutdown()
        self.assertEqual(
            s.conn.execute("SELECT COUNT(*) FROM episodes WHERE route='dex>dex'").fetchone(), (1,))


class TestOutput(unittest.TestCase):
    def test_episode_line_shows_prices_and_samples(self):
        from tracker import Episode
        e = Episode("dex>dex", "ETH", 1, 0.0, 9, 18.0, 10, 0.668, 0.668, "aero3-ts1", "pcsv3-500",
                    best_mid=3000.0, best_buy_px=2990.5, best_sell_px=3010.25)
        line = episode_line(e)
        self.assertIn("10 samples / 18s", line)
        self.assertIn("aero3-ts1 @2,990.50", line)
        self.assertIn("pcsv3-500 @3,010.25", line)
        self.assertIn("cex mid 3,000.00", line)

    def test_episode_line_without_prices(self):
        from tracker import Episode
        e = Episode("k", "BTC", 1, 0.0, 1, 0.0, 1, 0.1, 0.1, "A", "B")
        self.assertIn("@?", episode_line(e))

    def test_long_route_names_stay_in_their_column(self):
        s, *_ = make_scanner()
        _, results = s.step()
        s.shutdown()
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_table(results)
        for line in buf.getvalue().splitlines()[1:]:
            self.assertEqual(line[33], " ", line)    # the 34-wide route column always ends in a space

if __name__ == "__main__":
    unittest.main()
