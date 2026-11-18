"""Aero (Slipstream V3) pools: found by tick spacing, quoted by POOL ADDRESS through the
Metaquoter, which returns no gas estimate (dex.AERO_QUOTE_GAS = 130,000 is used).

Fake Aero pools (tests/fakes.py, aero_pools): price is token b per token a, fee 0.05%.
    WETH/USDC at ts50:   $2,000, or $2,020 when "1% rich"
    WETH/cbBTC at ts10:  1/30 cbBTC per WETH (ETH $2,000, BTC $60,000)
    USDT/USDC at ts1:    0.9990 USDC per USDT

Hand-worked:
    $100 buy at $2,000, 0.05% fee   -> 100/2000 * 0.9995 = 0.049975 WETH
    0.05 WETH sold at $2,000        -> 0.05 * 2000 * 0.9995 = $99.95
    Aero gas per leg: (130k + 50k overhead) * (0.01 + 0.001 gwei) = 1.98e12 wei = $0.00396,
                      plus L1 1e12 wei = $0.002 -> $0.00596
    uni-500 buy ($2,000) -> aerov3-ts50 sell ($2,020): 100 * 0.9995^2 * 1.01 = 100.8990 back
"""
import io
import unittest
from contextlib import redirect_stdout

import abi
import db
import dex
import find_pool
import recorder
from eth_abi import decode, encode
from scanner import Scanner
from tests.fakes import (AERO_FACTORY, AERO_METAQUOTER, CBBTC, USDC, USDT, WETH, FakeChain,
                         FakeFetchers, make_config)

AERO_ETH = "0x" + "a5" * 20
AERO_CROSS = "0x" + "a6" * 20
AERO_USDT = "0x" + "a7" * 20
AERO_DEX = {"name": "aerov3", "kind": "aero", "factory": AERO_FACTORY, "quoter": AERO_METAQUOTER,
            "tiers": [1, 10, 50]}
BASEFEE, L1 = 10_000_000, 10 ** 12


def aero_config(**over):
    cfg = make_config(**over)
    cfg.DEXES = cfg.DEXES + [AERO_DEX]
    return cfg


def aero_chain(eth_price=2000.0):
    c = FakeChain()
    c.pools[(AERO_FACTORY, 50)] = AERO_ETH       # fake getPool ignores the tokens: one pool per tier
    c.pools[(AERO_FACTORY, 10)] = AERO_CROSS
    c.pools[(AERO_FACTORY, 1)] = AERO_USDT
    c.aero_pools[AERO_ETH] = (WETH, USDC, eth_price, 0.0005)
    c.aero_pools[AERO_CROSS] = (WETH, CBBTC, 1 / 30, 0.0005)
    c.aero_pools[AERO_USDT] = (USDT, USDC, 0.9990, 0.0005)
    return c


def aero_pool(address=AERO_ETH, tier=50):
    return dex.Pool("aerov3", "aero", tier, address, AERO_METAQUOTER)


