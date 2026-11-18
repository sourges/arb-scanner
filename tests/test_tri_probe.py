"""Triangle size probes against the fake chain. Hand-worked numbers:

ETH $2,000, BTC $60,000, every pool 0.05% fee, WETH/cbBTC pools at 1 WETH = 1/30 cbBTC.
Gas per leg $0.0053 (see test_dex.py), three legs = $0.0159.

No arb (every pool agrees), size S:
    usd_back = S * 0.9995^3 = S * 0.998500750125   -> raw -0.149925% at every size
    net $100   = -0.149925 - 0.0159/100*100   = -0.165825%
    net $1,000 = -0.149925 - 0.0159/1000*100  = -0.151515%
    net $5,000 = -0.149925 - 0.0159/5000*100  = -0.150243%

Mispriced: aero's WETH/cbBTC pool pays 1/30 * 1.008 cbBTC per WETH. The forward loop
(USDC>WETH>cbBTC>USDC) uses aero for leg 2:
    usd_back = S * 0.998500750125 * 1.008 = S * 1.006488756   -> raw +0.648876%
    net $100 = +0.632976%
Depth model on that aero leg: price moves 0.2% per $1,000 of WETH sold into it.
Leg 2 sells x1 = S/2000 WETH, worth S, so move = 0.002 * S/1000:
    $1,000: 1.006488756 * 0.998 - 1 = +0.447578% raw  (shrinking)
    $5,000: 1.006488756 * 0.990 - 1 = -0.357612% raw  (gone)
And with aero's leg capped at $2,500: $5,000 can't fill (NULL edges).
cbBTC amounts are whole satoshis, so comparisons allow 0.001%.
"""
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

import db
import dex
import report
import tri_probe
from rpc import RpcError
from scanner import Scanner
from tests.fakes import CBBTC, WETH, FakeChain, FakeFetchers, make_config

UNI_QUOTER = "0x" + "a2" * 20
AERO_QUOTER = "0x" + "b2" * 20
SIZES = [100, 500, 1000, 2500, 5000]
REFS = {"ETH": 2000.0, "BTC": 60000.0}
FWD = "USDC>WETH>cbBTC>USDC"
REV = "USDC>cbBTC>WETH>USDC"
AERO_CROSS = (AERO_QUOTER, 100, WETH, CBBTC)
D = 1e-3


def add_cross(chain, aero_premium=1.0):
    chain.pegged[(UNI_QUOTER, 500, WETH, CBBTC)] = (1 / 30, 0.0005)
    chain.pegged[AERO_CROSS] = (1 / 30 * aero_premium, 0.0005)


def pools_for(chain, cfg):
    eth, btc = cfg.PAIRS
    found = lambda a, b: {p.label: p for p in dex.discover_pools(chain, cfg.MULTICALL3, cfg.DEXES, a, b)}
    return (found(eth["base"]["address"], cfg.USDC["address"]),
            found(WETH, CBBTC),
            found(btc["base"]["address"], cfg.USDC["address"]))


def probe_fwd(chain, leg2="aero-ts100"):
    cfg = make_config(PROBE_SIZES_USD=SIZES)
    eth_pools, cross, btc_pools = pools_for(chain, cfg)
    before = chain.calls
    res = tri_probe.quote_sizes(chain, cfg, FWD, cfg.PAIRS[0]["base"], cfg.PAIRS[1]["base"],
                                (eth_pools["uni-500"], cross[leg2], btc_pools["uni-500"]), SIZES,
                                2000.0, 60000.0, 10_000_000, 10 ** 12, 2000.0, trigger_net=0.5)
    return {r.size_usd: r for r in res}, chain.calls - before


def tri(path, net):
    """Stand-in for a triangle.TriResult with an exact net edge."""
    return SimpleNamespace(path=path, net_edge=net, legs=("a", "b", "c"))


