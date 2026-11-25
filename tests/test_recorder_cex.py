"""Recorder: Coinbase best bid/ask in the same minute as the Base quotes (Nov 25, 2026).

Hand-worked, fake chain + fake Coinbase:
    Base USDT/USDC (uni-500, price 1.0, fee 0.01%): bid 0.9999, ask 1 / 0.9999 = 1.00010001
        -> best mid 1.0000000 (off by ~0.0000005 %, so edges are checked to 5 decimal places)
    Coinbase USDT-USDC book: bid 0.99995 x 5000, ask 1.00005 x 7000 -> mid 1.00000
        raw_edge = 1.00000 / 1.00000 - 1 = 0.000 %
    Coinbase book moved to mid 1.0010 (bid 1.00095, ask 1.00105):
        raw_edge = 1.0010 / 1.0000 - 1 = +0.100 % (Coinbase 10 bp rich vs Base)
    Coinbase round trip on the book: 1.00105 / 1.00095 - 1 = 0.999 bp (prints as 1.0)
A Coinbase failure (down, slow, unknown product) logs a line and drops only that row;
the Base rows of the minute are still stored. If the Base quote fails, the Coinbase row
is still stored, with block and raw_edge = None.
"""
import io
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout

import db
import recorder
from cex import Book
from rpc import RpcError
from tests.fakes import FakeChain, make_config

REF = {"ETH": 2000.0, "BTC": 60000.0}
CEX = [{"pair": "USDT/USDC", "venue": "coinbase", "product": "USDT-USDC"}]


class FakeCoinbase:
    def __init__(self):
        self.books = {"USDT-USDC": Book("coinbase", [(0.99995, 5000.0)], [(1.00005, 7000.0)])}
        self.down = False
        self.delay = 0.0
        self.asked = []

    def __call__(self, product):
        self.asked.append(product)
        if self.delay:
            time.sleep(self.delay)
        if self.down:
            raise ConnectionError("coinbase down")
        return self.books[product]


def make(cex=CEX, executor=None, timeout=4.0):
    cfg = make_config(RECORD_CEX=cex, RECORD_CEX_TIMEOUT_S=timeout)
    chain = FakeChain()
    cb = FakeCoinbase()
    lines = []
    rec = recorder.Recorder(cfg, chain, db.connect(":memory:"), lines.append,
                            cex_fetchers={"coinbase": cb}, executor=executor)
    rec.startup()
    return rec, chain, cb, lines


def cex_rows(rec):
    return rec.conn.execute("SELECT ts, strategy, venue, path, size_usd, raw_edge, net_edge, block,"
                            " product, bid, ask, bid_size, ask_size FROM cex_quotes").fetchall()


class TestCexQuotes(unittest.TestCase):
    def test_hand_worked_row(self):
        rec, _, cb, _ = make()
        rec.record(1_000_000.0, REF)
        (row,) = cex_rows(rec)
        ts, strategy, venue, path, size, edge, net, block, product, bid, ask, bs, as_ = row
        self.assertEqual((ts, strategy, venue, path, product), (1_000_000.0, "peg_cex", "coinbase",
                                                                "USDT/USDC", "USDT-USDC"))
        self.assertEqual((size, net, block), (None, None, 100))
        self.assertEqual((bid, ask, bs, as_), (0.99995, 1.00005, 5000.0, 7000.0))
        self.assertAlmostEqual(edge, 0.0, places=5)
        self.assertEqual(cb.asked, ["USDT-USDC"])

    def test_edge_when_coinbase_is_rich(self):
        rec, _, cb, _ = make()
        cb.books["USDT-USDC"] = Book("coinbase", [(1.00095, 1.0)], [(1.00105, 1.0)])
        rec.record(1_000_000.0, REF)
        self.assertAlmostEqual(rec.last_cex[0].raw_edge, 0.100, places=5)
        self.assertAlmostEqual(recorder.round_trip_bp(1.00095, 1.00105), 0.999, places=3)

    def test_coinbase_down_keeps_base_rows(self):
        rec, _, cb, lines = make()
        cb.down = True
        quotes = rec.record(1_000_000.0, REF)
        self.assertTrue(quotes)
        self.assertGreater(rec.conn.execute("SELECT COUNT(*) FROM pegged_quotes").fetchone()[0], 0)
        self.assertEqual(cex_rows(rec), [])
        self.assertEqual(rec.cex_failures, 1)
        self.assertIn("recorder: coinbase USDT-USDC skipped (ConnectionError: coinbase down)", lines[-1])

    def test_unknown_product_is_skipped_not_fatal(self):
        rec, _, _, lines = make(cex=CEX + [{"pair": "cbETH/WETH", "venue": "coinbase",
                                             "product": "NOPE-ETH"}])
        rec.record(1_000_000.0, REF)
        self.assertEqual([r[3] for r in cex_rows(rec)], ["USDT/USDC"])
        self.assertTrue(any("NOPE-ETH skipped (KeyError" in line for line in lines))

    def test_base_failure_keeps_coinbase_row(self):
        rec, chain, _, lines = make()

        def boom(*_a, **_k):
            raise RpcError("rate limited")
        chain.eth_call = boom
        self.assertEqual(rec.record(1_000_000.0, REF), [])
        (row,) = cex_rows(rec)
        self.assertEqual((row[3], row[5], row[7]), ("USDT/USDC", None, None))   # no edge, no block
        self.assertIn("minute skipped (RpcError", "".join(lines))

    def test_pair_not_recorded_on_base_has_no_edge(self):
        rec, _, cb, _ = make(cex=[{"pair": "cbETH/WETH", "venue": "coinbase", "product": "CBETH-ETH"}])
        cb.books["CBETH-ETH"] = Book("coinbase", [(1.1439, 2.0)], [(1.1441, 3.0)])
        rec.record(1_000_000.0, REF)
        (row,) = cex_rows(rec)
        self.assertEqual((row[3], row[5]), ("cbETH/WETH", None))   # fake chain skips cbETH/WETH

    def test_venue_without_fetcher_is_ignored(self):
        rec, _, cb, lines = make(cex=[{"pair": "USDT/USDC", "venue": "kraken", "product": "USDTUSD"}])
        rec.record(1_000_000.0, REF)
        self.assertEqual((cex_rows(rec), cb.asked), ([], []))
        self.assertEqual(rec.cex_failures, 0)                     # quietly ignored, not an error
        self.assertFalse(any("skipped" in line for line in lines if "USDTUSD" in line))

    def test_off_by_default(self):
        rec, _, cb, _ = make(cex=[])
        rec.record(1_000_000.0, REF)
        self.assertEqual((cex_rows(rec), cb.asked), ([], []))


