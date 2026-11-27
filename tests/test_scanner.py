"""End-to-end: startup + step against the fake chain and fake CEX books."""
import io
import sqlite3
import unittest
from contextlib import redirect_stdout

import db
import scanner as scanner_mod
from rpc import RpcError
from scanner import Scanner, episode_line, print_table, status_line
from tests.fakes import WETH, FakeChain, FakeFetchers, make_config

AERO_QUOTER = "0x" + "b2" * 20


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def make_scanner(**cfg_over):
    cfg = make_config(**cfg_over)
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

    def test_recorder_runs_once_per_minute_inside_step(self):
        s, chain, clock, _ = make_scanner()
        self.assertEqual([p["name"] for p, _ in s.recorder.pairs], ["cbBTC/WBTC", "USDT/USDC"])
        clock.t = 1_000_020.0
        buf = io.StringIO()
        with redirect_stdout(buf):
            s.step()                                             # first step records
            chain.block, clock.t = 101, clock.t + 2
            s.step()                                             # same minute: no record
            chain.block, clock.t = 102, clock.t + 60
            s.step()                                             # next minute records again
        n = s.conn.execute("SELECT COUNT(DISTINCT ts) FROM pegged_quotes").fetchone()[0]
        self.assertEqual(n, 2)
        self.assertEqual(buf.getvalue().count("PEG cbBTC/WBTC"), 2)
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


class TestPriceRows(unittest.TestCase):
    def test_every_scan_writes_one_price_row_per_pair(self):
        s, chain, clock, _ = make_scanner()
        s.step()
        chain.block, clock.t = 101, clock.t + 2
        s.step()
        rows = s.conn.execute(
            "SELECT path, block, kraken_bid, kraken_ask, kraken_age, dex_buy_px, dex_buy_pool,"
            " dex_sell_px, raw_edge, net_edge FROM scan_prices ORDER BY ts, path").fetchall()
        self.assertEqual([(r[0], r[1]) for r in rows],
                         [("BTC", 100), ("ETH", 100), ("BTC", 101), ("ETH", 101)])
        eth = rows[3]
        self.assertEqual(eth[2:4], (1999.5, 2000.5))               # fake book: mid 2000, spread 1
        self.assertAlmostEqual(eth[4], 0.0)                        # fetched this scan
        self.assertAlmostEqual(eth[5], 2000 / 0.9995, delta=0.01)  # pool price + 0.05% fee
        self.assertEqual(eth[6], "uni-500")
        self.assertAlmostEqual(eth[7], 2000 * 0.9995, delta=0.01)
        self.assertAlmostEqual(eth[8], 0.0, delta=0.001)           # pools and Kraken agree
        self.assertIsNone(eth[9])
        s.shutdown()

    def test_stale_book_shows_its_age_then_drops_out(self):
        s, chain, clock, fetchers = make_scanner()
        fetchers.fail.add("kraken")
        with redirect_stdout(io.StringIO()):                       # hides the recorder's PEG line
            chain.block, clock.t = 101, clock.t + 4                # cached Kraken book, 4s old
            s.step()
            chain.block, clock.t = 102, clock.t + 4                # 8s old: past BOOK_MAX_AGE_S
            s.step()
        ages = s.conn.execute("SELECT kraken_age, kraken_bid, coinbase_age FROM scan_prices"
                              " WHERE path='ETH' ORDER BY ts").fetchall()
        self.assertAlmostEqual(ages[0][0], 4.0)
        self.assertEqual(ages[1][:2], (None, None))
        self.assertAlmostEqual(ages[1][2], 0.0)
        s.shutdown()

    def test_price_log_off_writes_nothing(self):
        s, *_ = make_scanner()
        s.cfg.PRICE_LOG = False
        s.step()
        self.assertEqual(s.conn.execute("SELECT COUNT(*) FROM scan_prices").fetchone(), (0,))
        s.shutdown()


class TestBadRpcReply(unittest.TestCase):
    def test_empty_reply_skips_the_round_and_the_scanner_carries_on(self):
        s, chain, clock, _ = make_scanner()
        real = chain.eth_call
        replies = [b"", b"\x00" * 40]                              # two bad rounds, then normal

        def flaky(to, data, block="latest"):
            return replies.pop(0) if replies else real(to, data, block)
        chain.eth_call = flaky
        chain.block = 101
        with redirect_stdout(io.StringIO()) as out:
            fails = scanner_mod.run_forever(s, 2.0, sleep=lambda _s: None, clock=clock, max_rounds=3)
        self.assertEqual(fails, 0)                                 # third round worked
        self.assertEqual(out.getvalue().count("round failed (RpcError"), 2)
        self.assertEqual(s.last_block, 101)
        s.shutdown()


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

class StubScanner:
    """step() raises the queued errors first, then returns a quiet round."""

    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = 0

    def step(self):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return 100 + self.calls, {}


