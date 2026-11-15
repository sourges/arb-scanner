"""Recorder: Aerodrome classic pools and pool depth (added Nov 14, 2026).

Hand-worked numbers (fake chain, size $100, depth size $1,000):

Classic USDT/USDC stable pool (aeroc-stable), price 0.9999 USDC per USDT, fee 0.05%
    bid = 0.9999 * 0.9995 = 0.99940
    ask = 0.9999 / 0.9995 = 1.00040
    round trip = ask / bid - 1 = 1 / 0.9995^2 - 1 = 10.0 bp      (just the fee twice)
    No volatile USDT/USDC classic pool exists, so only aeroc-stable is found.
    (The fake Slipstream factory reports a ts100 pool for any pair; its USDT quotes
    revert, so aero-ts100 is discovered but never recorded.)
    It doesn't set the best price: uni-500 bids 0.9999 and asks 1.0001.

Depth: uni-500 USDT/USDC (price 1.0, fee 0.01%) made thin, price moves 0.1% per $1,000
    $100:   move 0.0001   bid = 0.9999 * 0.9999 = 0.99980   ask = 1 / 0.99980 = 1.00020
            round trip = 4.0 bp
    $1,000: move 0.001    bid = 0.9999 * 0.999  = 0.9989001 ask = 1 / 0.9989001 = 1.0011011
            round trip = 22.0 bp   <- a thin pool's round trip blows up with size

Can't fill: uni-500 cbBTC/WBTC limited to $500 -> its $1,000 quotes revert, so
    depth_bid / depth_ask are None but the $100 bid/ask are still recorded.
"""
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout

import abi
import db
import recorder
from tests.fakes import (CBBTC, CLASSIC_FACTORY, CLASSIC_USDT_STABLE, UNI_QUOTER, USDC, USDT,
                         WBTC, FakeChain, make_config)

REF = {"ETH": 2000.0, "BTC": 60000.0}
CLASSIC = [{"name": "aeroc", "factory": CLASSIC_FACTORY, "stable": [True, False]}]


def make_recorder(classic=CLASSIC, depth=1000.0):
    cfg = make_config(RECORD_CLASSIC_DEXES=classic, RECORD_DEPTH_USD=depth)
    chain = FakeChain()
    lines = []
    rec = recorder.Recorder(cfg, chain, db.connect(":memory:"), lines.append)
    rec.startup()
    return rec, chain, lines


def by_pool(quotes):
    return {(q.pair, q.pool): q for q in quotes}


class TestClassicPools(unittest.TestCase):
    def test_finds_only_existing_classic_pools(self):
        rec, _, lines = make_recorder()
        pools = {p["name"]: [x.label for x in ps] for p, ps in rec.pairs}
        self.assertEqual(pools["USDT/USDC"], ["uni-500", "aero-ts100", "aeroc-stable"])  # no volatile
        self.assertEqual(pools["cbBTC/WBTC"], ["uni-500", "aero-ts100"])     # no classic pool
        self.assertTrue(any("USDT/USDC pools: uni-500, aero-ts100, aeroc-stable" in line for line in lines))

    def test_classic_hand_worked_bid_ask(self):
        rec, _, _ = make_recorder()
        q = by_pool(rec.record(1_000_000.0, REF))[("USDT/USDC", "aeroc-stable")]
        self.assertAlmostEqual(q.bid, 0.99940, delta=1e-6)
        self.assertAlmostEqual(q.ask, 1.00040, delta=1e-6)
        self.assertAlmostEqual(recorder.round_trip_bp(q.bid, q.ask), 10.0, delta=0.01)
        best = {s.pair: s for s in recorder.summarize(rec.last_quotes)}["USDT/USDC"]
        self.assertEqual((best.bid_pool, best.ask_pool), ("uni-500", "uni-500"))

    def test_volatile_pool_is_found_and_labelled_separately(self):
        chain = FakeChain()
        vol = "0x" + "d3" * 20
        chain.classic_pools[(frozenset({USDT, USDC}), False)] = vol
        chain.classic[vol] = (USDT, USDC, 0.9990, 0.003)            # volatile: 0.3% fee
        found = recorder.discover_classic_pools(chain, make_config().MULTICALL3, CLASSIC, USDT, USDC)
        self.assertEqual([(p.label, p.tier, p.address) for p in found],
                         [("aeroc-stable", 1, CLASSIC_USDT_STABLE), ("aeroc-vol", 0, vol)])

    def test_classic_calls_go_to_the_pool_with_get_amount_out(self):
        p = recorder.discover_classic_pools(FakeChain(), make_config().MULTICALL3, CLASSIC, USDT, USDC)[0]
        self.assertEqual((p.kind, p.tier, p.address), ("classic", 1, CLASSIC_USDT_STABLE))
        target, data = recorder._quote_call(p, USDT, USDC, 10 ** 8)
        self.assertEqual(target, CLASSIC_USDT_STABLE)
        self.assertEqual(data[:4], abi.selector(abi.SIG_GET_AMOUNT_OUT))

    def test_classic_off_by_default_in_tests_and_still_one_call(self):
        rec, chain, _ = make_recorder(classic=[])
        self.assertNotIn("aeroc-stable", [x.label for _, ps in rec.pairs for x in ps])
        before = chain.calls
        rec.record(1_000_000.0, REF)
        self.assertEqual(chain.calls, before + 1)

    def test_failed_classic_lookup_keeps_v3_pools(self):
        bad = [{"name": "aeroc", "factory": "0x" + "ee" * 20, "stable": [True, False]}]
        rec, _, _ = make_recorder(classic=bad)
        pools = {p["name"]: [x.label for x in ps] for p, ps in rec.pairs}
        self.assertEqual(pools["USDT/USDC"], ["uni-500", "aero-ts100"])


