"""dex.py against the fake chain. Hand-worked numbers:

ETH reference $2000, trade $100 -> x_base = 0.05 WETH.
Fake pools price ETH at $2000 with a 0.05% fee:
    buy:  $100 -> 100/2000 * 0.9995 = 0.049975 WETH
    sell: 0.05 WETH -> 0.05*2000*0.9995 = $99.95
Gas per leg: (100k quote + 50k overhead) * (0.01 + 0.001 gwei) = 1.65e12 wei
    = $0.0033 at $2000, plus L1 fee 1e12 wei = $0.0020 -> $0.0053
Amounts come back as whole token units (6 decimals for USDC, 8 for cbBTC),
so comparisons use a small delta instead of exact equality.
"""
import unittest

import dex
from tests.fakes import CBBTC, USDC, WETH, FakeChain, make_config

PRICES = {"ETH": 2000.0, "BTC": 60000.0}
AERO_QUOTER = "0x" + "b2" * 20


def setup():
    cfg = make_config()
    chain = FakeChain()
    pair_pools = {}
    for pair in cfg.PAIRS:
        pools = dex.discover_pools(chain, cfg.MULTICALL3, cfg.DEXES, pair["base"]["address"], USDC)
        pair_pools[pair["name"]] = (pair, pools)
    return cfg, chain, pair_pools


class TestDex(unittest.TestCase):
    def test_discover_pools_skips_missing_tiers(self):
        _, _, pair_pools = setup()
        labels = [p.label for p in pair_pools["ETH"][1]]
        self.assertEqual(labels, ["uni-500", "aero-ts100"])   # tiers 100 and 50 have no pool

    def test_verify_tokens_ok_and_mismatch(self):
        cfg, chain, _ = setup()
        tokens = [cfg.USDC] + [p["base"] for p in cfg.PAIRS]
        self.assertEqual(dex.verify_tokens(chain, cfg.MULTICALL3, tokens), [])
        chain.tokens[WETH] = ("WETH", 6)                       # wrong decimals
        problems = dex.verify_tokens(chain, cfg.MULTICALL3, tokens)
        self.assertEqual(len(problems), 1)
        self.assertIn("WETH/18", problems[0])
        del chain.tokens[CBBTC]                                 # address with no contract
        problems = dex.verify_tokens(chain, cfg.MULTICALL3, tokens)
        self.assertTrue(any("no response" in p for p in problems))

    def test_quote_round_one_call_and_hand_worked_amounts(self):
        cfg, chain, pair_pools = setup()
        rd = dex.quote_round(chain, cfg, pair_pools, PRICES, 100.0)
        self.assertEqual(chain.calls, 3)                        # 2 discovery calls + ONE quote round
        self.assertEqual((rd.block, rd.basefee_wei, rd.l1_fee_wei), (100, 10_000_000, 10 ** 12))
        q = dex.to_dex_quotes(cfg, rd, pair_pools["ETH"][0], eth_usd=2000.0)[0]
        self.assertAlmostEqual(q.x_base, 0.05)
        self.assertAlmostEqual(q.buy_base_out, 0.049975)
        self.assertAlmostEqual(q.sell_usd_out, 99.95, delta=1e-5)
        self.assertAlmostEqual(q.buy_gas_usd, 0.0053)

    def test_btc_uses_8_decimals(self):
        cfg, chain, pair_pools = setup()
        rd = dex.quote_round(chain, cfg, pair_pools, PRICES, 100.0)
        q = dex.to_dex_quotes(cfg, rd, pair_pools["BTC"][0], eth_usd=2000.0)[0]
        self.assertAlmostEqual(q.x_base, 100 / 60000, delta=1e-8)          # whole satoshis
        self.assertAlmostEqual(q.buy_base_out, 100 / 60000 * 0.9995, delta=1e-8)

    def test_failed_quote_leaves_none_and_round_survives(self):
        cfg, chain, pair_pools = setup()
        del chain.prices[(AERO_QUOTER, 100, WETH)]             # aero ETH quoter reverts
        rd = dex.quote_round(chain, cfg, pair_pools, PRICES, 100.0)
        uni, aero = dex.to_dex_quotes(cfg, rd, pair_pools["ETH"][0], eth_usd=2000.0)
        self.assertIsNotNone(uni.buy_base_out)
        self.assertIsNone(aero.buy_base_out)
        self.assertIsNone(aero.sell_usd_out)

    def test_l1_oracle_failure_uses_fallback(self):
        cfg, chain, pair_pools = setup()
        chain.l1_fails = True
        rd = dex.quote_round(chain, cfg, pair_pools, PRICES, 100.0)
        self.assertIsNone(rd.l1_fee_wei)
        q = dex.to_dex_quotes(cfg, rd, pair_pools["ETH"][0], eth_usd=2000.0)[0]
        self.assertAlmostEqual(q.buy_gas_usd, 0.0033 + cfg.FALLBACK_L1_FEE_USD)

    def test_leg_gas_usd_hand_worked(self):
        usd = dex.leg_gas_usd(100_000, 10_000_000, 1_000_000, 50_000, 10 ** 12, 0.002, 2000.0)
        self.assertAlmostEqual(usd, 0.0053)


if __name__ == "__main__":
    unittest.main()
