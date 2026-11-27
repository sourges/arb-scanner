"""Extra pairs (EXTRA_PAIRS) against the fake chain. Hand-worked numbers.

Fake prices: ETH $2,000, BTC $60,000, every pool fee 0.05% (x 0.9995).
Test tokens (made up for the fake chain):
    SOL_T   9 decimals on chain (config says None), $150 on uni-500 and aero-ts100,
            Kraken SOLUSD and Coinbase SOL-USD books at $150
    VIRT_T  18 decimals, priced in WETH: 0.0005 WETH per VIRT = $1.00, no CEX book

Reference prices at startup
    ETH/BTC   base_ref "ETH"           -> $2,000
    SOL       CEX mid (Kraken)         -> $150
    VIRT/ETH  median pool $100 buy     -> $100 buys 100/2000 WETH = 0.05 WETH
              -> 0.05 / 0.0005 * 0.9995 = 99.95 VIRT -> $100 / 99.95 = $1.0005

ETH/BTC gap: aero prices WETH 0.8% above uni (cbBTC per WETH 1/30 * 1.008).
    buy  on uni:  $100 of cbBTC = 0.00166666 cbBTC (whole satoshis) -> /(1/30) * 0.9995 WETH
    sell on aero: that WETH * (1.008/30) * 0.9995 cbBTC, worth $60,000 each
    USD back ~= 100 * 0.9995^2 * 1.008 = 100.699 -> raw +0.699%, minus $0.0106 gas -> net ~+0.689%
"""
import unittest

import db
import dex
import scanner as scanner_mod
from scanner import Scanner, status_line, token_problem
from tests import fakes
from tests.fakes import (CBBTC, SLIP_QUOTER, UNI_QUOTER, USDC, WETH, FakeChain, FakeFetchers,
                         make_config)

SOL_T = "0x" + "5a" * 20
VIRT_T = "0x" + "7b" * 20
AERO_CROSS = (SLIP_QUOTER, 100, WETH, CBBTC)


def extra_pairs():
    sol = {"address": SOL_T, "symbol": "SOL", "decimals": None}
    virt = {"address": VIRT_T, "symbol": "VIRTUAL", "decimals": None}
    weth = {"address": WETH, "symbol": "WETH", "decimals": 18}
    cbbtc = {"address": CBBTC, "symbol": "cbBTC", "decimals": 8}
    return [
        {"name": "ETH/BTC", "base": weth, "quote": cbbtc, "base_ref": "ETH", "quote_ref": "BTC"},
        {"name": "SOL", "base": sol, "kraken": "SOLUSD", "coinbase": "SOL-USD"},
        {"name": "VIRT/ETH", "base": virt, "quote": weth, "quote_ref": "ETH"},
    ]


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class ExtraBase(unittest.TestCase):
    def setUp(self):
        fakes.DECIMALS[SOL_T], fakes.DECIMALS[VIRT_T] = 9, 18
        fakes.USD_PER_UNIT[SOL_T], fakes.USD_PER_UNIT[VIRT_T] = 150.0, 1.0

    def tearDown(self):
        for d in (fakes.DECIMALS, fakes.USD_PER_UNIT):
            d.pop(SOL_T, None)
            d.pop(VIRT_T, None)

    def chain(self, eth_btc_premium=1.0):
        c = FakeChain()
        c.tokens[SOL_T] = ("SOL", 9)
        c.tokens[VIRT_T] = ("VIRTUAL", 18)
        c.prices[(UNI_QUOTER, 500, SOL_T)] = (150.0, 0.0005)
        c.prices[(SLIP_QUOTER, 100, SOL_T)] = (150.0, 0.0005)
        c.pegged[(UNI_QUOTER, 500, VIRT_T, WETH)] = (0.0005, 0.0005)
        c.pegged[(SLIP_QUOTER, 100, VIRT_T, WETH)] = (0.0005, 0.0005)
        c.pegged[(UNI_QUOTER, 500, WETH, CBBTC)] = (1 / 30, 0.0005)
        c.pegged[AERO_CROSS] = (1 / 30 * eth_btc_premium, 0.0005)
        return c

    def scanner(self, chain=None, pairs=None, mids=None, **cfg_over):
        chain = chain or self.chain()
        cfg = make_config(EXTRA_PAIRS=extra_pairs() if pairs is None else pairs,
                          TRIANGLE_PAIRS=None, **cfg_over)
        clock = Clock()
        m = {("kraken", "ETHUSD"): 2000.0, ("kraken", "XBTUSD"): 60000.0,
             ("coinbase", "ETH-USD"): 2000.0, ("coinbase", "BTC-USD"): 60000.0,
             ("kraken", "SOLUSD"): 150.0, ("coinbase", "SOL-USD"): 150.0}
        if mids is not None:
            m = mids
        fetchers = FakeFetchers(m, clock)
        lines = []
        old_log = scanner_mod.log
        scanner_mod.log = lines.append
        self.addCleanup(setattr, scanner_mod, "log", old_log)
        s = Scanner(cfg, chain, db.connect(":memory:"), fetchers=fetchers.as_dict(), clock=clock)
        s.startup()
        self.addCleanup(s.shutdown)
        return s, chain, clock, lines

    def step(self, s, chain, clock, block=None):
        chain.block = block or chain.block + 1
        clock.t += 2
        return s.step()


