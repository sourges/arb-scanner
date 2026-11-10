"""Pegged-pair recorder against the fake chain.

Hand-worked numbers (fake pools, BTC reference $60,000, size $100):

cbBTC/WBTC, price = WBTC per 1 cbBTC
    x_a = $100 / 60000 = 0.00166666 cbBTC = 166,666 sats (whole units, rest truncated)
    uni-500    pool price 0.9998, fee 0.01%
        bid = 0.9998 * 0.9999 = 0.99970      (sell cbBTC: receive less)
        ask = 0.9998 / 0.9999 = 0.99990      (buy cbBTC: pay more)
    aero-ts100 pool price 1.0003, fee 0.01%
        bid = 1.0003 * 0.9999 = 1.00020
        ask = 1.0003 / 0.9999 = 1.00040
    best bid 1.00020 (aero), best ask 0.99990 (uni)
        mid = (1.00020 + 0.99990) / 2 = 1.00005  -> +0.5 bp
        gap = 1.00020 / 0.99990 - 1   = +3.0 bp  -> buy on uni, sell on aero
    Truncating to whole sats moves these by under 0.1 bp, hence delta=1e-5.

USDT/USDC on uni-500, price 1.0, fee 0.01% (aero has no pool: its quotes revert)
    bid = 0.9999, ask = 1 / 0.9999 = 1.0001
    mid = 0.0 bp, gap = 0.9999 / 1.0001 - 1 = -2.0 bp (one pool can't beat itself)

USDbC and DAI are unknown to the fake chain, so those two pairs are skipped.
"""
import unittest

import db
import recorder
from rpc import RpcError
from tests.fakes import FakeChain, make_config

REF = {"ETH": 2000.0, "BTC": 60000.0}


def make_recorder():
    cfg = make_config()
    chain = FakeChain()
    lines = []
    rec = recorder.Recorder(cfg, chain, db.connect(":memory:"), lines.append)
    rec.startup()
    return rec, chain, lines


class TestStartup(unittest.TestCase):
    def test_rpc_failure_at_startup_skips_pairs_without_raising(self):
        cfg = make_config()
        chain = FakeChain()

        def down(*_args, **_kw):
            raise RpcError("eth_call: rate limited (HTTP 429)")

        chain.eth_call = down
        lines = []
        rec = recorder.Recorder(cfg, chain, db.connect(":memory:"), lines.append)
        rec.startup()                                   # must not raise
        self.assertEqual(rec.pairs, [])
        self.assertFalse(rec.due(0.0))
        self.assertEqual(len(lines), len(cfg.RECORD_PAIRS))
        self.assertIn("RpcError", lines[0])

    def test_skips_pairs_with_bad_tokens_and_keeps_the_rest(self):
        rec, _, lines = make_recorder()
        self.assertEqual([p["name"] for p, _ in rec.pairs], ["cbBTC/WBTC", "USDT/USDC"])
        skipped = [line for line in lines if "skipping" in line]
        self.assertEqual(len(skipped), 2)
        self.assertTrue(any("USDbC/USDC" in line for line in skipped))
        self.assertTrue(any("DAI/USDC" in line for line in skipped))

    def test_no_record_pairs_means_never_due(self):
        cfg = make_config(RECORD_PAIRS=[])
        rec = recorder.Recorder(cfg, FakeChain(), db.connect(":memory:"), lambda m: None)
        rec.startup()
        self.assertFalse(rec.due(1_000_000.0))


