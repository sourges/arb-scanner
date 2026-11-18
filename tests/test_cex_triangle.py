"""Binance.US triangle (cex_triangle.py) with fake order books. Hand-worked numbers:

Taker fee 0.02% on every leg unless a test says otherwise. Size $100.

A  No spread, deep books: BTCUSD 60000, ETHBTC 1/30, ETHUSD 2000.
   Both loops: raw 0.000%, net = 0.9998^3 - 1 = -0.059988%.

B  Real spreads: BTCUSD 59990/60010, ETHBTC 0.03331/0.03335, ETHUSD 1999/2001 (bid/ask).
   forward  USD>BTC>ETH>USD: 100 / 60010 / 0.03335 * 1999 = 99.883403 -> raw -0.116597%
            net = 99.883403 * 0.9998^3 = -0.176515%
   reverse  USD>ETH>BTC>USD: 100 / 2001 * 0.03331 * 59990 = 99.863413 -> raw -0.136587%
            net -0.196493%

C  ETH cheap in BTC: ETHBTC 0.0329/0.0330, BTCUSD and ETHUSD flat (60000, 2000).
   forward: 100 / 60000 / 0.0330 * 2000 = 101.010101 -> raw +1.010101%, net +0.949507%
   reverse: 100 / 2000 * 0.0329 * 60000 = 98.700    -> raw -1.300%

D  Thin ETHUSD bids: 0.02 ETH at 2000, then 10 at 1990. Book A otherwise.
   forward buys exactly 0.05 ETH (raw path), sells 0.02*2000 + 0.03*1990 = $99.70 -> raw -0.300%

E  ETHBTC taker fee 0.1%, the rest 0.02%, book A: net = 0.9998 * 0.999 * 0.9998 - 1 = -0.139956%
"""
import io
import unittest
from contextlib import redirect_stdout

import cex_triangle as ct
import db
import report
from cex import Book
from scanner import Scanner, status_line
from tests.fakes import FakeChain, FakeFetchers, make_config

FEES = {"default": 0.0002}
TRI_USD = [{"quote": "USD", "btc": "BTCUSD", "eth": "ETHUSD"}]
ALL3 = [{"quote": "USD", "btc": "BTCUSD", "eth": "ETHUSD"},
        {"quote": "USDT", "btc": "BTCUSDT", "eth": "ETHUSDT"},
        {"quote": "USDC", "btc": "BTCUSDC", "eth": "ETHUSDC"}]
D = 1e-6


def book(bid, ask, size=1000.0, now=0.0, bids=None):
    return Book("binanceus", bids or [(bid, size)], [(ask, size)], fetched_at=now)


def books_a(now=0.0):
    return {"BTCUSD": book(60000, 60000, now=now), "ETHBTC": book(1 / 30, 1 / 30, now=now),
            "ETHUSD": book(2000, 2000, now=now)}


def by_path(results):
    return {r.path: r for r in results}