class TestStartup(ExtraBase):
    def test_activates_pairs_reads_decimals_and_sets_reference_prices(self):
        s, *_ = self.scanner()
        self.assertEqual(s.extra_names, ["ETH/BTC", "SOL", "VIRT/ETH"])
        sol = s.pair_pools["SOL"][0]
        self.assertEqual(sol["base"]["decimals"], 9)                    # read from the chain
        self.assertAlmostEqual(s.ref_prices["ETH/BTC"], 2000.0)
        self.assertAlmostEqual(s.ref_prices["SOL"], 150.0)
        self.assertAlmostEqual(s.ref_prices["VIRT/ETH"], 1 / 0.9995, places=6)   # median pool buy
        self.assertEqual([p.label for p in s.pair_pools["VIRT/ETH"][1]], ["uni-500", "aero-ts100"])

    def test_core_pairs_unchanged(self):
        s, *_ = self.scanner()
        self.assertEqual([p["name"] for p in s.pairs[:2]], ["ETH", "BTC"])
        self.assertEqual([p.label for p in s.pair_pools["ETH"][1]], ["uni-500", "aero-ts100"])

    def test_wrong_symbol_skips_only_that_pair(self):
        chain = self.chain()
        chain.tokens[SOL_T] = ("SOLX", 9)
        s, _, _, lines = self.scanner(chain)
        self.assertNotIn("SOL", s.extra_names)
        self.assertIn("ETH/BTC", s.extra_names)
        self.assertTrue(any("extra pair SOL skipped" in l and "chain says SOLX" in l for l in lines))

    def test_unknown_token_and_fixed_decimals_mismatch(self):
        chain = self.chain()
        del chain.tokens[VIRT_T]
        pairs = extra_pairs()
        pairs[1]["base"]["decimals"] = 18                             # SOL is 9 on chain
        s, _, _, lines = self.scanner(chain, pairs=pairs)
        self.assertEqual(s.extra_names, ["ETH/BTC"])
        self.assertTrue(any("VIRT/ETH skipped" in l and "no response" in l for l in lines))
        self.assertTrue(any("SOL skipped" in l and "expected 18 decimals, chain says 9" in l for l in lines))

    def test_pair_with_no_working_pools_is_skipped(self):
        chain = self.chain()
        del chain.pegged[(UNI_QUOTER, 500, VIRT_T, WETH)]
        del chain.pegged[(SLIP_QUOTER, 100, VIRT_T, WETH)]
        s, _, _, lines = self.scanner(chain)
        self.assertNotIn("VIRT/ETH", s.extra_names)
        self.assertTrue(any("VIRT/ETH skipped: no pool could fill" in l for l in lines))

    def test_off_market_pool_dropped(self):
        chain = self.chain()
        chain.pegged[(SLIP_QUOTER, 100, VIRT_T, WETH)] = (0.0006, 0.0005)   # aero 20% off
        chain.pegged[(UNI_QUOTER, 100, VIRT_T, WETH)] = (0.0005, 0.0005)
        chain.pools[(fakes.UNI_FACTORY, 100)] = "0x" + "c9" * 20           # third pool so median is uni's
        s, _, _, lines = self.scanner(chain)
        labels = [p.label for p in s.pair_pools["VIRT/ETH"][1]]
        self.assertNotIn("aero-ts100", labels)
        self.assertIn("uni-500", labels)
        self.assertTrue(any("VIRT/ETH dropped as off-market" in l and "aero-ts100" in l for l in lines))

    def test_unavailable_cex_market_is_dropped_pair_kept(self):
        mids = {("kraken", "ETHUSD"): 2000.0, ("kraken", "XBTUSD"): 60000.0,
                ("coinbase", "ETH-USD"): 2000.0, ("coinbase", "BTC-USD"): 60000.0,
                ("coinbase", "SOL-USD"): 151.0}                         # no Kraken SOLUSD
        s, _, _, lines = self.scanner(mids=mids)
        sol = s.pair_pools["SOL"][0]
        self.assertIsNone(sol["kraken"])
        self.assertAlmostEqual(s.ref_prices["SOL"], 151.0)              # Coinbase instead
        self.assertTrue(any("SOL: kraken SOLUSD not available" in l for l in lines))

    def test_extras_off(self):
        s, *_ = self.scanner(pairs=[])
        self.assertEqual(s.extra_names, [])
        self.assertEqual(set(s.pair_pools), {"ETH", "BTC"})