class TestQuoteSizes(unittest.TestCase):
    def test_no_arb_hand_worked(self):
        chain = FakeChain()
        add_cross(chain)
        res, calls = probe_fwd(chain)
        self.assertEqual(calls, 1)                                      # ONE eth_call for all sizes
        for size, r in res.items():
            self.assertAlmostEqual(r.usd_back, size * 0.998500750125, delta=size * D / 100)
            self.assertAlmostEqual(r.raw_edge, -0.149925, delta=D)
            self.assertAlmostEqual(r.gas_usd, 0.0159, delta=1e-9)
        self.assertAlmostEqual(res[100].net_edge, -0.165825, delta=D)
        self.assertAlmostEqual(res[1000].net_edge, -0.151515, delta=D)
        self.assertAlmostEqual(res[5000].net_edge, -0.150243, delta=D)

    def test_gap_shrinks_with_size_then_goes(self):
        chain = FakeChain()
        add_cross(chain, aero_premium=1.008)
        chain.peg_impact[AERO_CROSS] = 0.002
        res, _ = probe_fwd(chain)
        self.assertAlmostEqual(res[100].raw_edge, 0.648876 - 0.02, delta=0.002)   # $100 moves 0.02%
        self.assertAlmostEqual(res[1000].raw_edge, 0.447578, delta=D)
        self.assertAlmostEqual(res[5000].raw_edge, -0.357612, delta=D)
        self.assertGreater(res[100].net_edge, res[1000].net_edge)
        self.assertEqual(res[100].path, f"{FWD} uni-500/aero-ts100/uni-500")
        self.assertEqual(res[100].trigger_net, 0.5)

    def test_size_a_leg_cant_fill_is_null(self):
        chain = FakeChain()
        add_cross(chain, aero_premium=1.008)
        chain.peg_max_usd[AERO_CROSS] = 2500
        res, _ = probe_fwd(chain)
        self.assertTrue(res[2500].filled)
        self.assertFalse(res[5000].filled)
        self.assertIsNone(res[5000].raw_edge)
        self.assertIsNone(res[5000].net_edge)
        self.assertIn("$5,000 can't fill", tri_probe.probe_line(list(res.values())))

    def test_any_single_leg_failing_is_cant_fill(self):
        for leg in range(3):
            chain = FakeChain()
            add_cross(chain, aero_premium=1.008)
            if leg == 0:
                chain.max_usd[(UNI_QUOTER, 500, WETH)] = 2500
            elif leg == 1:
                chain.peg_max_usd[AERO_CROSS] = 2500
            else:
                chain.max_usd[(UNI_QUOTER, 500, CBBTC)] = 2500
            res, _ = probe_fwd(chain)
            self.assertTrue(res[2500].filled, leg)
            self.assertFalse(res[5000].filled, leg)

    def test_flat_pools_no_arb_at_unmispriced_pool(self):
        chain = FakeChain()
        add_cross(chain, aero_premium=1.008)
        res, _ = probe_fwd(chain, leg2="uni-500")                       # the fair pool
        self.assertAlmostEqual(res[1000].raw_edge, -0.149925, delta=D)


class TestOpenings(unittest.TestCase):
    def make(self, **over):
        cfg = make_config(**{"PROBE_SIZES_USD": SIZES, "TRI_PROBE_MIN_NET_PCT": 0.02, **over})
        return tri_probe.TriProber(cfg, None, None, lambda m: None, lambda now: True)

    def test_crossing_rules(self):
        p = self.make()
        self.assertEqual([r.path for r in p.openings([tri(FWD, -0.03), tri(REV, 0.05)])], [REV])  # unseen + above
        self.assertEqual(p.openings([tri(FWD, -0.01), tri(REV, 0.06)]), [])                     # REV stays open
        self.assertEqual([r.path for r in p.openings([tri(FWD, 0.02), tri(REV, 0.01)])], [FWD])  # exactly at threshold
        self.assertEqual([r.path for r in p.openings([tri(FWD, 0.03), tri(REV, 0.04)])], [REV])  # REV re-opens
        p.openings([tri(FWD, 0.03)])                                    # REV missing = below
        self.assertEqual([r.path for r in p.openings([tri(FWD, 0.03), tri(REV, 0.04)])], [REV])

    def test_off_switches(self):
        self.assertEqual(self.make(TRI_PROBE_MIN_NET_PCT=None).openings([tri(FWD, 0.5)]), [])
        self.assertEqual(self.make(PROBE_SIZES_USD=[]).openings([tri(FWD, 0.5)]), [])


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def make_scanner(chain, **over):
    cfg = make_config(**{"PROBE_SIZES_USD": SIZES, **over})
    clock = Clock()
    mids = {("kraken", "ETHUSD"): 2000.0, ("kraken", "XBTUSD"): 60000.0,
            ("coinbase", "ETH-USD"): 2000.0, ("coinbase", "BTC-USD"): 60000.0}
    s = Scanner(cfg, chain, db.connect(":memory:"), fetchers=FakeFetchers(mids, clock).as_dict(), clock=clock)
    with redirect_stdout(io.StringIO()):
        s.startup()
    return s, clock