class TestMath(unittest.TestCase):
    def test_a_no_spread(self):
        res = by_path(ct.compute(100, TRI_USD, "ETHBTC", books_a(), FEES))
        self.assertEqual(set(res), {"USD>BTC>ETH>USD", "USD>ETH>BTC>USD"})
        for r in res.values():
            self.assertAlmostEqual(r.raw_edge, 0.0, delta=D)
            self.assertAlmostEqual(r.net_edge, -0.059988, delta=D)
            self.assertAlmostEqual(r.fee_usd, 100 - r.back_net, delta=1e-9)

    def test_b_spreads(self):
        books = {"BTCUSD": book(59990, 60010), "ETHBTC": book(0.03331, 0.03335), "ETHUSD": book(1999, 2001)}
        res = by_path(ct.compute(100, TRI_USD, "ETHBTC", books, FEES))
        fwd, rev = res["USD>BTC>ETH>USD"], res["USD>ETH>BTC>USD"]
        self.assertAlmostEqual(fwd.back_raw, 99.883403, delta=1e-5)
        self.assertAlmostEqual(fwd.raw_edge, -0.116597, delta=D)
        self.assertAlmostEqual(fwd.net_edge, -0.176515, delta=D)
        self.assertAlmostEqual(rev.raw_edge, -0.136587, delta=D)
        self.assertAlmostEqual(rev.net_edge, -0.196493, delta=D)
        self.assertEqual(fwd.legs, ("BTCUSD", "ETHBTC", "ETHUSD"))
        self.assertEqual(rev.legs, ("ETHUSD", "ETHBTC", "BTCUSD"))

    def test_c_mispriced_cross(self):
        books = {"BTCUSD": book(60000, 60000), "ETHBTC": book(0.0329, 0.0330), "ETHUSD": book(2000, 2000)}
        res = by_path(ct.compute(100, TRI_USD, "ETHBTC", books, FEES))
        self.assertAlmostEqual(res["USD>BTC>ETH>USD"].raw_edge, 1.010101, delta=D)
        self.assertAlmostEqual(res["USD>BTC>ETH>USD"].net_edge, 0.949507, delta=D)
        self.assertAlmostEqual(res["USD>ETH>BTC>USD"].raw_edge, -1.3, delta=D)

    def test_d_walks_thin_book(self):
        books = books_a()
        books["ETHUSD"] = book(0, 2000, bids=[(2000, 0.02), (1990, 10.0)])
        fwd = by_path(ct.compute(100, TRI_USD, "ETHBTC", books, FEES))["USD>BTC>ETH>USD"]
        self.assertAlmostEqual(fwd.back_raw, 99.70, delta=1e-9)
        self.assertAlmostEqual(fwd.raw_edge, -0.3, delta=D)

    def test_too_thin_drops_that_loop_only(self):
        books = books_a()
        books["ETHUSD"] = Book("binanceus", [(2000, 0.01)], [(2000, 1000.0)], fetched_at=0)
        res = by_path(ct.compute(100, TRI_USD, "ETHBTC", books, FEES))
        self.assertEqual(set(res), {"USD>ETH>BTC>USD"})                 # forward can't sell 0.05 ETH

    def test_e_fee_per_symbol(self):
        fees = {"default": 0.0002, "ETHBTC": 0.001}
        for r in ct.compute(100, TRI_USD, "ETHBTC", books_a(), fees):
            self.assertAlmostEqual(r.net_edge, -0.139956, delta=D)

    def test_missing_book_skips_its_triangle(self):
        books = books_a()
        books.update({"BTCUSDT": book(60000, 60000), "ETHUSDT": book(2000, 2000)})
        res = ct.compute(100, ALL3, "ETHBTC", books, FEES)     # no USDC books
        self.assertEqual({r.path for r in res},
                         {"USD>BTC>ETH>USD", "USD>ETH>BTC>USD", "USDT>BTC>ETH>USDT", "USDT>ETH>BTC>USDT"})

    def test_book_age_is_the_gap_between_fetches(self):
        """Books fetched at 100.0, 100.0 and 98.5 -> 1.5s apart, whichever leg is the odd one.
        It never goes negative, however the books are timed."""
        for oldest in ("BTCUSD", "ETHBTC", "ETHUSD"):
            books = books_a(now=100.0)
            books[oldest].fetched_at = 98.5
            for r in ct.compute(100, TRI_USD, "ETHBTC", books, FEES):
                self.assertAlmostEqual(r.book_age, 1.5, msg=oldest)
        books = books_a(now=100.0)
        books["ETHUSD"].fetched_at = 100.3                              # fetched after the others
        for r in ct.compute(100, TRI_USD, "ETHBTC", books, FEES):
            self.assertAlmostEqual(r.book_age, 0.3)

    def test_collector_rows_have_a_non_negative_gap(self):
        """The bug seen live: books fetched AFTER the scan started gave age -0.2s."""
        clock = Clock(1000.0)
        src = BookSource(clock)
        c, _ = collector(clock, src)
        clock.t = 1000.2                                                # fetches happen 0.2s later
        res = c.collect(1000.0)
        self.assertTrue(res)
        for r in res:
            self.assertAlmostEqual(r.book_age, 0.0)