class TestEachRound(ExtraBase):
    def test_eth_btc_gap_in_usd(self):
        s, chain, clock, _ = self.scanner(self.chain(eth_btc_premium=1.008))
        _, results = self.step(s, chain, clock)
        r = results[("ETH/BTC", "dex>dex")]
        self.assertEqual((r.buy_venue, r.sell_venue), ("uni-500", "aero-ts100"))
        self.assertAlmostEqual(r.usd_back, 100 * 0.9995 ** 2 * 1.008, delta=0.01)
        self.assertAlmostEqual(r.net_pct, 0.689, delta=0.01)
        self.assertAlmostEqual(r.buy_px, 2000 / 0.9995, delta=0.5)      # USD per WETH

    def test_cex_routes_only_for_usdc_quoted_pairs(self):
        s, chain, clock, _ = self.scanner()
        _, results = self.step(s, chain, clock)
        keys = lambda pair: {k for p, k in results if p == pair}
        self.assertEqual(keys("ETH/BTC"), {"dex>dex"})
        self.assertEqual(keys("VIRT/ETH"), {"dex>dex"})
        self.assertIn("kraken_reduced_taker>dex", keys("SOL"))
        self.assertIn("dex>coinbase_maker", keys("SOL"))

    def test_weth_priced_pair_with_a_cex_book_still_gets_no_cex_routes(self):
        # Like AERO/ETH and VIRTUAL/ETH in the real config: the Coinbase book gives the
        # reference price and scan_prices columns, but no CEX route is computed.
        pairs = extra_pairs()
        pairs[2]["coinbase"] = "VIRT-USD"
        mids = {("kraken", "ETHUSD"): 2000.0, ("kraken", "XBTUSD"): 60000.0,
                ("coinbase", "ETH-USD"): 2000.0, ("coinbase", "BTC-USD"): 60000.0,
                ("kraken", "SOLUSD"): 150.0, ("coinbase", "SOL-USD"): 150.0,
                ("coinbase", "VIRT-USD"): 1.0}
        s, chain, clock, _ = self.scanner(pairs=pairs, mids=mids)
        self.assertAlmostEqual(s.ref_prices["VIRT/ETH"], 1.0)           # from the CEX book
        _, results = self.step(s, chain, clock)
        self.assertEqual({k for p, k in results if p == "VIRT/ETH"}, {"dex>dex"})
        price = s.conn.execute("SELECT coinbase_bid FROM scan_prices WHERE path LIKE 'VIRT/ETH%'"
                               " OR path = 'VIRT/ETH'").fetchone()
        self.assertIsNotNone(price)

    def test_sol_route_numbers(self):
        # flat pools at the CEX price: dex>dex = 0.9995^2 - 1 -> -0.09995%, minus gas
        s, chain, clock, _ = self.scanner()
        _, results = self.step(s, chain, clock)
        r = results[("SOL", "dex>dex")]
        self.assertAlmostEqual(r.usd_back, 100 * 0.9995 ** 2, delta=0.001)

    def test_dex_derived_reference_follows_the_pools(self):
        s, chain, clock, _ = self.scanner()
        for key in ((UNI_QUOTER, 500, VIRT_T, WETH), (SLIP_QUOTER, 100, VIRT_T, WETH)):
            chain.pegged[key] = (0.0006, 0.0005)                       # VIRT now $1.20
        self.step(s, chain, clock)                                      # quoted at old ref
        self.assertAlmostEqual(s.dex_ref["VIRT/ETH"], 1.2, delta=0.002)
        self.step(s, chain, clock)
        self.assertAlmostEqual(s.ref_prices["VIRT/ETH"], 1.2, delta=0.002)

    def test_core_results_identical_with_extras_on(self):
        on, chain_on, clock_on, _ = self.scanner()
        off, chain_off, clock_off, _ = self.scanner(pairs=[])
        _, r_on = self.step(on, chain_on, clock_on)
        _, r_off = self.step(off, chain_off, clock_off)
        for key in r_off:
            self.assertAlmostEqual(r_on[key].net_usd, r_off[key].net_usd, places=9)

    def test_probe_uses_the_quote_token(self):
        s, chain, clock, _ = self.scanner(self.chain(eth_btc_premium=1.008))
        self.step(s, chain, clock)
        rows = s.conn.execute("SELECT path, size_usd, net_edge FROM episode_probes"
                              " WHERE pair='ETH/BTC' ORDER BY size_usd").fetchall()
        self.assertEqual(len(rows), len(s.cfg.PROBE_SIZES_USD))
        self.assertEqual(rows[0][0], "ETH/BTC uni-500>aero-ts100")
        for _path, size, net in rows:                                   # flat pools: same edge
            self.assertAlmostEqual(net, 0.699 - 1.06 / size, delta=0.01)

    def test_status_line_groups_extras(self):
        s, chain, clock, _ = self.scanner(self.chain(eth_btc_premium=1.008))
        block, results = self.step(s, chain, clock)
        line = status_line(block, results, extras=s.extra_names)
        self.assertIn("ETH best ", line)
        self.assertIn("BTC best ", line)
        self.assertIn("EXTRA best ETH/BTC dex>dex +0.6", line)
        self.assertNotIn("SOL best", line)