class TestEncoding(unittest.TestCase):
    def test_metaquote_calldata(self):
        data = abi.encode_metaquote(AERO_ETH, WETH, 12345)
        self.assertEqual(data[:4], abi.selector("quoteExactInputSingleV3(address,address,uint256,uint160)"))
        self.assertEqual(decode(["address", "address", "uint256", "uint160"], data[4:]),
                         (AERO_ETH, WETH, 12345, 0))

    def test_metaquote_reply(self):
        self.assertEqual(abi.decode_metaquote(encode(["uint256", "uint160", "uint32"], [777, 5, 2])), 777)

    def test_get_pool_uses_int24_like_slipstream(self):
        data = abi.encode_get_pool("aero", WETH, USDC, 50)
        self.assertEqual(data[:4], abi.selector(abi.SIG_GET_POOL_SLIP))

    def test_quote_call_targets_metaquoter_with_pool_address(self):
        target, data = dex.quote_call(aero_pool(), USDC, WETH, 100)
        self.assertEqual(target, AERO_METAQUOTER)
        self.assertEqual(decode(["address", "address", "uint256", "uint160"], data[4:])[0], AERO_ETH)

    def test_quote_call_other_kinds_unchanged(self):
        p = dex.Pool("univ3", "uni", 500, "0x" + "c1" * 20, "0x" + "a2" * 20)
        self.assertEqual(dex.quote_call(p, USDC, WETH, 100),
                         ("0x" + "a2" * 20, abi.encode_quote("uni", USDC, WETH, 100, 500)))

    def test_read_quote(self):
        ok = (True, encode(["uint256", "uint160", "uint32"], [500, 0, 0]))
        self.assertEqual(dex.read_quote("aero", ok), (500, dex.AERO_QUOTE_GAS))
        self.assertEqual(dex.read_quote("aero", (False, b"")), (None, None))
        self.assertEqual(dex.read_quote("aero", (True, b"\x00" * 32)), (None, None))      # cut off
        zero = (True, encode(["uint256", "uint160", "uint32"], [0, 0, 0]))
        self.assertEqual(dex.read_quote("aero", zero), (None, None))
        uni = (True, encode(["uint256", "uint160", "uint32", "uint256"], [9, 0, 0, 88_000]))
        self.assertEqual(dex.read_quote("uni", uni), (9, 88_000))
        self.assertEqual(dex.read_quote("classic", (True, encode(["uint256"], [4]))), (4, None))

    def test_label(self):
        self.assertEqual(aero_pool().label, "aerov3-ts50")


class TestQuoting(unittest.TestCase):
    def test_discovered_and_quoted_in_the_same_call(self):
        cfg, chain = aero_config(), aero_chain()
        pair = cfg.PAIRS[0]
        pools = dex.discover_pools(chain, cfg.MULTICALL3, cfg.DEXES, pair["base"]["address"], USDC)
        self.assertIn("aerov3-ts50", [p.label for p in pools])
        pools = [p for p in pools if p.label == "aerov3-ts50"]
        before = chain.calls
        rd = dex.quote_round(chain, cfg, {"ETH": (pair, pools)}, {"ETH": 2000.0}, 100.0)
        self.assertEqual(chain.calls - before, 1)
        q = dex.to_dex_quotes(cfg, rd, pair, 2000.0)[0]
        self.assertAlmostEqual(q.buy_base_out, 0.049975)
        self.assertAlmostEqual(q.sell_usd_out, 99.95, delta=1e-5)
        self.assertAlmostEqual(q.buy_gas_usd, 0.00596)

    def test_pool_buy_prices(self):
        cfg, chain = aero_config(), aero_chain()
        px = dex.pool_buy_prices(chain, cfg, cfg.PAIRS[0], [aero_pool()], 1.0, 100.0)
        self.assertAlmostEqual(px["aerov3-ts50"], 100 / 0.049975, places=3)


def make_scanner(chain, **over):
    cfg = aero_config(**over)

    class Clock:
        t = 1_000_000.0

        def __call__(self):
            return self.t

    clock = Clock()
    mids = {("kraken", "ETHUSD"): 2000.0, ("kraken", "XBTUSD"): 60000.0,
            ("coinbase", "ETH-USD"): 2000.0, ("coinbase", "BTC-USD"): 60000.0}
    s = Scanner(cfg, chain, db.connect(":memory:"), fetchers=FakeFetchers(mids, clock).as_dict(), clock=clock)
    with redirect_stdout(io.StringIO()):
        s.startup()
    return s, clock


def step(s, chain, clock):
    chain.block, clock.t = chain.block + 1, clock.t + 2
    with redirect_stdout(io.StringIO()):
        return s.step()