class FakeResponse:
    def __init__(self, status, payload=None, headers=None, text=""):
        self.status_code, self._payload, self.headers, self.text = status, payload, headers or {}, text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return self.resp


class TestFetch(unittest.TestCase):
    def test_parses_and_reads_weight(self):
        s = FakeSession(FakeResponse(200, {"lastUpdateId": 1, "bids": [["1999.5", "0.4"], ["2000.0", "1"]],
                                            "asks": [["2001", "2"]]}, {"X-MBX-USED-WEIGHT-1M": "35"}))
        b, used = ct.fetch_depth("ETHUSD", 20, s)
        self.assertEqual(b.bids, [(2000.0, 1.0), (1999.5, 0.4)])
        self.assertEqual(b.best_ask, 2001.0)
        self.assertEqual(used, 35)
        self.assertEqual(s.calls[0], (ct.DEPTH_URL, {"symbol": "ETHUSD", "limit": 20}))

    def test_rate_limit_and_ban(self):
        for status in (429, 418):
            with self.assertRaises(ct.RateLimited) as cm:
                ct.fetch_depth("ETHUSD", 20, FakeSession(FakeResponse(status, headers={"Retry-After": "90"})))
            self.assertEqual((cm.exception.status, cm.exception.retry_after), (status, 90))

    def test_bad_symbol(self):
        with self.assertRaises(ValueError):
            ct.fetch_depth("NOPE", 20, FakeSession(FakeResponse(400, text='{"code":-1121,"msg":"Invalid symbol."}')))

    def test_unexpected_payload(self):
        with self.assertRaises(ValueError):
            ct.parse_depth({"code": -1003, "msg": "Too many requests"})


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class BookSource:
    """fetch(symbol) for the collector. books: {symbol: (bid, ask)}; errors: {symbol: exception}."""

    def __init__(self, clock, books=None, used=None):
        self.clock = clock
        self.books = books or {"BTCUSD": (60000, 60000), "ETHBTC": (1 / 30, 1 / 30), "ETHUSD": (2000, 2000),
                               "BTCUSDT": (60000, 60000), "ETHUSDT": (2000, 2000),
                               "BTCUSDC": (60000, 60000), "ETHUSDC": (2000, 2000)}
        self.errors = {}
        self.used = used
        self.calls = 0

    def __call__(self, symbol):
        self.calls += 1
        if symbol in self.errors:
            raise self.errors[symbol]
        bid, ask = self.books[symbol]
        return book(bid, ask, now=self.clock()), self.used


def collector(clock, src, **over):
    cfg = make_config(**{"BUS_SECONDS": 4, "BUS_TRIANGLES": ALL3, "BUS_TAKER_FEES": FEES, **over})
    logs = []
    c = ct.CexTriCollector(cfg, db.connect(":memory:"), logs.append, fetch=src, clock=clock)
    return c, logs


def count(c):
    return c.conn.execute("SELECT COUNT(*) FROM cex_tri_quotes").fetchone()[0]


