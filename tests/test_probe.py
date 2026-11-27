"""probe.py against the fake chain. Hand-worked numbers.

Fake pools: uni-500 prices ETH at $2,000, aero-ts100 at $2,020 (1% higher), both 0.05% fee.
Reference (CEX) price $2,000. Buy on uni-500, sell on aero-ts100, size S:
    ETH bought   = S / 2000 * 0.9995
    USDC back    = ETH bought * 2020 * 0.9995 = S * 0.9995^2 * 1.01 = S * 1.0089902525
    raw edge     = +0.89902525% at every size (flat pools, no slippage)
Gas per leg: (100k + 50k overhead) * (0.01 + 0.001 gwei) = 1.65e12 wei = $0.0033,
    plus L1 1e12 wei = $0.0020 -> $0.0053 per leg, $0.0106 per round trip
    net at $100   = 0.89902525 - 0.0106/100*100   = 0.88842525%
    net at $5,000 = 0.89902525 - 0.0106/5000*100  = 0.89881325%

Depth model (fakes.py): aero sell moves the price down 0.2% per $1,000 sold.
    The sell is quoted for x = S/2000 ETH, worth S*1.01 at $2,020, so the move is
    0.002 * 1.01 * S/1000 and USDC back = S * 1.0089902525 * (1 - move).
    $1,000: move 0.00202 -> raw = 1.0089902525 * 0.99798 - 1 = +0.6952% (shrinking)
    $5,000: move 0.0101  -> raw = 1.0089902525 * 0.9899  - 1 = -0.1202% (gone)
"""
import unittest

import db
import dex
import probe
from routes import RouteResult
from tests.fakes import FakeChain, USDC, WETH, make_config

UNI_QUOTER = "0x" + "a2" * 20
AERO_QUOTER = "0x" + "b2" * 20
SIZES = [100, 500, 1000, 2500, 5000]
BASEFEE, L1 = 10_000_000, 10 ** 12


def setup(**cfg_over):
    cfg = make_config(**{"PROBE_SIZES_USD": SIZES, **cfg_over})
    chain = FakeChain()
    chain.prices[(AERO_QUOTER, 100, WETH)] = (2020.0, 0.0005)
    pair = cfg.PAIRS[0]
    pools = dex.discover_pools(chain, cfg.MULTICALL3, cfg.DEXES, pair["base"]["address"], USDC)
    by_label = {p.label: p for p in pools}
    return cfg, chain, pair, pools, by_label["uni-500"], by_label["aero-ts100"]


def run(cfg, chain, pair, buy, sell):
    return probe.quote_sizes(chain, cfg, pair, buy, sell, SIZES, 2000.0, BASEFEE, L1, 2000.0)


class TestQuoteSizes(unittest.TestCase):
    def test_flat_pools_same_raw_edge_at_every_size(self):
        cfg, chain, pair, _, uni, aero = setup()
        res = run(cfg, chain, pair, uni, aero)
        self.assertEqual([r.size_usd for r in res], SIZES)
        for r in res:
            self.assertAlmostEqual(r.raw_edge, 0.89902525, places=4)
            self.assertAlmostEqual(r.gas_usd, 0.0106, places=6)
        self.assertAlmostEqual(res[0].net_edge, 0.88842525, places=4)
        self.assertAlmostEqual(res[-1].net_edge, 0.89881325, places=4)

    def test_slippage_shrinks_the_edge_as_size_grows(self):
        cfg, chain, pair, _, uni, aero = setup()
        chain.impact[(AERO_QUOTER, 100, WETH)] = 0.002
        res = {r.size_usd: r for r in run(cfg, chain, pair, uni, aero)}
        self.assertAlmostEqual(res[1000].raw_edge, (1.0089902525 * 0.99798 - 1) * 100, places=4)
        self.assertAlmostEqual(res[5000].raw_edge, (1.0089902525 * 0.9899 - 1) * 100, places=4)
        self.assertLess(res[5000].net_edge, 0)
        nets = [res[s].net_edge for s in SIZES]
        self.assertEqual(nets, sorted(nets, reverse=True))      # bigger = worse

    def test_sizes_the_pool_cannot_fill(self):
        cfg, chain, pair, _, uni, aero = setup()
        chain.max_usd[(UNI_QUOTER, 500, WETH)] = 2000          # buy pool holds about $2,000
        res = {r.size_usd: r for r in run(cfg, chain, pair, uni, aero)}
        self.assertTrue(res[1000].filled)
        for s in (2500, 5000):
            self.assertFalse(res[s].filled)
            self.assertIsNone(res[s].net_edge)
            self.assertIsNone(res[s].raw_edge)

    def test_zero_output_counts_as_cant_fill(self):
        cfg, chain, pair, _, uni, aero = setup()
        chain.prices[(UNI_QUOTER, 500, WETH)] = (1e30, 0.0005)    # buy returns 0 ETH
        res = run(cfg, chain, pair, uni, aero)
        self.assertTrue(all(not r.filled for r in res))
        chain.prices[(UNI_QUOTER, 500, WETH)] = (2000.0, 0.0005)
        chain.prices[(AERO_QUOTER, 100, WETH)] = (1e-30, 0.0005)  # sell returns 0 USDC
        res = run(cfg, chain, pair, uni, aero)
        self.assertTrue(all(not r.filled for r in res))

    def test_all_sizes_in_one_eth_call(self):
        cfg, chain, pair, _, uni, aero = setup()
        before = chain.calls
        run(cfg, chain, pair, uni, aero)
        self.assertEqual(chain.calls - before, 1)

    def test_prices_and_path(self):
        cfg, chain, pair, _, uni, aero = setup()
        r = run(cfg, chain, pair, uni, aero)[0]
        self.assertEqual(r.path, "ETH uni-500>aero-ts100")
        self.assertAlmostEqual(r.buy_px, 100 / (0.05 * 0.9995), places=2)   # 2001.00
        self.assertAlmostEqual(r.sell_px, 2020 * 0.9995, places=2)          # 2018.99

    def test_probe_line(self):
        cfg, chain, pair, _, uni, aero = setup()
        chain.max_usd[(UNI_QUOTER, 500, WETH)] = 2000
        line = probe.probe_line(run(cfg, chain, pair, uni, aero))
        self.assertTrue(line.startswith("PROBE ETH uni-500>aero-ts100: $100 +0.888% | $500 "))
        self.assertTrue(line.endswith("$2,500 can't fill | $5,000 can't fill"))


