"""Recorder vs the node's gas cap (Nov 25, 2026).

A real --once run failed with: out of gas: gas required exceeds: 100000000. One eth_call
carried every pair's quotes, and quotes into near-empty pools burn a lot of gas. The fake
chain mimics that with max_subcalls: an eth_call with more sub-calls than that fails whole.

Fake chain, depth quotes on ($100 and $1,000):
    per pair: 1 block-number call + 2 pools x 2 sizes x 2 directions = 9 sub-calls
    two pairs in one call (the old way): 1 + 16 = 17 sub-calls
With max_subcalls = 12: the old single call (17) would fail; one call per pair (9) passes.

Adaptive split (added Nov 25, 2026, after Ankr kept benching on out-of-gas):
    a group the node refuses as too heavy is split in half and retried in the SAME minute;
    the split is remembered, so later minutes send the smaller calls straight away.
    max_subcalls = 5: a one-pool call is 1 + 4 = 5 -> fits. cbBTC/WBTC (2 pools, 9) fails once,
        splits into 2 calls of 5. USDT/USDC: the fake only quotes uni-500 (aero reverts), but
        the recorder still sends both pools, so it splits the same way.
        minute 1: 2 failed + 4 split calls = 6 eth_calls; minute 2: 4 eth_calls, no errors.
    max_subcalls = 4: even one pool alone is too heavy -> every pool is parked for
        RECORD_RECHECK_MIN minutes, the minute is skipped, and nothing is sent until then.
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

    def test_too_heavy_groups_split_in_the_same_minute(self):
        rec, chain, lines = make(max_subcalls=5)
        before = chain.calls
        quotes = rec.record(1_000_000.0, REF)
        self.assertEqual(chain.calls - before, 6)        # 2 refused + 4 halves
        self.assertEqual({(q.pair, q.pool) for q in quotes},
                         {("cbBTC/WBTC", "uni-500"), ("cbBTC/WBTC", "aero-ts100"),
                          ("USDT/USDC", "uni-500")})
        self.assertEqual([q.pair for q in quotes], sorted([q.pair for q in quotes],
                         key=["cbBTC/WBTC", "USDT/USDC"].index))     # pair order kept
        self.assertEqual((rec.failures, rec.pair_failures), (0, 0))
        self.assertEqual([len(g) for g in rec.groups["cbBTC/WBTC"]], [1, 1])
        self.assertTrue(any("cbBTC/WBTC too heavy for one call: now 2 calls" in l for l in lines))

    def test_pair_order_kept_when_only_one_pair_splits(self):
        # only cbBTC/WBTC is too heavy: its halves come back AFTER USDT/USDC, then get sorted
        rec, chain, _ = make()
        real = chain.eth_call
        cbbtc = bytes.fromhex(rec.pairs[0][0]["a"]["address"][2:].lower())

        def heavy_btc(to, data, block="latest"):
            chain.max_subcalls = 5 if cbbtc in data else None
            return real(to, data, block)
        chain.eth_call = heavy_btc
        quotes = rec.record(1_000_000.0, REF)
        self.assertEqual([q.pair for q in quotes], ["cbBTC/WBTC", "cbBTC/WBTC", "USDT/USDC"])
        self.assertEqual([len(g) for g in rec.groups["USDT/USDC"]], [2])    # never split

    def test_split_is_remembered(self):
        rec, chain, lines = make(max_subcalls=5)
        rec.record(1_000_000.0, REF)
        n_lines, before = len(lines), chain.calls
        quotes = rec.record(1_000_060.0, REF)
        self.assertEqual(chain.calls - before, 4)        # straight to the small calls
        self.assertEqual(len(quotes), 3)
        self.assertEqual(lines[n_lines:], [])            # no errors, no new split lines

    def test_split_matches_unsplit_quotes(self):
        whole, _, _ = make()
        split, _, _ = make(max_subcalls=5)
        key = lambda qs: [(q.pair, q.pool, q.bid, q.ask, q.depth_bid, q.depth_ask) for q in qs]
        self.assertEqual(key(split.record(1_000_000.0, REF)), key(whole.record(1_000_000.0, REF)))

    def test_pool_too_heavy_alone_is_parked_then_rechecked(self):
        rec, chain, lines = make(max_subcalls=4)
        self.assertEqual(rec.record(1_000_020.0, REF), [])           # minute 16667
        self.assertIn("minute skipped (RpcError: eth_call: {'code': -32003, 'message': 'out of gas",
                      lines[-1])
        self.assertEqual(rec.failures, 1)
        self.assertEqual(len(rec.parked), 4)                        # 2 pairs x 2 pools
        self.assertTrue(any("cbBTC/WBTC uni-500 too heavy even alone: skipped for 60 min" in l
                            for l in lines))
        before = chain.calls
        self.assertEqual(rec.record(1_000_080.0, REF), [])           # next minute: nothing to send
        self.assertEqual(chain.calls, before)
        self.assertEqual(rec.failures, 1)                           # nothing tried, nothing failed
        chain.max_subcalls = None                                   # the pools got lighter
        self.assertEqual(rec.record(1_000_020.0 + 59 * 60, REF), []) # 59 min later: still parked
        quotes = rec.record(1_000_020.0 + 60 * 60, REF)             # 60 min: tried again
        self.assertEqual(len(quotes), 3)
        self.assertEqual(rec.parked, {})

    def test_recheck_minutes_come_from_config(self):
        rec, _, lines = make(max_subcalls=4)
        rec.cfg.RECORD_RECHECK_MIN = 5
        rec.record(1_000_020.0, REF)
        self.assertTrue(any("skipped for 5 min" in l for l in lines))
        self.assertEqual({until for _, _, until in rec.parked.values()}, {16667 + 5})

    def test_non_gas_errors_do_not_split(self):
        rec, chain, lines = make()
        from rpc import RpcError

        def down(*_a, **_k):
            raise RpcError("rate limited (HTTP 429)")
        chain.eth_call = down
        self.assertEqual(rec.record(1_000_000.0, REF), [])
        self.assertEqual([len(g) for g in rec.groups["cbBTC/WBTC"]], [2])
        self.assertEqual(rec.parked, {})
        self.assertIn("minute skipped (RpcError: rate limited (HTTP 429))", lines[-1])

    def test_gas_error_text(self):
        self.assertTrue(recorder.is_gas_error(Exception(
            "eth_call: {'code': -32003, 'message': 'out of gas: gas required exceeds: 100000000'}")))
        self.assertTrue(recorder.is_gas_error(Exception("execution reverted: Out Of Gas")))
        self.assertFalse(recorder.is_gas_error(Exception("rate limited (HTTP 429)")))

    def test_one_failing_pair_fails_alone(self):
        rec, chain, lines = make()
        real = chain.eth_call

        def picky(to, data, block="latest"):
            # fail only the call that asks about the USDT token (the USDT/USDC pair)
            if bytes.fromhex(rec.pairs[1][0]["a"]["address"][2:].lower()) in data:
                from rpc import RpcError
                raise RpcError("eth_call: header not found")
            return real(to, data, block)
        chain.eth_call = picky
        quotes = rec.record(1_000_000.0, REF)
        self.assertEqual({q.pair for q in quotes}, {"cbBTC/WBTC"})
        self.assertIn("recorder: USDT/USDC skipped this minute (RpcError: eth_call: header not found)",
                      lines[-1])
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
