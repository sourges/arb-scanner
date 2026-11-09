"""Hand-worked route math. Numbers chosen so each result can be checked on paper.

Pool A is cheap to buy on, pool B pays well to sell on. Both quotes are for
x_base = 0.05 (i.e. $100 at a $2000 reference price).
    A: $100 USDC buys 0.0505;  0.05 sells for $99.00
    B: $100 USDC buys 0.0495;  0.05 sells for $100.50
CEX book: best bid 1990, best ask 2000, deep enough for $100.
Fees (kraken_reduced): maker 0.22%, taker 0.38%. Gas $0.01 per leg.
"""
import unittest

from cex import Book
from dex import DexQuote
from routes import best_dex_sell, compute_routes, dex_dex_route

FEES = {"kraken_reduced": {"book": "kraken", "maker": 0.0022, "taker": 0.0038}}


def quotes():
    return [
        DexQuote("A", 0.05, 0.0505, 99.00, 0.01, 0.01),
        DexQuote("B", 0.05, 0.0495, 100.50, 0.01, 0.01),
    ]


def book():
    return Book("kraken", [(1990.0, 10.0)], [(2000.0, 10.0)])


def routes(rebalance=0.0):
    return compute_routes("ETH", 100.0, quotes(), {"kraken": book(), "coinbase": None}, FEES, rebalance)


class TestRoutes(unittest.TestCase):
    def test_dex_dex_picks_buy_a_sell_b(self):
        # 0.0505 sold on B = 100.50 * 0.0505/0.05 = 101.505; minus $0.02 gas = +1.485
        r = dex_dex_route("ETH", 100.0, quotes())
        self.assertEqual((r.buy_venue, r.sell_venue), ("A", "B"))
        self.assertAlmostEqual(r.usd_back, 101.505)
        self.assertAlmostEqual(r.net_usd, 1.485)
        self.assertAlmostEqual(r.net_pct, 1.485)

    def test_cex_taker_buy_then_dex_sell(self):
        # lift ask 2000: $100 -> 0.05; sell on B -> 100.50; fee 100*0.0038 = 0.38; gas 0.01
        r = routes()["kraken_reduced_taker>dex"]
        self.assertEqual(r.sell_venue, "B")
        self.assertAlmostEqual(r.usd_back, 100.50)
        self.assertAlmostEqual(r.cex_fee_usd, 0.38)
        self.assertAlmostEqual(r.net_usd, 0.11)

    def test_cex_maker_buy_then_dex_sell(self):
        # bid fills at 1990: 100/1990 = 0.0502513 base; on B: 100.5 * 0.0502513/0.05 = 101.00503
        r = routes()["kraken_reduced_maker>dex"]
        back = 100.5 * (100 / 1990) / 0.05
        self.assertAlmostEqual(r.usd_back, back)
        self.assertAlmostEqual(r.net_usd, back - 100 - 0.22 - 0.01)

    def test_dex_buy_then_cex_taker_sell(self):
        # best buy is A: 0.0505 base; hit bid 1990 -> 100.495; fee 0.38188; gas 0.01
        r = routes()["dex>kraken_reduced_taker"]
        self.assertEqual(r.buy_venue, "A")
        self.assertAlmostEqual(r.usd_back, 100.495)
        self.assertAlmostEqual(r.cex_fee_usd, 100.495 * 0.0038)
        self.assertAlmostEqual(r.net_usd, 0.495 - 100.495 * 0.0038 - 0.01)

    def test_dex_buy_then_cex_maker_sell(self):
        # ask fills at 2000: 0.0505 * 2000 = 101.0; fee 0.2222; gas 0.01 -> 0.7678
        r = routes()["dex>kraken_reduced_maker"]
        self.assertAlmostEqual(r.net_usd, 0.7678)

    def test_rebalance_cost_hits_cex_routes_only(self):
        with_cost, base = routes(rebalance=0.5), routes()
        self.assertAlmostEqual(with_cost["dex>dex"].net_usd, base["dex>dex"].net_usd)
        self.assertAlmostEqual(with_cost["dex>kraken_reduced_maker"].net_usd,
                               base["dex>kraken_reduced_maker"].net_usd - 0.5)

    def test_route_keys_and_missing_book_skipped(self):
        fees = dict(FEES, coinbase={"book": "coinbase", "maker": 0.0035, "taker": 0.0075})
        rs = compute_routes("ETH", 100.0, quotes(), {"kraken": book(), "coinbase": None}, fees, 0.0)
        self.assertEqual(set(rs), {"dex>dex", "kraken_reduced_maker>dex", "kraken_reduced_taker>dex",
                                   "dex>kraken_reduced_maker", "dex>kraken_reduced_taker"})

    def test_failed_quotes_are_skipped(self):
        qs = [DexQuote("A", 0.05, None, None, 0.0, 0.0), DexQuote("B", 0.05, 0.0495, 100.5, 0.01, 0.01)]
        rs = compute_routes("ETH", 100.0, qs, {"kraken": book()}, FEES, 0.0)
        self.assertNotIn("dex>dex", rs)              # needs two working pools
        self.assertEqual(rs["dex>kraken_reduced_taker"].buy_venue, "B")

    def test_best_dex_sell_accounts_for_gas(self):
        # C pays $0.10 more but costs $0.50 more gas -> B wins
        qs = [DexQuote("B", 0.05, None, 100.50, 0.0, 0.01), DexQuote("C", 0.05, None, 100.60, 0.0, 0.51)]
        q, usd = best_dex_sell(qs, 0.05)
        self.assertEqual(q.label, "B")
        self.assertAlmostEqual(usd, 100.50)

    def test_thin_cex_book_drops_taker_route(self):
        thin = Book("kraken", [(1990.0, 0.001)], [(2000.0, 0.001)])
        rs = compute_routes("ETH", 100.0, quotes(), {"kraken": thin}, FEES, 0.0)
        self.assertNotIn("kraken_reduced_taker>dex", rs)
        self.assertNotIn("dex>kraken_reduced_taker", rs)
        self.assertIn("kraken_reduced_maker>dex", rs)


if __name__ == "__main__":
    unittest.main()