class TestDepth(unittest.TestCase):
    def test_thin_pool_round_trip_blows_up_with_size(self):
        rec, chain, _ = make_recorder()
        chain.peg_impact[(UNI_QUOTER, 500, USDT, USDC)] = 0.001
        before = chain.calls
        q = by_pool(rec.record(1_000_000.0, REF))[("USDT/USDC", "uni-500")]
        self.assertEqual(chain.calls, before + 1)                     # both sizes, ONE eth_call
        self.assertAlmostEqual(q.bid, 0.99980, delta=1e-6)
        self.assertAlmostEqual(q.ask, 1.00020, delta=1e-6)
        self.assertAlmostEqual(q.depth_bid, 0.9989001, delta=1e-6)
        self.assertAlmostEqual(q.depth_ask, 1.0011011, delta=1e-6)
        self.assertAlmostEqual(recorder.round_trip_bp(q.bid, q.ask), 4.0, delta=0.01)
        self.assertAlmostEqual(recorder.round_trip_bp(q.depth_bid, q.depth_ask), 22.0, delta=0.05)

    def test_size_the_pool_cannot_fill_is_none_but_100_is_kept(self):
        rec, chain, _ = make_recorder()
        chain.peg_max_usd[(UNI_QUOTER, 500, CBBTC, WBTC)] = 500
        q = by_pool(rec.record(1_000_000.0, REF))[("cbBTC/WBTC", "uni-500")]
        self.assertIsNotNone(q.bid)
        self.assertIsNotNone(q.ask)
        self.assertIsNone(q.depth_bid)
        self.assertIsNone(q.depth_ask)

    def test_depth_does_not_change_the_100_dollar_prices(self):
        with_depth = by_pool(make_recorder(depth=1000.0)[0].record(1_000_000.0, REF))
        without = by_pool(make_recorder(depth=None)[0].record(1_000_000.0, REF))
        self.assertEqual(set(with_depth), set(without))
        for key in without:
            self.assertEqual((with_depth[key].bid, with_depth[key].ask), (without[key].bid, without[key].ask))
            self.assertIsNone(without[key].depth_bid)

    def test_rows_store_depth_columns(self):
        rec, chain, _ = make_recorder()
        chain.peg_max_usd[(UNI_QUOTER, 500, CBBTC, WBTC)] = 500
        rec.record(1_000_000.0, REF)
        rows = {(p, pool): (usd, b, a) for p, pool, usd, b, a in rec.conn.execute(
            "SELECT pair, pool, depth_usd, depth_bid, depth_ask FROM pegged_quotes")}
        usd, b, a = rows[("USDT/USDC", "aeroc-stable")]
        self.assertEqual(usd, 1000.0)
        self.assertAlmostEqual(b, 0.99940, delta=1e-6)                # classic: no impact in fake
        self.assertEqual(rows[("cbBTC/WBTC", "uni-500")], (1000.0, None, None))

    def test_depth_off_stores_null_size(self):
        rec, _, _ = make_recorder(depth=None)
        rec.record(1_000_000.0, REF)
        self.assertEqual(rec.conn.execute(
            "SELECT COUNT(*) FROM pegged_quotes WHERE depth_usd IS NOT NULL").fetchone(), (0,))

    def test_print_table_shows_both_round_trips(self):
        rec, chain, _ = make_recorder()
        chain.peg_impact[(UNI_QUOTER, 500, USDT, USDC)] = 0.001
        quotes = rec.record(1_000_000.0, REF)
        buf = io.StringIO()
        with redirect_stdout(buf):
            recorder.print_table(quotes, 1000.0)
        out = buf.getvalue()
        self.assertIn("rt bp $1,000", out)
        line = next(x for x in out.splitlines() if x.startswith("USDT/USDC") and "uni-500" in x)
        self.assertEqual(line.split()[-2:], ["4.0", "22.0"])


class TestOldDatabase(unittest.TestCase):
    def test_upgrade_adds_depth_columns_and_keeps_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.sqlite")
            raw = sqlite3.connect(path)
            raw.execute("CREATE TABLE pegged_quotes (ts REAL, block INTEGER, pair TEXT,"
                        " pool TEXT, bid REAL, ask REAL)")
            raw.execute("INSERT INTO pegged_quotes VALUES (1.0, 1, 'USDT/USDC', 'x', 0.9999, 1.0001)")
            raw.commit()
            raw.close()
            conn = db.connect(path)
            try:
                row = conn.execute("SELECT bid, depth_usd, depth_bid, depth_ask FROM pegged_quotes").fetchone()
            finally:
                conn.close()
        self.assertEqual(row, (0.9999, None, None, None))


if __name__ == "__main__":
    unittest.main()
