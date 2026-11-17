"""prices.py. Hand-worked numbers, $100 trade:

Pool A: buy gives 0.0500 ETH -> buy px 100/0.0500 = 2000.00;  sell of 0.05 ETH gives $99.50 -> 1990.00
Pool B: buy gives 0.0498 ETH -> buy px 100/0.0498 = 2008.03;  sell of 0.05 ETH gives $100.20 -> 2004.00
Best buy  = A at 2000.00 (lowest).  Best sell = B at 2004.00 (highest).
DEX mid = 2002.00.  Kraken 1999/2001 -> mid 2000.00.  raw_edge = 2002/2000 - 1 = +0.1000 %
"""
import unittest

import db
import prices
from cex import Book
from dex import DexQuote


def quotes():
    return [DexQuote("A", 0.05, 0.0500, 99.50, 0.01, 0.01),
            DexQuote("B", 0.05, 0.0498, 100.20, 0.01, 0.01)]


def books(now=1000.0):
    return {"kraken": Book("kraken", [(1999.0, 5.0)], [(2001.0, 5.0)], fetched_at=now - 1.5),
            "coinbase": Book("coinbase", [(1998.0, 5.0)], [(2003.0, 5.0)], fetched_at=now - 0.25)}


class TestPrices(unittest.TestCase):
    def test_best_buy_is_lowest_and_best_sell_is_highest(self):
        buy_px, buy_pool, sell_px, sell_pool = prices.best_dex_prices(quotes(), 100.0)
        self.assertAlmostEqual(buy_px, 2000.0)
        self.assertEqual(buy_pool, "A")
        self.assertAlmostEqual(sell_px, 2004.0)
        self.assertEqual(sell_pool, "B")

    def test_row_has_books_ages_and_edge(self):
        r = prices.build("ETH", 100.0, quotes(), books(), 1000.0)
        self.assertEqual((r.kraken_bid, r.kraken_ask, r.coinbase_bid, r.coinbase_ask),
                         (1999.0, 2001.0, 1998.0, 2003.0))
        self.assertAlmostEqual(r.kraken_age, 1.5)
        self.assertAlmostEqual(r.coinbase_age, 0.25)
        self.assertAlmostEqual(r.dex_mid, 2002.0)
        self.assertAlmostEqual(r.cex_mid, 2000.0)          # Kraken, not Coinbase (2000.5)
        self.assertAlmostEqual(r.raw_edge, 0.1)
        self.assertFalse(r.empty)

    def test_no_kraken_book_uses_coinbase_mid(self):
        b = books()
        b["kraken"] = None
        r = prices.build("ETH", 100.0, quotes(), b, 1000.0)
        self.assertIsNone(r.kraken_bid)
        self.assertIsNone(r.kraken_age)
        self.assertAlmostEqual(r.cex_mid, 2000.5)
        self.assertAlmostEqual(r.raw_edge, (2002.0 / 2000.5 - 1) * 100)

    def test_failed_quotes_are_skipped_and_one_sided_has_no_edge(self):
        qs = [DexQuote("A", 0.05, None, None, 0.0, 0.0),        # pool quoted nothing
              DexQuote("B", 0.05, 0.0498, None, 0.01, 0.0)]     # buy only
        r = prices.build("ETH", 100.0, qs, books(), 1000.0)
        self.assertEqual(r.dex_buy_pool, "B")
        self.assertIsNone(r.dex_sell_px)
        self.assertIsNone(r.raw_edge)
        self.assertFalse(r.empty)                               # still worth a row: books + one side

    def test_nothing_at_all_is_empty(self):
        r = prices.build("ETH", 100.0, [], {"kraken": None, "coinbase": None}, 1000.0)
        self.assertTrue(r.empty)
        self.assertIsNone(r.raw_edge)

    def test_insert_uses_shared_columns_and_null_net_edge(self):
        conn = db.connect(":memory:")
        db.insert_prices(conn, 1000.0, 77, [prices.build("ETH", 100.0, quotes(), books(), 1000.0)])
        cols = [c[1] for c in conn.execute("PRAGMA table_info(scan_prices)")]
        self.assertEqual(cols[:7], ["ts", "strategy", "venue", "path", "size_usd", "raw_edge", "net_edge"])
        row = conn.execute("SELECT ts, strategy, venue, path, size_usd, raw_edge, net_edge, block,"
                           " kraken_age, dex_buy_px, dex_buy_pool, dex_sell_px, dex_sell_pool"
                           " FROM scan_prices").fetchone()
        self.assertEqual(row[:5], (1000.0, "px", "base", "ETH", 100.0))
        self.assertAlmostEqual(row[5], 0.1)
        self.assertIsNone(row[6])
        self.assertEqual(row[7], 77)
        self.assertAlmostEqual(row[8], 1.5)
        self.assertAlmostEqual(row[9], 2000.0)
        self.assertEqual((row[10], row[12]), ("A", "B"))
        self.assertAlmostEqual(row[11], 2004.0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
