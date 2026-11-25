"""Recorder vs the node's gas cap (Nov 25, 2026).

A real --once run failed with: out of gas: gas required exceeds: 100000000. One eth_call
carried every pair's quotes, and quotes into near-empty pools burn a lot of gas. The fake
chain mimics that with max_subcalls: an eth_call with more sub-calls than that fails whole.

Fake chain, depth quotes on ($100 and $1,000):
    per pair: 1 block-number call + 2 pools x 2 sizes x 2 directions = 9 sub-calls
    two pairs in one call (the old way): 1 + 16 = 17 sub-calls
With max_subcalls = 12: the old single call (17) would fail; one call per pair (9) passes.
With max_subcalls = 5: every pair's call fails -> the minute is skipped, like before.
"""
import unittest
from concurrent.futures import ThreadPoolExecutor

import db
import recorder
from tests.fakes import FakeChain, make_config

REF = {"ETH": 2000.0, "BTC": 60000.0}


def make(max_subcalls=None, executor=None):
    chain = FakeChain()
    lines = []
    rec = recorder.Recorder(make_config(RECORD_DEPTH_USD=1000.0), chain, db.connect(":memory:"),
                            lines.append, executor=executor)
    rec.startup()
    chain.max_subcalls = max_subcalls
    return rec, chain, lines


class TestGasCap(unittest.TestCase):
    def test_per_pair_calls_fit_under_the_cap(self):
        rec, chain, lines = make(max_subcalls=12)
        before = chain.calls
        quotes = rec.record(1_000_000.0, REF)
        self.assertEqual(chain.calls - before, 2)
        self.assertEqual({q.pair for q in quotes}, {"cbBTC/WBTC", "USDT/USDC"})
        self.assertEqual((rec.failures, rec.pair_failures), (0, 0))
        self.assertFalse(any("skipped" in line for line in lines))

    def test_all_pairs_over_the_cap_skip_the_minute(self):
        rec, _, lines = make(max_subcalls=5)
        self.assertEqual(rec.record(1_000_000.0, REF), [])
        self.assertIn("minute skipped (RpcError: eth_call: {'code': -32003, 'message': 'out of gas",
                      lines[-1])

    def test_one_heavy_pair_fails_alone(self):
        rec, chain, lines = make()
        real = chain.eth_call

        def picky(to, data, block="latest"):
            # fail only the call that asks about the USDT token (the USDT/USDC pair)
            if bytes.fromhex(rec.pairs[1][0]["a"]["address"][2:].lower()) in data:
                from rpc import RpcError
                raise RpcError("eth_call: out of gas")
            return real(to, data, block)
        chain.eth_call = picky
        quotes = rec.record(1_000_000.0, REF)
        self.assertEqual({q.pair for q in quotes}, {"cbBTC/WBTC"})
        self.assertIn("recorder: USDT/USDC skipped this minute (RpcError: eth_call: out of gas)", lines[-1])
        self.assertEqual(rec.pair_failures, 1)
        stored = {r[0] for r in rec.conn.execute("SELECT DISTINCT pair FROM pegged_quotes")}
        self.assertEqual(stored, {"cbBTC/WBTC"})

    def test_parallel_matches_sequential(self):
        rec_seq, _, _ = make()
        seq = rec_seq.record(1_000_000.0, REF)
        with ThreadPoolExecutor(max_workers=4) as ex:
            rec_par, _, _ = make(executor=ex)
            par = rec_par.record(1_000_000.0, REF)
        self.assertEqual([(q.pair, q.pool, q.bid, q.ask, q.depth_bid, q.depth_ask) for q in par],
                         [(q.pair, q.pool, q.bid, q.ask, q.depth_bid, q.depth_ask) for q in seq])

    def test_pair_calls_go_through_the_executor(self):
        class Counting(ThreadPoolExecutor):
            submitted = 0

            def submit(self, *a, **k):
                Counting.submitted += 1
                return super().submit(*a, **k)
        with Counting(max_workers=4) as ex:
            rec, _, _ = make(executor=ex)
            rec.record(1_000_000.0, REF)
        self.assertEqual(Counting.submitted, 2)          # one job per pair, run in parallel

    def test_block_is_the_lowest_pair_block(self):
        rec, chain, _ = make()
        blocks = iter([105, 103])
        real = chain.eth_call

        def bumping(to, data, block="latest"):
            chain.block = next(blocks)
            return real(to, data, block)
        chain.eth_call = bumping
        block, quotes, failed = recorder.quote_pegged(chain, rec.cfg, rec.pairs, REF, 100.0, 1000.0)
        self.assertEqual((block, failed), (103, []))


if __name__ == "__main__":
    unittest.main()