class TestPriceSnapshot(ExtraBase):
    """Bug fixed Nov 27, 2026: pools were SIZED with the previous scan's quote-token price
    but VALUED with this scan's, so the quote token's ~2s move showed up as fake edge.

    Hand-worked: VIRT/ETH pools are both at 0.0005 WETH per VIRT, so the true dex>dex gap
    is the same every scan (two 0.05% fees and gas: about -0.11%). Kraken ETH moves from
    $2,000 to $2,006 (+0.3%) between scans; the pools don't move.
        buggy: spend 100/2000 = 0.05 WETH, value what comes back at $2,006
               -> usd_back * 2006/2000 -> net about +0.3% better than it should be
        fixed: value it at the $2,000 it was sized with -> net unchanged
    ("Unchanged" to 0.001%: gas is still converted at the new ETH price, which moves the
    ~$0.0106 gas by 0.5% = $0.00005, i.e. 0.00005% of $100.)
    Same for ETH/BTC (quoted in cbBTC) when Kraken BTC moves."""

    def mids(self):
        return {("kraken", "ETHUSD"): 2000.0, ("kraken", "XBTUSD"): 60000.0,
                ("coinbase", "ETH-USD"): 2000.0, ("coinbase", "BTC-USD"): 60000.0,
                ("kraken", "SOLUSD"): 150.0, ("coinbase", "SOL-USD"): 150.0}

    def net_after_move(self, pair, moves):
        m = self.mids()
        s, chain, clock, _ = self.scanner(mids=m)
        before = self.step(s, chain, clock)[1][(pair, "dex>dex")]
        for k, v in moves.items():
            m[k] = v
        after = self.step(s, chain, clock)[1][(pair, "dex>dex")]
        return before, after

    def test_eth_move_adds_no_fake_edge_on_weth_quoted_pair(self):
        up = {("kraken", "ETHUSD"): 2006.0, ("coinbase", "ETH-USD"): 2006.0}
        before, after = self.net_after_move("VIRT/ETH", up)
        self.assertAlmostEqual(before.net_pct, (0.9995 ** 2 - 1) * 100, delta=0.02)
        self.assertAlmostEqual(after.net_pct, before.net_pct, delta=0.001)     # was +0.3 higher

    def test_eth_drop_adds_no_fake_loss(self):
        down = {("kraken", "ETHUSD"): 1990.0, ("coinbase", "ETH-USD"): 1990.0}
        before, after = self.net_after_move("VIRT/ETH", down)
        self.assertAlmostEqual(after.net_pct, before.net_pct, delta=0.001)     # was -0.5 lower

    def test_btc_move_adds_no_fake_edge_on_eth_btc(self):
        up = {("kraken", "XBTUSD"): 60300.0, ("coinbase", "BTC-USD"): 60300.0}
        before, after = self.net_after_move("ETH/BTC", up)
        self.assertAlmostEqual(after.net_pct, before.net_pct, delta=0.001)     # was +0.5 higher

    def test_next_scan_uses_the_new_price(self):
        """The new price isn't lost: the scan after the move sizes and values with it."""
        m = self.mids()
        s, chain, clock, _ = self.scanner(mids=m)
        self.step(s, chain, clock)
        m[("kraken", "ETHUSD")] = m[("coinbase", "ETH-USD")] = 2006.0
        self.step(s, chain, clock)
        r = self.step(s, chain, clock)[1][("VIRT/ETH", "dex>dex")]
        self.assertAlmostEqual(r.usd_back / r.size_usd, 0.9995 ** 2, places=5)
        self.assertAlmostEqual(s.quote_usds()["VIRT/ETH"], 2006.0)


