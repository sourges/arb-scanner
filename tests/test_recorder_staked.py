"""Recorder: staked-ETH pairs (cbETH/WETH, wstETH/WETH, added Nov 18, 2026).

Staked ETH earns rewards, so its price in WETH sits well above 1.0. Hand-worked, fake chain,
ETH reference $2,000, size $100:
    x_a = $100 / 2000 = 0.05 cbETH   (sized by the ETH price; really ~$112 of cbETH)
    uni-500 cbETH/WETH pool price 1.12 WETH per cbETH, fee 0.01%
        bid = 1.12 x 0.9999 = 1.119888        ask = 1.12 / 0.9999 = 1.120112
        mid = 1.12 -> +1,200.0 bp away from 1.0;  gap = bid / ask - 1 = -2.0 bp (one pool)
wstETH/WETH: tokens known but no pool quotes -> found at startup, no rows recorded.
"""
import unittest

import config
import db
import recorder
from tests.fakes import CBETH, UNI_QUOTER, WETH, WSTETH, FakeChain, make_config

REF = {"ETH": 2000.0, "BTC": 60000.0}
STAKED = [p for p in config.RECORD_PAIRS if p["name"] in ("cbETH/WETH", "wstETH/WETH")]


def make():
    chain = FakeChain()
    chain.tokens[CBETH] = ("cbETH", 18)
    chain.tokens[WSTETH] = ("wstETH", 18)
    chain.pegged[(UNI_QUOTER, 500, CBETH, WETH)] = (1.12, 0.0001)
    lines = []
    rec = recorder.Recorder(make_config(RECORD_PAIRS=STAKED), chain, db.connect(":memory:"), lines.append)
    rec.startup()
    return rec, lines


class TestStakedConfig(unittest.TestCase):
    def test_new_pairs_are_in_the_real_config(self):
        self.assertEqual([p["name"] for p in STAKED], ["cbETH/WETH", "wstETH/WETH"])
        for p in STAKED:
            self.assertEqual((p["a"]["decimals"], p["b"]["symbol"], p["usd_ref"]), (18, "WETH", "ETH"))

    def test_every_usd_ref_is_a_scanner_price(self):
        # a typo here would make every recorder minute fail with a KeyError
        names = {p["name"] for p in config.PAIRS}
        for p in config.RECORD_PAIRS:
            self.assertTrue(p["usd_ref"] is None or p["usd_ref"] in names, p["name"])


class TestStakedQuotes(unittest.TestCase):
    def test_hand_worked_price_above_one(self):
        rec, lines = make()
        self.assertEqual([p["name"] for p, _ in rec.pairs], ["cbETH/WETH", "wstETH/WETH"])
        quotes = rec.record(1_000_000.0, REF)
        q = {(x.pair, x.pool): x for x in quotes}
        self.assertEqual(set(p for p, _ in q), {"cbETH/WETH"})          # wstETH: no pool quotes
        cb = q[("cbETH/WETH", "uni-500")]
        self.assertAlmostEqual(cb.bid, 1.119888, places=6)
        self.assertAlmostEqual(cb.ask, 1.120112, places=6)
        s = {x.pair: x for x in recorder.summarize(quotes)}["cbETH/WETH"]
        self.assertAlmostEqual(s.mid_bp, 1200.0, places=2)
        self.assertAlmostEqual(s.gap_bp, -2.0, places=2)
        self.assertIn("cbETH/WETH mid +1200.0bp gap -2.0bp", recorder.summary_line(quotes))

    def test_rows_stored(self):
        rec, _ = make()
        rec.record(1_000_000.0, REF)
        rows = rec.conn.execute("SELECT pair, pool, bid, ask FROM pegged_quotes").fetchall()
        self.assertEqual([(r[0], r[1]) for r in rows], [("cbETH/WETH", "uni-500")])


if __name__ == "__main__":
    unittest.main()