class TestRunForever(unittest.TestCase):
    def run_loop(self, stub, rounds):
        sleeps = []
        with redirect_stdout(io.StringIO()) as out:
            fails = scanner_mod.run_forever(stub, 2.0, sleep=sleeps.append, clock=lambda: 0.0,
                                            max_rounds=rounds)
        return fails, sleeps, out.getvalue()

    def test_locked_database_is_retried_not_fatal(self):
        stub = StubScanner([sqlite3.OperationalError("database is locked")])
        fails, sleeps, out = self.run_loop(stub, 2)
        self.assertEqual(stub.calls, 2)
        self.assertEqual(sleeps, [2, 2.0])        # back-off 2s, then a normal 2s poll
        self.assertEqual(fails, 0)                # second round succeeded
        self.assertIn("OperationalError: database is locked", out)

    def test_back_off_doubles_and_caps_at_30(self):
        stub = StubScanner([RpcError("x")] * 6)
        _, sleeps, _ = self.run_loop(stub, 6)
        self.assertEqual(sleeps, [2, 4, 8, 16, 30, 30])

    def test_real_bugs_are_not_swallowed(self):
        stub = StubScanner([KeyError("bug")])
        with self.assertRaises(KeyError):
            self.run_loop(stub, 1)

class TestSizeProbes(unittest.TestCase):
    def test_probe_runs_once_when_an_episode_opens_and_joins_it(self):
        s, chain, clock, _ = make_scanner()
        chain.prices[(AERO_QUOTER, 100, WETH)] = (2020.0, 0.0005)    # 1% gap opens
        chain.block, clock.t = 101, clock.t + 2
        with redirect_stdout(io.StringIO()) as out:
            s.step()
            chain.block, clock.t = 102, clock.t + 2                  # same episode continues
            s.step()
            chain.prices[(AERO_QUOTER, 100, WETH)] = (2000.0, 0.0005)   # gap closes
            chain.block, clock.t = 103, clock.t + 2
            s.step()
        probes = s.conn.execute("SELECT ts, path, size_usd FROM episode_probes").fetchall()
        sizes = len(s.cfg.PROBE_SIZES_USD)
        self.assertEqual(len(probes), sizes)                         # probed once, not twice
        self.assertEqual({p[1] for p in probes}, {"ETH uni-500>aero-ts100"})
        (start,) = s.conn.execute(
            "SELECT start_ts FROM episodes WHERE route='dex>dex' AND pair='ETH'").fetchone()
        self.assertEqual({p[0] for p in probes}, {start})            # joins on start_ts
        self.assertIn("PROBE ETH uni-500>aero-ts100:", out.getvalue())
        s.shutdown()

    def test_episode_that_opens_small_is_probed_when_it_crosses(self):
        """Opens at about +0.019% (aero 0.13% above uni: (0.9995^2 * 1.0013 - 1) - 0.0106% gas),
        under the 0.05% threshold; the next scan it grows to +0.89% and is probed then.
        The probe joins the episode's start, and the episode isn't probed twice."""
        s, chain, clock, _ = make_scanner()
        chain.prices[(AERO_QUOTER, 100, WETH)] = (2000.0 * 1.0013, 0.0005)
        chain.block, clock.t = 101, clock.t + 2
        with redirect_stdout(io.StringIO()):
            s.step()
            self.assertEqual(s.conn.execute("SELECT COUNT(*) FROM episode_probes").fetchone(), (0,))
            chain.prices[(AERO_QUOTER, 100, WETH)] = (2020.0, 0.0005)
            chain.block, clock.t = 102, clock.t + 2
            s.step()
            chain.block, clock.t = 103, clock.t + 2
            s.step()
            chain.prices[(AERO_QUOTER, 100, WETH)] = (2000.0, 0.0005)
            chain.block, clock.t = 104, clock.t + 2
            s.step()
        probes = s.conn.execute("SELECT DISTINCT ts FROM episode_probes").fetchall()
        (start, samples) = s.conn.execute(
            "SELECT start_ts, samples FROM episodes WHERE route='dex>dex' AND pair='ETH'").fetchone()
        self.assertEqual(samples, 3)                     # one episode: small, big, big
        self.assertEqual(probes, [(start,)])             # probed once, joined to its start
        s.shutdown()

    def test_gap_in_samples_splits_the_episode(self):
        """Two positive scans 10s apart (> EPISODE_MAX_GAP_S = 6) are two episodes."""
        s, chain, clock, _ = make_scanner(EPISODE_MAX_GAP_S=6.0)
        chain.prices[(AERO_QUOTER, 100, WETH)] = (2020.0, 0.0005)
        with redirect_stdout(io.StringIO()):
            chain.block, clock.t = 101, clock.t + 2
            s.step()
            chain.block, clock.t = 102, clock.t + 10                 # scans missed in between
            s.step()
            chain.block, clock.t = 103, clock.t + 2
            s.step()
        s.shutdown()
        rows = s.conn.execute("SELECT samples, seconds FROM episodes WHERE route='dex>dex'"
                              " AND pair='ETH' ORDER BY start_ts").fetchall()
        self.assertEqual(rows, [(1, 0.0), (2, 2.0)])

    def test_quiet_market_makes_no_probes(self):
        s, chain, clock, _ = make_scanner()
        s.step()
        self.assertEqual(s.conn.execute("SELECT COUNT(*) FROM episode_probes").fetchone(), (0,))
        s.shutdown()

if __name__ == "__main__":
    unittest.main()
