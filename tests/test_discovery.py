"""Wider pool discovery for the extra pairs, and find_pool.py. Hand-worked cases.

Fake DEXES (make_config): uni tiers [100, 500], aero (slip) tiers [50, 100].
EXTRA_DISCOVERY_TIERS (config.py): uni +[10000], slip +[10, 500, 1000, 2000].
A SOL pool at Aerodrome tick spacing 2000 (like SOL's real Aerodrome pools, which the
first live run missed) is found for the extra pair SOL, never for the core pair ETH.
"""
import io
import unittest
from contextlib import redirect_stdout

import dex
import find_pool
from tests.fakes import (CBBTC, CLASSIC_FACTORY, SLIP_FACTORY, SLIP_QUOTER, UNI_FACTORY, USDC,
                         WETH, FakeChain, make_config)
from tests.test_extra_pairs import SOL_T, ExtraBase

SOL_TS2000 = "0x" + "e2" * 20


class TestDiscoveryDexes(unittest.TestCase):
    def test_adds_tiers_without_duplicates_and_keeps_order(self):
        dexes = [{"name": "u", "kind": "uni", "tiers": [100, 500]},
                 {"name": "a", "kind": "slip", "tiers": [50, 100]}]
        wide = dex.discovery_dexes(dexes, {"uni": [10000, 500], "slip": [2000]})
        self.assertEqual(wide[0]["tiers"], [100, 500, 10000])
        self.assertEqual(wide[1]["tiers"], [50, 100, 2000])
        self.assertEqual(dexes[0]["tiers"], [100, 500])            # config not changed

    def test_no_extra_tiers(self):
        dexes = [{"name": "u", "kind": "uni", "tiers": [100]}]
        self.assertEqual(dex.discovery_dexes(dexes, None)[0]["tiers"], [100])
        self.assertEqual(dex.discovery_dexes(dexes, {"slip": [2000]})[0]["tiers"], [100])


class TestExtraPairDiscovery(ExtraBase):
    def chain_with_ts2000(self, price=150.0):
        c = self.chain()
        c.pools[(SLIP_FACTORY, 2000)] = SOL_TS2000
        c.prices[(SLIP_QUOTER, 2000, SOL_T)] = (price, 0.01)       # 1% pool
        return c

    def test_extra_pair_finds_the_wider_tier_pool(self):
        s, *_ = self.scanner(self.chain_with_ts2000())
        self.assertIn("aero-ts2000", [p.label for p in s.pair_pools["SOL"][1]])

    def test_core_pairs_still_use_only_dexes_tiers(self):
        s, *_ = self.scanner(self.chain_with_ts2000())
        self.assertNotIn("aero-ts2000", [p.label for p in s.pair_pools["ETH"][1]])
        self.assertNotIn("aero-ts2000", [p.label for p in s.pair_pools["BTC"][1]])

    def test_switched_off(self):
        s, *_ = self.scanner(self.chain_with_ts2000(), EXTRA_DISCOVERY_TIERS={})
        self.assertNotIn("aero-ts2000", [p.label for p in s.pair_pools["SOL"][1]])

    def test_new_pool_still_screened(self):
        s, _, _, lines = self.scanner(self.chain_with_ts2000(price=200.0))   # +33% off $150
        self.assertNotIn("aero-ts2000", [p.label for p in s.pair_pools["SOL"][1]])
        self.assertTrue(any("SOL dropped as off-market" in l and "aero-ts2000" in l for l in lines))

    def test_wider_pool_used_in_routes(self):
        # ts2000 pool prices SOL 1.5% above the others ($152.25, 1% fee). Best dex>dex:
        #   buy on uni-500 ($150, 0.05% fee), sell on aero-ts2000:
        #   100 * 0.9995 * (152.25 / 150) * 0.99 = 100.4350 back (vs 99.90 selling on aero-ts100)
        s, chain, clock, _ = self.scanner(self.chain_with_ts2000(price=152.25))
        _, results = self.step(s, chain, clock)
        r = results[("SOL", "dex>dex")]
        self.assertEqual(r.sell_venue, "aero-ts2000")
        self.assertAlmostEqual(r.usd_back, 100 * 0.9995 * 1.015 * 0.99, delta=0.002)


class TestFindPool(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()
        self.chain = FakeChain()

    def pool(self, address, **fields):
        self.chain.pool_info[address] = fields
        return address

    def text(self, address):
        return "\n".join(find_pool.describe(self.chain, self.cfg, address))

    def test_v3_pool_in_dexes_tiers(self):
        a = self.pool("0x" + "f1" * 20, token0=WETH, token1=USDC, fee=500, tick_spacing=10,
                      factory=UNI_FACTORY)
        out = self.text(a)
        self.assertIn("WETH (18 decimals)", out)
        self.assertIn("USDC (6 decimals)", out)
        self.assertIn("fee     0.05% (fee() = 500)", out)
        self.assertIn("spacing 10", out)
        self.assertIn("scanner uni-500: scanned", out)

    def test_one_percent_tier_is_extra_pairs_only(self):
        a = self.pool("0x" + "f2" * 20, token0=WETH, token1=USDC, fee=10000, tick_spacing=200,
                      factory=UNI_FACTORY)
        self.assertIn("scanner uni-10000: extra pairs only", self.text(a))

    def test_uncovered_tier_says_not_scanned(self):
        a = self.pool("0x" + "f3" * 20, token0=WETH, token1=USDC, fee=3000, tick_spacing=60,
                      factory=UNI_FACTORY)
        self.assertIn("scanner uni-3000: NOT scanned", self.text(a))

    def test_slipstream_uses_tick_spacing(self):
        a = self.pool("0x" + "f4" * 20, token0=WETH, token1=CBBTC, fee=10000, tick_spacing=2000,
                      factory=SLIP_FACTORY)
        out = self.text(a)
        self.assertIn("fee     1% (fee() = 10000)", out)
        self.assertIn("scanner aero-ts2000: extra pairs only", out)

    def test_classic_pool(self):
        cfg = make_config(RECORD_CLASSIC_DEXES=[{"name": "aeroc", "factory": CLASSIC_FACTORY}])
        a = self.pool("0x" + "f5" * 20, token0=USDC, token1=WETH, factory=CLASSIC_FACTORY, stable=False)
        out = "\n".join(find_pool.describe(self.chain, cfg, a))
        self.assertIn("type    classic volatile pool, aeroc", out)
        self.assertIn("recorder only", out)

    def test_unknown_factory(self):
        a = self.pool("0x" + "f6" * 20, token0=WETH, token1=USDC, fee=500, tick_spacing=10,
                      factory="0x" + "99" * 20)
        out = self.text(a)
        self.assertIn("unknown factory 0x" + "99" * 20, out)
        self.assertNotIn("scanner", out)

    def test_not_a_pool(self):
        self.assertIn("not a pool", self.text("0x" + "f7" * 20))

    def test_no_arguments_prints_help(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(find_pool.main([]), 1)
        self.assertIn("python find_pool.py", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