def step(s, chain, clock):
    chain.block, clock.t = chain.block + 1, clock.t + 2
    out = io.StringIO()
    with redirect_stdout(out):
        s.step()
    return out.getvalue()


def rows(s):
    return s.conn.execute("SELECT ts, strategy, venue, path, size_usd, net_edge, loop, leg2, trigger_net"
                          " FROM tri_probes ORDER BY ts, size_usd").fetchall()


class TestInScanner(unittest.TestCase):
    def test_opening_loop_is_probed_once_in_shared_format(self):
        chain = FakeChain()
        add_cross(chain, aero_premium=1.008)
        s, clock = make_scanner(chain)
        log = step(s, chain, clock)
        r = rows(s)
        self.assertEqual(len(r), len(SIZES))                            # forward only; reverse is negative
        for row in r:
            self.assertEqual(row[1:3], ("tri_arb_probe", "base"))
            self.assertEqual((row[6], row[7]), (FWD, "aero-ts100"))
            self.assertAlmostEqual(row[8], 0.632976, delta=D)           # the $100 net that opened it
        self.assertEqual([row[4] for row in r], SIZES)
        self.assertIn("PROBE TRI USDC>WETH>cbBTC>USDC", log)
        step(s, chain, clock)                                           # still open: no second probe
        self.assertEqual(len(rows(s)), len(SIZES))
        s.shutdown()

    def test_reverse_loop_probes_in_its_own_order(self):
        """aero pays 1.008x the fair WETH per cbBTC, so the REVERSE loop opens:
        USDC -> cbBTC (uni) -> WETH (aero) -> USDC (uni) = S * 0.998500750125 * 1.008,
        raw +0.648876% at every size (flat pools)."""
        chain = FakeChain()
        add_cross(chain, aero_premium=1 / 1.008)
        s, clock = make_scanner(chain)
        step(s, chain, clock)
        r = s.conn.execute("SELECT loop, leg2, size_usd, raw_edge FROM tri_probes ORDER BY size_usd").fetchall()
        self.assertEqual(len(r), len(SIZES))
        for loop, leg2, _size, raw in r:
            self.assertEqual((loop, leg2), (REV, "aero-ts100"))
            self.assertAlmostEqual(raw, 0.648876, delta=D)
        s.shutdown()

    def test_closes_and_reopens(self):
        chain = FakeChain()
        add_cross(chain, aero_premium=1.008)
        s, clock = make_scanner(chain)
        step(s, chain, clock)
        add_cross(chain)                                                # gap closes
        step(s, chain, clock)
        add_cross(chain, aero_premium=1.008)                            # and opens again
        step(s, chain, clock)
        self.assertEqual(len(rows(s)), 2 * len(SIZES))
        s.shutdown()

    def test_one_extra_eth_call_per_probe(self):
        chain = FakeChain()
        add_cross(chain)
        s, clock = make_scanner(chain)
        step(s, chain, clock)                                           # quiet scan: one call
        quiet = chain.calls
        step(s, chain, clock)
        one_scan = chain.calls - quiet
        add_cross(chain, aero_premium=1.008)
        before = chain.calls
        step(s, chain, clock)
        self.assertEqual(chain.calls - before, one_scan + 1)
        s.shutdown()

    def test_has_its_own_hourly_cap(self):
        # The dex>dex cap at 0 must NOT stop triangle probes (separate budgets since Nov 18).
        chain = FakeChain()
        add_cross(chain, aero_premium=1.008)
        s, clock = make_scanner(chain, PROBE_MAX_PER_HOUR=0)
        step(s, chain, clock)
        self.assertNotEqual(rows(s), [])
        self.assertEqual(s.tri_cap.skipped, 0)
        s.shutdown()

    def test_own_cap_reached_skips_only_triangle_probes(self):
        chain = FakeChain()
        add_cross(chain, aero_premium=1.008)
        s, clock = make_scanner(chain, TRI_PROBE_MAX_PER_HOUR=0)
        step(s, chain, clock)
        self.assertEqual(rows(s), [])
        self.assertEqual(s.tri_cap.skipped, 1)                          # counted on its own cap
        self.assertEqual(s.prober.skipped, 0)                           # dex>dex budget untouched
        s.shutdown()

    def test_busy_triangle_cannot_use_up_dex_dex_probes(self):
        # Spend the whole triangle budget, then check the dex>dex cap still has all of it.
        chain = FakeChain()
        s, clock = make_scanner(chain, TRI_PROBE_MAX_PER_HOUR=3, PROBE_MAX_PER_HOUR=3)
        for _ in range(5):
            s.tri_cap.allow(clock())
        self.assertEqual(s.tri_cap.skipped, 2)
        self.assertTrue(all(s.prober.allow(clock()) for _ in range(3)))
        self.assertFalse(s.prober.allow(clock()))
        s.shutdown()

    def test_off_switch(self):
        chain = FakeChain()
        add_cross(chain, aero_premium=1.008)
        s, clock = make_scanner(chain, TRI_PROBE_MIN_NET_PCT=None)
        step(s, chain, clock)
        self.assertEqual(rows(s), [])
        s.shutdown()

    def test_failed_probe_is_logged_and_scan_goes_on(self):
        chain = FakeChain()
        add_cross(chain, aero_premium=1.008)
        s, clock = make_scanner(chain)

        class Down:
            def eth_call(self, *a, **k):
                raise RpcError("cut off")
        s.tri_prober.rpc = Down()
        log = step(s, chain, clock)
        self.assertIn("tri probe failed", log)
        self.assertEqual(rows(s), [])
        self.assertEqual(s.conn.execute("SELECT COUNT(*) FROM tri_quotes").fetchone()[0], 2)
        s.shutdown()