class TestQuotes(unittest.TestCase):
    def test_hand_worked_bid_ask_per_pool(self):
        rec, chain, _ = make_recorder()
        calls_before = chain.calls
        quotes = rec.record(1_000_000.0, REF)
        self.assertEqual(chain.calls, calls_before + 1)          # ONE eth_call for all pairs
        q = {(x.pair, x.pool): x for x in quotes}
        self.assertEqual(set(q), {("cbBTC/WBTC", "uni-500"), ("cbBTC/WBTC", "aero-ts100"),
                                  ("USDT/USDC", "uni-500")})     # aero USDT pool reverts: left out
        self.assertAlmostEqual(q[("cbBTC/WBTC", "uni-500")].bid, 0.99970, delta=1e-5)
        self.assertAlmostEqual(q[("cbBTC/WBTC", "uni-500")].ask, 0.99990, delta=1e-5)
        self.assertAlmostEqual(q[("cbBTC/WBTC", "aero-ts100")].bid, 1.00020, delta=1e-5)
        self.assertAlmostEqual(q[("cbBTC/WBTC", "aero-ts100")].ask, 1.00040, delta=1e-5)
        self.assertAlmostEqual(q[("USDT/USDC", "uni-500")].bid, 0.9999, delta=1e-6)
        self.assertAlmostEqual(q[("USDT/USDC", "uni-500")].ask, 1.0001, delta=1e-6)

    def test_summary_mid_and_gap(self):
        rec, _, _ = make_recorder()
        s = {x.pair: x for x in recorder.summarize(rec.record(1_000_000.0, REF))}
        btc = s["cbBTC/WBTC"]
        self.assertEqual((btc.ask_pool, btc.bid_pool), ("uni-500", "aero-ts100"))
        self.assertAlmostEqual(btc.mid_bp, 0.5, delta=0.1)
        self.assertAlmostEqual(btc.gap_bp, 3.0, delta=0.1)
        self.assertAlmostEqual(s["USDT/USDC"].mid_bp, 0.0, delta=0.01)
        self.assertAlmostEqual(s["USDT/USDC"].gap_bp, -2.0, delta=0.01)

    def test_summary_line_names_pools_only_for_positive_gap(self):
        rec, _, _ = make_recorder()
        line = recorder.summary_line(rec.record(1_000_000.0, REF))
        self.assertIn("cbBTC/WBTC mid +0.5bp gap +2.9bp (buy uni-500 > sell aero-ts100)", line)
        self.assertIn("USDT/USDC mid +0.0bp gap -2.0bp", line)
        self.assertNotIn("USDT/USDC mid +0.0bp gap -2.0bp (", line)

    def test_one_sided_pair(self):
        quotes = [recorder.PegQuote("X/Y", "p", 0.999, None)]
        s = recorder.summarize(quotes)[0]
        self.assertIsNone(s.mid_bp)
        self.assertIsNone(s.gap_bp)
        self.assertIn("X/Y one-sided", recorder.summary_line(quotes))


class TestTimingAndFailures(unittest.TestCase):
    def test_records_once_per_minute_and_stores_rows(self):
        rec, _, _ = make_recorder()
        t = 1_000_040.0                       # 20s into the minute that starts at 16667 * 60 = 1_000_020
        self.assertTrue(rec.due(t))
        rec.record(t, REF)
        self.assertFalse(rec.due(t + 39))     # 1_000_079: same minute
        self.assertTrue(rec.due(t + 40))      # 1_000_080: next minute
        rec.record(t + 40, REF)
        rows = rec.conn.execute("SELECT COUNT(*), COUNT(DISTINCT ts) FROM pegged_quotes").fetchone()
        self.assertEqual(rows, (6, 2))        # 3 pool rows x 2 minutes

    def test_rpc_failure_skips_the_minute_without_raising(self):
        rec, chain, lines = make_recorder()

        def boom(*_a, **_k):
            raise RpcError("rate limited")
        chain.eth_call = boom
        self.assertEqual(rec.record(1_000_000.0, REF), [])
        self.assertEqual(rec.failures, 1)
        self.assertFalse(rec.due(1_000_001.0))  # waits for the next minute instead of retrying
        self.assertIn("minute skipped (RpcError: rate limited)", lines[-1])
        self.assertEqual(rec.conn.execute("SELECT COUNT(*) FROM pegged_quotes").fetchone(), (0,))

    def test_missing_reference_price_skips_the_minute(self):
        rec, _, lines = make_recorder()
        self.assertEqual(rec.record(1_000_000.0, {"ETH": 2000.0}), [])
        self.assertIn("KeyError", lines[-1])


if __name__ == "__main__":
    unittest.main()