class TestHelpers(unittest.TestCase):
    def test_token_problem(self):
        tok = {"address": "0xabc", "symbol": "SOL", "decimals": None}
        self.assertIsNone(token_problem(tok, ("SOL", 9)))
        self.assertIn("no response", token_problem(tok, None))
        self.assertIn("chain says SOLX", token_problem(tok, ("SOLX", 9)))
        self.assertIn("expected 6 decimals", token_problem(dict(tok, decimals=6), ("SOL", 9)))

    def test_median_price(self):
        self.assertEqual(dex.median_price([3.0, None, 1.0, 2.0]), 2.0)
        self.assertEqual(dex.median_price([1.0, 4.0]), 2.5)
        self.assertIsNone(dex.median_price([None]))

    def test_dex_mid(self):
        # pool: $100 buys 0.049975 (buy px 2001.0005); 0.05 sells for $99.95 (sell px 1999)
        q = dex.DexQuote("a", 0.05, 0.049975, 99.95, 0.0, 0.0)
        self.assertAlmostEqual(dex.dex_mid([q], 100.0), (100 / 0.049975 + 99.95 / 0.05) / 2)
        self.assertIsNone(dex.dex_mid([dex.DexQuote("b", 0.05, None, None, 0.0, 0.0)], 100.0))

    def test_quote_round_skips_a_pair_without_reference(self):
        cfg = make_config()
        chain = FakeChain()
        pair = cfg.PAIRS[0]
        pools = dex.discover_pools(chain, cfg.MULTICALL3, cfg.DEXES, pair["base"]["address"], USDC)
        rd = dex.quote_round(chain, cfg, {"ETH": (pair, pools), "X": (dict(pair, name="X"), pools)},
                             {"ETH": 2000.0}, 100.0)
        self.assertIn("ETH", rd.quotes)
        self.assertNotIn("X", rd.quotes)


if __name__ == "__main__":
    unittest.main()
