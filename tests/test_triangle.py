"""Triangle (three-way) arb against the fake chain. Hand-worked numbers:

ETH $2000, BTC $60000, every pool charges 0.05%. WETH/cbBTC pools price
1 WETH = 2000/60000 = 1/30 cbBTC.

Forward USDC -> WETH -> cbBTC -> USDC, $100:
    leg 1  100 / 2000 * 0.9995          = 0.049975 WETH
    leg 2  0.049975 / 30 * 0.9995       = 0.00166500 cbBTC
    leg 3  0.00166500 * 60000 * 0.9995  = $99.850075  (= 100 * 0.9995^3)
    raw_edge = -0.149925%
    gas = 3 legs * $0.0053 = $0.0159  ->  net_edge = (99.850075 - 0.0159)/100 - 1 = -0.165825%
The reverse loop is the same by symmetry. No arb when every pool agrees.

Mispriced: aero's WETH/cbBTC pool pays 1/30 * 1.008 cbBTC per WETH (WETH 0.8% rich there).
Forward picks aero for leg 2: 99.850075 * 1.008 = $100.648876 back, net +0.632976%.
Reverse buys WETH with cbBTC, so it avoids aero (WETH costs more there) and stays at uni.
"""
import io
import unittest
from contextlib import redirect_stdout

import db
import dex
import triangle
from scanner import Scanner, status_line
from tests.fakes import CBBTC, WETH, FakeChain, FakeFetchers, make_config

PRICES = {"ETH": 2000.0, "BTC": 60000.0}
UNI_QUOTER = "0x" + "a2" * 20
AERO_QUOTER = "0x" + "b2" * 20
GAS3 = 3 * 0.0053
FWD = "USDC>WETH>cbBTC>USDC"
REV = "USDC>cbBTC>WETH>USDC"


def add_cross(chain, aero_premium=1.0):
    """WETH/cbBTC pools on both fake DEXes (the fake 'pegged' table quotes any token pair)."""
    chain.pegged[(UNI_QUOTER, 500, WETH, CBBTC)] = (1 / 30, 0.0005)
    chain.pegged[(AERO_QUOTER, 100, WETH, CBBTC)] = (1 / 30 * aero_premium, 0.0005)


def run_round(chain):
    cfg = make_config()
    pairs = {p["name"]: p for p in cfg.PAIRS}
    pair_pools = {n: (p, dex.discover_pools(chain, cfg.MULTICALL3, cfg.DEXES, p["base"]["address"],
                                            cfg.USDC["address"])) for n, p in pairs.items()}
    cross_pools = dex.discover_pools(chain, cfg.MULTICALL3, cfg.DEXES, WETH, CBBTC)
    calls_before = chain.calls
    rd = dex.quote_round(chain, cfg, pair_pools, PRICES, 100.0,
                         cross=(pairs["ETH"], pairs["BTC"], cross_pools))
    rounds = chain.calls - calls_before
    a_to_b, b_to_a = triangle.cross_legs(cfg, rd, pairs["ETH"], pairs["BTC"], eth_usd=2000.0)
    results = triangle.compute(100.0, "WETH", "cbBTC",
                               dex.to_dex_quotes(cfg, rd, pairs["ETH"], 2000.0),
                               dex.to_dex_quotes(cfg, rd, pairs["BTC"], 2000.0),
                               a_to_b, b_to_a)
    return {r.path: r for r in results}, rounds


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def make_scanner(chain, **cfg_overrides):
    cfg = make_config(**cfg_overrides)
    clock = Clock()
    mids = {("kraken", "ETHUSD"): 2000.0, ("kraken", "XBTUSD"): 60000.0,
            ("coinbase", "ETH-USD"): 2000.0, ("coinbase", "BTC-USD"): 60000.0}
    s = Scanner(cfg, chain, db.connect(":memory:"),
                fetchers=FakeFetchers(mids, clock).as_dict(), clock=clock)
    with redirect_stdout(io.StringIO()):
        s.startup()
    return s, clock


