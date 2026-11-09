import unittest

import cex

ASKS = [(100.0, 1.0), (101.0, 2.0)]   # 100 + 202 = $302 of depth
BIDS = [(99.0, 1.0), (98.0, 2.0)]     # 3 units of depth


class TestWalkBuy(unittest.TestCase):
    def test_inside_first_level(self):
        self.assertAlmostEqual(cex.walk_buy(ASKS, 50), 0.5)

    def test_exactly_first_level(self):
        self.assertAlmostEqual(cex.walk_buy(ASKS, 100), 1.0)

    def test_crosses_levels(self):
        # $100 buys 1.0 at 100, remaining $50 buys 50/101 at 101
        self.assertAlmostEqual(cex.walk_buy(ASKS, 150), 1 + 50 / 101)

    def test_book_too_thin(self):
        self.assertIsNone(cex.walk_buy(ASKS, 1000))


class TestWalkSell(unittest.TestCase):
    def test_inside_first_level(self):
        self.assertAlmostEqual(cex.walk_sell(BIDS, 0.5), 49.5)

    def test_crosses_levels(self):
        self.assertAlmostEqual(cex.walk_sell(BIDS, 2.0), 99 + 98)

    def test_book_too_thin(self):
        self.assertIsNone(cex.walk_sell(BIDS, 4.0))


class TestParsers(unittest.TestCase):
    def test_kraken_takes_only_result_key_and_sorts(self):
        payload = {"error": [], "result": {"XETHZUSD": {
            "bids": [["1999.0", "1.5", 1], ["2000.0", "2.0", 1]],
            "asks": [["2002.0", "1.0", 1], ["2001.0", "3.0", 1]],
        }}}
        b = cex.parse_kraken(payload)
        self.assertEqual(b.bids, [(2000.0, 2.0), (1999.0, 1.5)])
        self.assertEqual(b.asks, [(2001.0, 3.0), (2002.0, 1.0)])
        self.assertAlmostEqual(b.mid, 2000.5)

    def test_kraken_error_raises(self):
        with self.assertRaises(ValueError):
            cex.parse_kraken({"error": ["EGeneral:Too many requests"], "result": {}})

    def test_coinbase(self):
        payload = {"pricebook": {"product_id": "ETH-USD",
                                 "bids": [{"price": "1999.5", "size": "0.4"}],
                                 "asks": [{"price": "2000.5", "size": "0.7"}]}}
        b = cex.parse_coinbase(payload)
        self.assertEqual((b.best_bid, b.best_ask), (1999.5, 2000.5))
        self.assertEqual(b.venue, "coinbase")


class TestBookCache(unittest.TestCase):
    def test_expires_old_books(self):
        cache = cex.BookCache(max_age_s=5)
        b = cex.Book("kraken", [(1, 1)], [(2, 1)], fetched_at=1000.0)
        cache.put("kraken", "ETHUSD", b)
        self.assertIs(cache.get("kraken", "ETHUSD", now=1004.0), b)
        self.assertIsNone(cache.get("kraken", "ETHUSD", now=1006.0))
        self.assertIsNone(cache.get("coinbase", "ETH-USD", now=1000.0))


if __name__ == "__main__":
    unittest.main()