class TestCollector(unittest.TestCase):
    def test_round_writes_six_loops_in_shared_format(self):
        clock = Clock()
        src = BookSource(clock)
        c, logs = collector(clock, src)
        c.startup()
        self.assertIn("binance.us triangle books", logs[-1])
        self.assertEqual(len(c.symbols()), 7)
        res = c.collect(clock())
        self.assertEqual(len(res), 6)
        rows = c.conn.execute("SELECT ts, strategy, venue, path, size_usd, raw_edge, net_edge, leg2"
                              " FROM cex_tri_quotes").fetchall()
        self.assertEqual(len(rows), 6)
        for ts, strategy, venue, _path, size, raw, net, leg2 in rows:
            self.assertEqual((ts, strategy, venue, size, leg2), (1000.0, "tri_arb", "binanceus", 100.0, "ETHBTC"))
            self.assertAlmostEqual(raw, 0.0, delta=D)
            self.assertAlmostEqual(net, -0.059988, delta=D)

    def test_startup_drops_a_quote_with_a_bad_symbol(self):
        clock = Clock()
        src = BookSource(clock)
        src.errors["BTCUSDC"] = ValueError("Invalid symbol")
        c, logs = collector(clock, src)
        c.startup()
        self.assertEqual([t["quote"] for t in c.triangles], ["USD", "USDT"])
        self.assertTrue(any("skipping USDC loops" in m for m in logs))
        self.assertEqual(len(c.collect(clock())), 4)

    def test_startup_without_the_cross_book_turns_it_off(self):
        clock = Clock()
        src = BookSource(clock)
        src.errors["ETHBTC"] = ValueError("Invalid symbol")
        c, logs = collector(clock, src)
        c.startup()
        self.assertFalse(c.enabled)
        self.assertFalse(c.due(clock()))
        self.assertIn("triangle off", logs[-1])

    def test_cadence(self):
        clock = Clock()
        c, _ = collector(clock, BookSource(clock))
        self.assertTrue(c.due(1000.0))
        c.collect(1000.0)
        self.assertFalse(c.due(1002.0))
        self.assertTrue(c.due(1004.0))

    def test_rate_limited_pauses_for_retry_after_or_backoff(self):
        for retry_after, pause in ((30, 60), (120, 120)):
            clock = Clock()
            src = BookSource(clock)
            src.errors["ETHUSD"] = ct.RateLimited(429, retry_after)
            c, logs = collector(clock, src)
            self.assertEqual(c.collect(1000.0), [])
            self.assertEqual(count(c), 0)
            self.assertIn("rate limited", logs[-1])
            self.assertFalse(c.due(1000.0 + pause - 1))
            self.assertTrue(c.due(1000.0 + pause))

    def test_high_weight_skips_a_round(self):
        clock = Clock()
        c, logs = collector(clock, BookSource(clock, used=900))
        self.assertEqual(len(c.collect(1000.0)), 6)                     # this round still counts
        self.assertIn("used weight 900", logs[-1])
        self.assertFalse(c.due(1004.0))
        self.assertTrue(c.due(1008.0))

    def test_normal_weight_does_not_skip(self):
        clock = Clock()
        c, _ = collector(clock, BookSource(clock, used=300))
        c.collect(1000.0)
        self.assertTrue(c.due(1004.0))

    def test_one_failed_book_skips_the_round(self):
        clock = Clock()
        src = BookSource(clock)
        src.errors["ETHUSDT"] = ConnectionError("reset")
        c, logs = collector(clock, src)
        self.assertEqual(c.collect(1000.0), [])
        self.assertEqual((count(c), c.failures), (0, 1))
        self.assertIn("round skipped", logs[-1])

    def test_stale_book_is_not_used(self):
        clock = Clock()
        src = BookSource(clock)
        c, _ = collector(clock, src)
        real = src.__call__

        def old_eth(symbol):
            b, used = real(symbol)
            if symbol == "ETHUSD":
                b.fetched_at = clock() - 10                             # older than BOOK_MAX_AGE_S
            return b, used
        c.fetch = old_eth
        self.assertEqual({r.path[:4] for r in c.collect(clock())}, {"USDT", "USDC"})

    def test_off_when_seconds_is_none(self):
        clock = Clock()
        src = BookSource(clock)
        c, _ = collector(clock, src, BUS_SECONDS=None)
        c.startup()
        self.assertFalse(c.due(clock()))
        self.assertEqual(src.calls, 0)

    def test_best_line_and_table(self):
        clock = Clock()
        c, _ = collector(clock, BookSource(clock))
        res = c.collect(clock())
        line = ct.best_line(res)
        self.assertTrue(line.startswith("BUS best "))
        self.assertTrue(line.endswith(" -0.060%"), line)                # net (after fees), not raw 0.000%
        self.assertEqual(ct.best_line([]), "BUS -")
        out = io.StringIO()
        with redirect_stdout(out):
            ct.print_table(res)
            ct.print_table([])
        self.assertIn("USDT>BTC>ETH>USDT", out.getvalue())
        self.assertIn("no Binance.US triangle", out.getvalue())