class TestParallel(unittest.TestCase):
    def test_runs_in_the_executor_and_times_out(self):
        with ThreadPoolExecutor(max_workers=2) as ex:
            rec, _, cb, lines = make(executor=ex, timeout=0.05)
            cb.delay = 0.3
            start = time.perf_counter()
            quotes = rec.record(1_000_000.0, REF)
            took = time.perf_counter() - start
        self.assertTrue(quotes)                       # Base rows still stored
        self.assertEqual(cex_rows(rec), [])
        self.assertLess(took, 0.25)                   # didn't wait the full 0.3 s
        self.assertIn("USDT-USDC skipped (TimeoutError", lines[-1])

    def test_executor_result_matches_direct(self):
        with ThreadPoolExecutor(max_workers=2) as ex:
            rec, _, _, _ = make(executor=ex)
            rec.record(1_000_000.0, REF)
        self.assertEqual(cex_rows(rec)[0][9:11], (0.99995, 1.00005))


class TestScannerWiring(unittest.TestCase):
    def test_scanner_passes_its_fetchers_and_pool(self):
        from tests.test_scanner import make_scanner
        s, _, _, fetchers = make_scanner()
        self.assertIs(s.recorder.executor, s.pool)
        self.assertEqual(set(s.recorder.cex_fetchers), {"kraken", "coinbase"})
        s.shutdown()

    def test_print_table(self):
        rows = [recorder.CexQuote(1.0, "coinbase", "USDT/USDC", "USDT-USDC", 1.00095, 1.00105,
                                  1.0, 1.0, 0.1, 100, 0.100)]
        buf = io.StringIO()
        with redirect_stdout(buf):
            recorder.print_cex_table(rows)
        line = buf.getvalue().splitlines()[1]
        self.assertEqual(line.split()[-2:], ["1.0", "+10.0"])        # 1 bp round trip, +10 bp vs Base
        self.assertIn("coinbase USDT-USDC", line)


class TestConfig(unittest.TestCase):
    def test_real_config_pairs_exist(self):
        import config
        names = {p["name"] for p in config.RECORD_PAIRS}
        self.assertEqual([c["product"] for c in config.RECORD_CEX], ["CBETH-ETH", "USDT-USDC"])
        for c in config.RECORD_CEX:
            self.assertIn(c["pair"], names)
        self.assertIn("cbETH/wstETH", names)


class TestOldDatabase(unittest.TestCase):
    def test_new_table_appears_on_connect(self):
        conn = db.connect(":memory:")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(cex_quotes)")]
        self.assertEqual(cols[:7], ["ts", "strategy", "venue", "path", "size_usd", "raw_edge", "net_edge"])


if __name__ == "__main__":
    unittest.main()