class TestInScanner(unittest.TestCase):
    def test_eth_gap_to_aero_is_found_probed_and_logged(self):
        chain = aero_chain(eth_price=2020.0)                  # Aero 1% rich
        s, clock = make_scanner(chain)
        _, results = step(s, chain, clock)
        r = results[("ETH", "dex>dex")]
        self.assertEqual(r.sell_venue, "aerov3-ts50")
        self.assertAlmostEqual(r.usd_back, 100 * 0.9995 ** 2 * 1.01, delta=0.001)
        rows = s.conn.execute("SELECT size_usd, net_edge FROM episode_probes"
                              " WHERE path LIKE '%aerov3-ts50'").fetchall()
        self.assertEqual(len(rows), len(s.cfg.PROBE_SIZES_USD))       # probe went through Aero
        self.assertTrue(all(net is not None for _size, net in rows))
        s.shutdown()

    def test_probe_buying_on_aero(self):
        # Aero 1% CHEAP: buy on aerov3-ts50, sell on uni-500. Every probe size must fill.
        chain = aero_chain(eth_price=1980.0)
        s, clock = make_scanner(chain)
        _, results = step(s, chain, clock)
        self.assertEqual(results[("ETH", "dex>dex")].buy_venue, "aerov3-ts50")
        nets = [n for (n,) in s.conn.execute(
            "SELECT net_edge FROM episode_probes WHERE path LIKE 'ETH aerov3-ts50>%'")]
        self.assertEqual(len(nets), len(s.cfg.PROBE_SIZES_USD))
        self.assertTrue(all(n is not None and n > 0 for n in nets))
        s.shutdown()

    def test_triangle_probe_with_an_aero_leg(self):
        # Aero WETH->cbBTC 0.8% rich: loop USDC>WETH>cbBTC>USDC clears +0.02% and is probed
        # with Aero as its middle leg; every size must fill.
        chain = aero_chain()
        chain.aero_pools[AERO_CROSS] = (WETH, CBBTC, 1 / 30 * 1.008, 0.0005)
        s, clock = make_scanner(chain)
        step(s, chain, clock)
        rows = s.conn.execute("SELECT leg2, net_edge FROM tri_probes").fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(leg == "aerov3-ts10" for leg, _ in rows))
        self.assertTrue(all(net is not None for _, net in rows))
        s.shutdown()

    def test_triangle_uses_aero_cross_pool(self):
        # Aero's WETH/cbBTC pool pays 0.2% more cbBTC per WETH than uni-500, so the loop
        # USDC > WETH > cbBTC > USDC must take its middle leg (WETH -> cbBTC) on Aero.
        chain = aero_chain()
        chain.aero_pools[AERO_CROSS] = (WETH, CBBTC, 1 / 30 * 1.002, 0.0005)
        s, clock = make_scanner(chain)
        self.assertIn("aerov3-ts10", [p.label for p in s.cross[2]])
        step(s, chain, clock)
        fwd = [r for r in s.last_tri if r.path == "USDC>WETH>cbBTC>USDC"][0]
        self.assertEqual(fwd.legs[1], "aerov3-ts10")
        s.shutdown()

    def test_quiet_market_no_errors(self):
        chain = aero_chain()
        s, clock = make_scanner(chain)
        block, results = step(s, chain, clock)
        self.assertTrue(all(r.net_usd < 0 for r in results.values()))
        s.shutdown()


class TestRecorderAndFindPool(unittest.TestCase):
    def test_recorder_quotes_an_aero_pegged_pool(self):
        cfg, chain = aero_config(), aero_chain()
        rec = recorder.Recorder(cfg, chain, db.connect(":memory:"), lambda *_: None)
        rec.startup()
        quotes = rec.record(0.0, {"ETH": 2000.0, "BTC": 60000.0})
        q = [q for q in quotes if q.pair == "USDT/USDC" and q.pool == "aerov3-ts1"]
        self.assertEqual(len(q), 1)
        self.assertAlmostEqual(q[0].bid, 0.9990 * 0.9995, places=6)       # selling USDT
        self.assertAlmostEqual(q[0].ask, 0.9990 / 0.9995, places=6)       # buying USDT

    def test_find_pool_knows_aero(self):
        cfg, chain = aero_config(), aero_chain()
        chain.pool_info[AERO_ETH] = {"token0": WETH, "token1": USDC, "fee": 125,
                                     "tick_spacing": 50, "factory": AERO_FACTORY}
        out = "\n".join(find_pool.describe(chain, cfg, AERO_ETH))
        self.assertIn("dex     aerov3", out)
        self.assertIn("scanner aerov3-ts50: scanned", out)
        self.assertIn("fee     0.0125%", out)


if __name__ == "__main__":
    unittest.main()