class TestTriangleMath(unittest.TestCase):
    def test_no_arb_hand_worked(self):
        chain = FakeChain()
        add_cross(chain)
        res, _ = run_round(chain)
        self.assertEqual(set(res), {FWD, REV})
        for r in res.values():
            self.assertAlmostEqual(r.usd_back, 99.850075, delta=1e-3)   # whole satoshis: ~$0.0006
            self.assertAlmostEqual(r.raw_edge, -0.149925, delta=1e-3)
            self.assertAlmostEqual(r.gas_usd, GAS3, delta=1e-9)
            self.assertAlmostEqual(r.net_edge, -0.165825, delta=1e-3)

    def test_mispriced_cross_pool_shows_profit_and_picks_it(self):
        chain = FakeChain()
        add_cross(chain, aero_premium=1.008)
        res, _ = run_round(chain)
        fwd, rev = res[FWD], res[REV]
        self.assertAlmostEqual(fwd.usd_back, 100.648876, delta=1e-3)
        self.assertAlmostEqual(fwd.net_edge, 0.632976, delta=1e-3)
        self.assertEqual(fwd.legs[1], "aero-ts100")
        self.assertEqual(rev.legs[1], "uni-500")                    # aero is the bad side for reverse
        self.assertLess(rev.net_edge, 0)

    def test_cross_quotes_ride_in_the_same_eth_call(self):
        chain = FakeChain()
        add_cross(chain)
        _, rounds = run_round(chain)
        self.assertEqual(rounds, 1)

    def test_missing_cross_leg_drops_the_loops(self):
        chain = FakeChain()                                          # no WETH/cbBTC quotes: they revert
        res, _ = run_round(chain)
        self.assertEqual(res, {})

    def test_one_dead_pool_uses_the_other(self):
        chain = FakeChain()
        add_cross(chain)
        del chain.pegged[(UNI_QUOTER, 500, WETH, CBBTC)]
        res, _ = run_round(chain)
        self.assertEqual(res[FWD].legs[1], "aero-ts100")
        self.assertAlmostEqual(res[FWD].usd_back, 99.850075, delta=1e-3)

    def test_best_leg_rules(self):
        self.assertIsNone(triangle.best([]))
        a = triangle.Leg("a", 1.0, 0.02)
        b = triangle.Leg("b", 1.0, 0.01)
        c = triangle.Leg("c", 0.9, 0.0)
        self.assertEqual(triangle.best([a, b, c]).pool, "b")         # same rate: cheaper gas wins
        self.assertEqual(triangle.best([c, triangle.Leg("d", 1.1, 5.0)]).pool, "d")


class TestTriangleInScanner(unittest.TestCase):
    def test_step_logs_both_loops_in_shared_format(self):
        chain = FakeChain()
        add_cross(chain)
        s, clock = make_scanner(chain)
        self.assertIsNotNone(s.cross)
        chain.block, clock.t = 101, clock.t + 2
        block, results = s.step()
        rows = s.conn.execute(
            "SELECT ts, strategy, venue, path, size_usd, raw_edge, net_edge, block, gas_usd, leg1, leg2, leg3"
            " FROM tri_quotes ORDER BY path").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r[3] for r in rows}, {FWD, REV})
        for r in rows:
            self.assertEqual((r[0], r[1], r[2], r[4], r[7]), (clock.t, "tri_arb", "base", 100.0, 101))
            self.assertAlmostEqual(r[6], -0.165825, delta=1e-3)
        self.assertIn("TRI best", status_line(block, results, tri=s.last_tri))
        s.shutdown()

    def test_every_scan_writes_rows(self):
        chain = FakeChain()
        add_cross(chain)
        s, clock = make_scanner(chain)
        for i in range(3):
            chain.block, clock.t = 101 + i, clock.t + 2
            s.step()
        self.assertEqual(s.conn.execute("SELECT COUNT(*) FROM tri_quotes").fetchone()[0], 6)
        s.shutdown()

    def test_turned_off_with_none(self):
        chain = FakeChain()
        add_cross(chain)
        s, clock = make_scanner(chain, TRIANGLE_PAIRS=None)
        self.assertIsNone(s.cross)
        chain.block, clock.t = 101, clock.t + 2
        block, results = s.step()
        self.assertEqual(s.conn.execute("SELECT COUNT(*) FROM tri_quotes").fetchone()[0], 0)
        self.assertNotIn("TRI", status_line(block, results))
        s.shutdown()

    def test_bad_pair_name_turns_triangle_off_not_scanner(self):
        chain = FakeChain()
        s, clock = make_scanner(chain, TRIANGLE_PAIRS=("ETH", "DOGE"))
        self.assertIsNone(s.cross)
        chain.block, clock.t = 101, clock.t + 2
        self.assertIsNotNone(s.step())
        s.shutdown()

    def test_no_quotes_this_block_writes_nothing(self):
        chain = FakeChain()                                          # pools found, quotes revert
        s, clock = make_scanner(chain)
        chain.block, clock.t = 101, clock.t + 2
        s.step()
        self.assertEqual(s.last_tri, [])
        self.assertEqual(s.conn.execute("SELECT COUNT(*) FROM tri_quotes").fetchone()[0], 0)
        s.shutdown()

    def test_print_table(self):
        chain = FakeChain()
        add_cross(chain)
        res, _ = run_round(chain)
        buf = io.StringIO()
        with redirect_stdout(buf):
            triangle.print_table(list(res.values()))
            triangle.print_table([])
        self.assertIn(FWD, buf.getvalue())
        self.assertIn("no triangle this block", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