class TestReport(unittest.TestCase):
    """Hand-built rows:
    tri_quotes  FWD nets -0.03, -0.01, +0.05 -> 3 scans, 33.33% > 0, median -0.010, best +0.050
                REV nets -0.02, -0.04       -> 2 scans, 0.00% > 0, median -0.030, best -0.020
    tri_probes  one probe: $100 +0.031, $500 -0.012, $5,000 can't fill
    """

    def test_triangle_sections(self):
        conn = db.connect(":memory:")
        tri_rows = [(FWD, -0.03), (FWD, -0.01), (FWD, 0.05), (REV, -0.02), (REV, -0.04)]
        for i, (path, net) in enumerate(tri_rows):
            conn.execute("INSERT INTO tri_quotes (ts, path, net_edge) VALUES (?,?,?)", (1000 + i, path, net))
        res = [tri_probe.TriProbeResult(FWD, ("p1", "p2", "p3"), z, back, 0.0, 0.04)
               for z, back in ((100, 100.031), (500, 499.94), (5000, None))]
        db.insert_tri_probes(conn, 2000.0, 7, res)
        out = io.StringIO()
        with redirect_stdout(out):
            report.print_report(conn, 0, 0.05, 15)
        text = out.getvalue()
        self.assertIn("TRIANGLE (every scan", text)
        fwd = next(l for l in text.splitlines() if l.startswith(FWD) and "%" in l and "p1" not in l)
        self.assertEqual(fwd.split()[1:], ["3", "33.33%", "-0.010%", "+0.050%"])
        rev = next(l for l in text.splitlines() if l.startswith(REV))
        self.assertEqual(rev.split()[1:], ["2", "0.00%", "-0.030%", "-0.020%"])
        self.assertIn("TRIANGLE SIZE PROBES", text)
        self.assertIn(f"{FWD} p1/p2/p3", text)
        self.assertIn("can't fill", text)
        self.assertEqual(report.probe_summary(conn, 0, "tri_probes")[0][:4], (100.0, 1, 1, 1))
        self.assertEqual(report.probe_summary(conn, 0, "episode_probes"), [])   # not mixed in

    def test_old_database_without_triangle_tables(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE minute_stats (minute, pair, route, samples, positives, best_net_pct, avg_net_pct)")
        self.assertEqual(report.tri_summary(conn, 0), [])
        self.assertEqual(report.probe_summary(conn, 0, "tri_probes"), [])

    def test_connect_adds_tri_probes_to_an_existing_database(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.sqlite")
            conn = db.connect(path)
            conn.execute("DROP TABLE tri_probes")                       # a database from before today
            conn.execute("INSERT INTO tri_quotes (ts, path, net_edge) VALUES (1, 'x', 0.1)")
            conn.commit()
            conn.close()
            conn = db.connect(path)
            self.assertTrue(report.has_table(conn, "tri_probes"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tri_quotes").fetchone()[0], 1)  # data kept
            conn.close()


if __name__ == "__main__":
    unittest.main()
