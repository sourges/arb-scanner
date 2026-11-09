"""End-to-end: startup + step against the fake chain and fake CEX books."""
import unittest

import db
from scanner import Scanner, status_line
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
            "SELECT pair, route, blocks, best_sell FROM episodes WHERE route='dex>dex'").fetchall()
        self.assertEqual(rows, [("ETH", "dex>dex", 2, "aero-ts100")])
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


if __name__ == "__main__":
    unittest.main()
