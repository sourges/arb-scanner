"""markout.py: CEX<->DEX episodes re-priced with the CEX leg done later. Hand-worked.

Fees (config): kraken_reduced taker 0.38%, coinbase maker 0.35%.

A. ETH dex>kraken_reduced_taker, opens at ts 1000:
   bought on the pool at dex_buy 1990 (row 0); sold on Kraken at the BID (taker), fee 0.38%
     +0s  bid 2000: 2000 * 0.9962 / 1990 - 1 = +0.120603%
     +2s  bid 1996: 1988.4152 / 1990 - 1 = -0.079638%
     +4s  bid 1998: 1990.4076 / 1990 - 1 = +0.020482%
     +10s bid 2000: +0.120603%      +30s no row -> None
   who closed (+10s): DEX mid 1989.5 -> 1999.5, Kraken mid 2000.5 unchanged
     g0 = 1989.5/2000.5 - 1 = -0.550%, g10 = 1999.5/2000.5 - 1 = -0.050% (shrank > half)
     all of it from the pool moving -> "pool"
B. Same open, but Kraken moves to the pool instead (bid 1990, ask 1991 at +10s,
   DEX unchanged): g10 = 1989.5/1990.5 - 1 = -0.050% -> "cex"
C. BTC coinbase_maker>dex: bought on Coinbase at the BID (maker), sold on the pool at
   dex_sell 2000, fee 0.35%:  2000 / 1990 - 1 - 0.0035 = +0.152513%
"""
import unittest

import config
import db
import markout

FEES = config.CEX_FEES


def add_row(conn, pair, ts, kb=None, ka=None, cb=None, ca=None, dbuy=None, dsell=None):
    conn.execute("INSERT INTO scan_prices (ts, path, kraken_bid, kraken_ask, coinbase_bid, coinbase_ask,"
                 " dex_buy_px, dex_sell_px) VALUES (?,?,?,?,?,?,?,?)",
                 (ts, pair, kb, ka, cb, ca, dbuy, dsell))


def add_episode(conn, pair, route, start, usd=0.12, samples=2):
    conn.execute("INSERT INTO episodes (pair, route, start_ts, samples, max_net_usd) VALUES (?,?,?,?,?)",
                 (pair, route, start, samples, usd))


def case_a(conn, start=1000.0, cex_moves=False):
    add_row(conn, "ETH", start, kb=2000, ka=2001, dbuy=1990, dsell=1989)
    add_row(conn, "ETH", start + 2.1, kb=1996, ka=1997, dbuy=1990, dsell=1989)
    add_row(conn, "ETH", start + 3.9, kb=1998, ka=1999, dbuy=1990, dsell=1989)
    if cex_moves:
        add_row(conn, "ETH", start + 10, kb=1990, ka=1991, dbuy=1990, dsell=1989)
    else:
        add_row(conn, "ETH", start + 10, kb=2000, ka=2001, dbuy=2000, dsell=1999)


class TestParse(unittest.TestCase):
    def test_routes(self):
        self.assertEqual(markout.parse_route("dex>kraken_reduced_taker", FEES),
                         ("dex>cex", "kraken", "taker", 0.0038))
        self.assertEqual(markout.parse_route("coinbase_maker>dex", FEES),
                         ("cex>dex", "coinbase", "maker", 0.0035))
        self.assertIsNone(markout.parse_route("dex>dex", FEES))
        self.assertIsNone(markout.parse_route("dex>nowhere_taker", FEES))

    def test_which_side_of_the_book(self):
        row = {"kraken_bid": 1.0, "kraken_ask": 2.0}
        self.assertEqual(markout.cex_px(row, "kraken", "dex>cex", "taker"), 1.0)   # sell into the bid
        self.assertEqual(markout.cex_px(row, "kraken", "dex>cex", "maker"), 2.0)   # resting ask fills
        self.assertEqual(markout.cex_px(row, "kraken", "cex>dex", "taker"), 2.0)   # buy from the ask
        self.assertEqual(markout.cex_px(row, "kraken", "cex>dex", "maker"), 1.0)   # resting bid fills