class Ep:
    def __init__(self, start_ts):
        self.start_ts = start_ts


def rr(net_pct, key="dex>dex", pair="ETH", buy="uni-500", sell="aero-ts100"):
    return RouteResult(key, pair, buy, sell, 100.0, 100.0 + net_pct, 0.0, 0.0, 0.0, 0.05)


class TestProber(unittest.TestCase):
    def make(self, **cfg_over):
        cfg, chain, pair, pools, _, _ = setup(**cfg_over)
        lines = []
        conn = db.connect(":memory:")
        p = probe.Prober(cfg, chain, conn, lines.append)
        return p, chain, {"ETH": (pair, pools)}, lines, conn

    def test_openings_only_big_dex_dex(self):
        p, *_ = self.make()
        results = {("ETH", "dex>dex"): rr(0.20),
                   ("BTC", "dex>dex"): rr(0.04, pair="BTC"),                   # under 0.05%
                   ("ETH", "dex>kraken_taker"): rr(0.50, key="dex>kraken_taker")}
        self.assertEqual([r.pair for r in p.openings(results, {})], ["ETH"])

    # Changed Nov 27, 2026. This used to check that an already-open episode is NEVER
    # probed. Now an open episode is probed once, the first scan it is at or above the
    # threshold (it may have opened below it); never again for the same episode.
    def test_open_episode_probed_once_when_it_crosses(self):
        p, chain, pair_pools, lines, conn = self.make()
        k = ("ETH", "dex>dex")
        ep = Ep(start_ts=1000.0)
        self.assertEqual(p.openings({k: rr(0.01)}, {k: ep}), [])            # opened small
        self.assertEqual(len(p.openings({k: rr(0.30)}, {k: ep})), 1)       # crossed
        p.probe(1004.0, 9, [rr(0.30)], pair_pools, {"ETH": 2000.0}, BASEFEE, L1)
        self.assertEqual(p.openings({k: rr(0.40)}, {k: ep}), [])            # same episode: done
        self.assertEqual(p.openings({k: rr(0.01)}, {k: ep}), [])
        self.assertEqual(p.openings({k: rr(0.40)}, {k: ep}), [])            # re-crossing: still done
        new_ep = Ep(start_ts=2000.0)
        self.assertEqual(len(p.openings({k: rr(0.30)}, {k: new_ep})), 1)   # a new episode is probed

    def test_crossing_probe_joins_its_episode_start(self):
        p, chain, pair_pools, lines, conn = self.make()
        k = ("ETH", "dex>dex")
        ep = Ep(start_ts=1000.0)
        p.openings({k: rr(0.01)}, {k: ep})
        opening = p.openings({k: rr(0.30)}, {k: ep})
        p.probe(1004.0, 9, opening, pair_pools, {"ETH": 2000.0}, BASEFEE, L1)
        self.assertEqual({r[0] for r in conn.execute("SELECT ts FROM episode_probes")}, {1000.0})

    def test_cap_refusal_is_retried_and_stored_once(self):
        p, chain, pair_pools, lines, conn = self.make(PROBE_MAX_PER_HOUR=1)
        a, b = ("ETH", "dex>dex"), ("BTC", "dex>dex")
        ep_a, ep_b = Ep(start_ts=10.0), Ep(start_ts=11.0)
        eps = {a: ep_a, b: ep_b}
        results = {a: rr(0.5), b: rr(0.6, pair="BTC")}
        pp = dict(pair_pools)
        pp["BTC"] = pair_pools["ETH"]                                  # same fake pools for BTC
        refs = {"ETH": 2000.0, "BTC": 2000.0}
        p.probe(20.0, 1, p.openings(results, eps), pp, refs, BASEFEE, L1)    # ETH probed, BTC refused
        p.probe(22.0, 2, p.openings(results, eps), pp, refs, BASEFEE, L1)    # BTC refused again
        skips = conn.execute("SELECT ts, strategy, path, ROUND(net_edge, 6), reason FROM probe_skips").fetchall()
        self.assertEqual(skips, [(20.0, "dex_arb_probe", "BTC uni-500>aero-ts100", 0.6, "hourly_cap")])
        self.assertEqual(p.skipped, 2)                                       # both refusals counted
        p.probe(3620.0, 3, p.openings(results, eps), pp, refs, BASEFEE, L1)  # an hour later: retried
        # (the fake BTC uses ETH's pools, so its rows say pair ETH: tell them apart by ts)
        self.assertEqual(sorted(conn.execute("SELECT DISTINCT ts FROM episode_probes")),
                         [(10.0,), (11.0,)])                                  # BTC probed at last

    def test_probe_block_is_the_probes_own(self):
        p, chain, pair_pools, lines, conn = self.make()
        chain.block = 123
        p.probe(1000.0, 7, [rr(0.5)], pair_pools, {"ETH": 2000.0}, BASEFEE, L1)
        rows = set(conn.execute("SELECT block, probe_block FROM episode_probes"))
        self.assertEqual(rows, {(7, 123)})                                 # trigger 7, probed at 123

    def test_probing_off_when_no_sizes(self):
        p, *_ = self.make(PROBE_SIZES_USD=[])
        self.assertEqual(p.openings({("ETH", "dex>dex"): rr(0.5)}, {}), [])

    def test_probe_stores_one_row_per_size_and_logs(self):
        p, chain, pair_pools, lines, conn = self.make()
        done = p.probe(1000.0, 7, [rr(0.5)], pair_pools, {"ETH": 2000.0}, BASEFEE, L1)
        self.assertEqual(list(done), ["ETH uni-500>aero-ts100"])
        rows = conn.execute("SELECT ts, block, route, pair, size_usd, strategy, venue FROM episode_probes"
                            " ORDER BY size_usd").fetchall()
        self.assertEqual(rows[0], (1000.0, 7, "dex>dex", "ETH", 100.0, "dex_arb_probe", "base"))
        self.assertEqual([r[4] for r in rows], [float(s) for s in SIZES])
        self.assertTrue(lines[0].startswith("PROBE ETH uni-500>aero-ts100:"))

    def test_hourly_cap(self):
        p, chain, pair_pools, lines, conn = self.make(PROBE_MAX_PER_HOUR=2)
        for t in (0.0, 10.0, 20.0):
            p.probe(t, 1, [rr(0.5)], pair_pools, {"ETH": 2000.0}, BASEFEE, L1)
        self.assertEqual(p.skipped, 1)
        p.probe(3600.0, 1, [rr(0.5)], pair_pools, {"ETH": 2000.0}, BASEFEE, L1)   # an hour later
        self.assertEqual(conn.execute("SELECT COUNT(DISTINCT ts) FROM episode_probes").fetchone(), (3,))

    def test_failure_is_logged_not_raised(self):
        p, chain, pair_pools, lines, conn = self.make()

        def down(*_a, **_k):
            raise ConnectionError("rpc down")

        chain.eth_call = down
        self.assertEqual(p.probe(0.0, 1, [rr(0.5)], pair_pools, {"ETH": 2000.0}, BASEFEE, L1), {})
        self.assertIn("probe failed for ETH uni-500>aero-ts100 (ConnectionError", lines[0])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM episode_probes").fetchone(), (0,))

    def test_cant_fill_stored_as_null(self):
        p, chain, pair_pools, lines, conn = self.make()
        chain.max_usd[(UNI_QUOTER, 500, WETH)] = 2000
        p.probe(0.0, 1, [rr(0.5)], pair_pools, {"ETH": 2000.0}, BASEFEE, L1)
        rows = conn.execute("SELECT size_usd, net_edge, usd_back FROM episode_probes"
                            " WHERE size_usd >= 2500").fetchall()
        self.assertEqual(rows, [(2500.0, None, None), (5000.0, None, None)])


if __name__ == "__main__":
    unittest.main()