class TestInScanner(unittest.TestCase):
    def make(self, src_errors=None):
        cfg = make_config(BUS_SECONDS=4, BUS_TRIANGLES=ALL3, BUS_TAKER_FEES=FEES)
        chain = FakeChain()
        clock = Clock(1_000_000.0)
        mids = {("kraken", "ETHUSD"): 2000.0, ("kraken", "XBTUSD"): 60000.0,
                ("coinbase", "ETH-USD"): 2000.0, ("coinbase", "BTC-USD"): 60000.0}
        src = BookSource(clock)
        s = Scanner(cfg, chain, db.connect(":memory:"), fetchers=FakeFetchers(mids, clock).as_dict(),
                    clock=clock, bus_fetch=src)
        with redirect_stdout(io.StringIO()):
            s.startup()
        if src_errors:
            src.errors.update(src_errors)
        return s, chain, clock

    def step(self, s, chain, clock, dt=2):
        chain.block, clock.t = chain.block + 1, clock.t + dt
        out = io.StringIO()
        with redirect_stdout(out):
            res = s.step()
        return res, out.getvalue()

    def test_runs_on_its_own_cadence(self):
        s, chain, clock = self.make()
        for _ in range(4):                                              # 4 scans, 2s apart = 8s
            self.step(s, chain, clock)
        n = s.conn.execute("SELECT COUNT(*) FROM cex_tri_quotes").fetchone()[0]
        self.assertEqual(n, 12)                                         # rounds at +2s and +6s
        (block, results), _ = self.step(s, chain, clock)
        self.assertIn("BUS best", status_line(block, results, bus=s.bus.last))
        s.shutdown()

    def test_binance_failure_never_stops_the_base_scan(self):
        s, chain, clock = self.make({"ETHBTC": ConnectionError("down")})
        res, log = self.step(s, chain, clock)
        self.assertIsNotNone(res)
        self.assertIn("binance.us: round skipped", log)
        self.assertGreater(s.conn.execute("SELECT COUNT(*) FROM minute_stats").fetchone()[0]
                           + len(s.minutes.flush()), 0)
        s.shutdown()


class TestReport(unittest.TestCase):
    """Rows: USD>BTC>ETH>USD nets -0.06, -0.02, +0.01 (raws 0.0, 0.04, 0.07; gaps 1, 2, 3)
    -> 3 rounds, 33.33% > 0, median net -0.020, best +0.010, median raw +0.040, gap 2.00."""

    def test_cex_triangle_section(self):
        conn = db.connect(":memory:")
        for i, (net, raw, age) in enumerate(((-0.06, 0.0, 1), (-0.02, 0.04, 2), (0.01, 0.07, 3))):
            conn.execute("INSERT INTO cex_tri_quotes (ts, venue, path, net_edge, raw_edge, book_age)"
                         " VALUES (?,?,?,?,?,?)", (1000 + i, "binanceus", "USD>BTC>ETH>USD", net, raw, age))
        out = io.StringIO()
        with redirect_stdout(out):
            report.print_report(conn, 0, 0.05, 15)
        line = next(l for l in out.getvalue().splitlines() if l.startswith("binanceus"))
        self.assertEqual(line.split(), ["binanceus", "USD>BTC>ETH>USD", "3", "33.33%", "-0.020%",
                                        "+0.010%", "+0.040%", "2.00"])

    def test_old_negative_ages_are_ignored_in_the_gap(self):
        """Two rows from before the fix (-0.2, -0.3) and two after (0.1, 0.3) -> gap median 0.20."""
        conn = db.connect(":memory:")
        for i, age in enumerate((-0.2, -0.3, 0.1, 0.3)):
            conn.execute("INSERT INTO cex_tri_quotes (ts, venue, path, net_edge, raw_edge, book_age)"
                         " VALUES (?,?,?,?,?,?)", (1000 + i, "binanceus", "USD>BTC>ETH>USD", -0.1, -0.04, age))
        (row,) = report.cex_tri_summary(conn, 0)
        self.assertEqual(row[2], 4)                                     # every row still counts
        self.assertAlmostEqual(row[7], 0.2)

    def test_old_database(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        self.assertEqual(report.cex_tri_summary(conn, 0), [])


if __name__ == "__main__":
    unittest.main()