class TestEpisode(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")

    def test_case_a_nets_and_pool_closed(self):
        case_a(self.conn)
        m = markout.episode_markout(self.conn, "ETH", "dex>kraken_reduced_taker", 1000.0, FEES)
        self.assertAlmostEqual(m["nets"][0], 0.120603, places=5)
        self.assertAlmostEqual(m["nets"][2], -0.079638, places=5)
        self.assertAlmostEqual(m["nets"][4], 0.020482, places=5)
        self.assertAlmostEqual(m["nets"][10], 0.120603, places=5)
        self.assertIsNone(m["nets"][30])
        self.assertEqual(m["closed"], "pool")

    def test_case_b_cex_closed(self):
        case_a(self.conn, cex_moves=True)
        m = markout.episode_markout(self.conn, "ETH", "dex>kraken_reduced_taker", 1000.0, FEES)
        self.assertEqual(m["closed"], "cex")

    def test_case_c_buy_on_cex_maker(self):
        add_row(self.conn, "BTC", 500.0, cb=1990, ca=1991, dbuy=2001, dsell=2000)
        m = markout.episode_markout(self.conn, "BTC", "coinbase_maker>dex", 500.0, FEES)
        self.assertAlmostEqual(m["nets"][0], 0.152513, places=5)

    def test_still_open(self):
        add_row(self.conn, "ETH", 0.0, kb=2000, ka=2001, dbuy=1990, dsell=1989)
        add_row(self.conn, "ETH", 10.0, kb=2000, ka=2001, dbuy=1991, dsell=1990)   # closed only 1/11
        m = markout.episode_markout(self.conn, "ETH", "dex>kraken_reduced_taker", 0.0, FEES)
        self.assertEqual(m["closed"], "open")

    def test_mixed(self):
        add_row(self.conn, "ETH", 0.0, kb=2000, ka=2001, dbuy=1990, dsell=1989)   # g0 -0.550%
        add_row(self.conn, "ETH", 10.0, kb=1995, ka=1996, dbuy=1995, dsell=1994)  # both moved 5
        m = markout.episode_markout(self.conn, "ETH", "dex>kraken_reduced_taker", 0.0, FEES)
        self.assertEqual(m["closed"], "mixed")

    def test_falls_back_to_the_last_horizon_with_a_row(self):
        add_row(self.conn, "ETH", 0.0, kb=2000, ka=2001, dbuy=1990, dsell=1989)
        add_row(self.conn, "ETH", 4.0, kb=2000, ka=2001, dbuy=2000, dsell=1999)   # no +10s row
        m = markout.episode_markout(self.conn, "ETH", "dex>kraken_reduced_taker", 0.0, FEES)
        self.assertEqual(m["closed"], "pool")

    def test_rows_outside_the_tolerance_are_ignored(self):
        add_row(self.conn, "ETH", 0.0, kb=2000, ka=2001, dbuy=1990, dsell=1989)
        add_row(self.conn, "ETH", 6.0, kb=1000, ka=1001, dbuy=1990, dsell=1989)   # 2s from +4, 4s from +10
        m = markout.episode_markout(self.conn, "ETH", "dex>kraken_reduced_taker", 0.0, FEES)
        self.assertIsNone(m["nets"][4])
        self.assertIsNone(m["nets"][10])

    def test_no_row_at_the_start_or_dex_dex(self):
        self.assertIsNone(markout.episode_markout(self.conn, "ETH", "dex>kraken_reduced_taker", 0.0, FEES))
        add_row(self.conn, "ETH", 0.0, kb=2000, ka=2001, dbuy=1990, dsell=1989)
        self.assertIsNone(markout.episode_markout(self.conn, "ETH", "dex>dex", 0.0, FEES))

    def test_missing_book_is_none(self):
        add_row(self.conn, "ETH", 0.0, kb=None, ka=None, dbuy=1990, dsell=1989)
        m = markout.episode_markout(self.conn, "ETH", "dex>kraken_reduced_taker", 0.0, FEES)
        self.assertIsNone(m["nets"][0])
        self.assertIsNone(m["closed"])


class TestSummary(unittest.TestCase):
    def test_grouped_medians_and_counts(self):
        conn = db.connect(":memory:")
        case_a(conn, 1000.0)                               # pool, +4s +0.0205%
        case_a(conn, 2000.0, cex_moves=True)               # cex,  +4s +0.0205%
        add_episode(conn, "ETH", "dex>kraken_reduced_taker", 1000.0)
        add_episode(conn, "ETH", "dex>kraken_reduced_taker", 2000.0)
        add_row(conn, "SOL", 3000.0, kb=150, ka=150.1, dbuy=149, dsell=148.9)
        add_episode(conn, "SOL", "dex>kraken_reduced_taker", 3000.0)
        add_episode(conn, "ETH", "dex>dex", 1000.0)                        # not CEX: skipped
        case_a(conn, 4000.0)
        add_episode(conn, "ETH", "dex>kraken_reduced_taker", 4000.0, usd=0.01)   # dust: skipped
        add_row(conn, "ETH", 5000.0, kb=2000, ka=2001, dbuy=1990, dsell=1989)
        add_row(conn, "ETH", 5004.0, kb=1980, ka=1981, dbuy=1990, dsell=1989)   # +4s: -1.12%
        add_episode(conn, "ETH", "dex>kraken_reduced_taker", 5000.0)
        rows = markout.summary(conn, 0, 0.05, FEES)
        self.assertEqual([(r[0], r[1], r[2]) for r in rows],
                         [("ETH", "dex>kraken_reduced_taker", 3), ("SOL", "dex>kraken_reduced_taker", 1)])
        eth = rows[0]
        self.assertAlmostEqual(eth[3][0], 0.120603, places=5)
        self.assertAlmostEqual(eth[4], 200 / 3)                            # 2 of 3 > 0 at +4s
        # 3rd (ts 5000): at +4s the gap flipped from -0.55% to +0.45%, still over half: "open"
        self.assertEqual(eth[5], {"pool": 1, "cex": 1, "mixed": 0, "open": 1})

    def test_print_section(self):
        import io
        from contextlib import redirect_stdout
        conn = db.connect(":memory:")
        case_a(conn)
        add_episode(conn, "ETH", "dex>kraken_reduced_taker", 1000.0)
        buf = io.StringIO()
        with redirect_stdout(buf):
            markout.print_section(conn, 0, 0.05, FEES)
        out = buf.getvalue()
        self.assertIn("-- majors --", out)
        self.assertIn("+0.121", out)
        self.assertIn("1/0/0/0", out)


if __name__ == "__main__":
    unittest.main()
