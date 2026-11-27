"""rpc.RpcPool: load split over two providers, failover, benching, and scanner wiring.

Fake clients answer from a script, so every number here is counted by hand:
  - weights [1, 1]: scans go A, B, A, B ...; weights [2, 1]: A, A, B, A, A, B ...
  - a failed call is tried once on the other provider (so the round isn't lost)
  - 3 failures in a row (RPC_FAIL_LIMIT) or one HTTP 429 benches a provider for 60s
"""
import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

import db
import scanner as scanner_mod
from rpc import (BASE_CHAIN_ID, RpcCallError, RpcClient, RpcError, RpcPool, RpcRateLimited,
                 check_providers, provider_name)
from scanner import Scanner, build_rpc, status_line
from tests.fakes import FakeChain, FakeFetchers, make_config
from tests.test_rpc import Resp, Session

ALCHEMY = "https://base-mainnet.g.alchemy.com/v2/SECRETKEY1"
ANKR = "https://rpc.ankr.com/base/SECRETKEY2"


class FakeClient:
    """fail: list of errors to raise on the next calls (None = answer normally)."""

    def __init__(self, url, chain=BASE_CHAIN_ID):
        self.url = url
        self.chain = chain
        self.fail = []
        self.calls = 0

    def _next(self, value):
        self.calls += 1
        if self.fail:
            err = self.fail.pop(0)
            if err is not None:
                raise err
        return value

    def call(self, method, params):
        return self._next(f"{self.url}:{method}")

    def eth_call(self, to, data, block="latest"):
        return self._next(self.url.encode())

    def get_code(self, address):
        return self._next(b"\x60")

    def chain_id(self):
        return self._next(self.chain)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def pool(weights=(1, 1), fail_limit=3, bench_s=60.0):
    a, b = FakeClient(ALCHEMY), FakeClient(ANKR)
    lines, clock = [], Clock()
    p = RpcPool([a, b], list(weights), fail_limit, bench_s, lines.append, clock)
    return p, a, b, lines, clock


class TestNames(unittest.TestCase):
    def test_short_names_never_show_the_key(self):
        self.assertEqual(provider_name(ALCHEMY), "alchemy")
        self.assertEqual(provider_name(ANKR), "ankr")
        self.assertEqual(provider_name("https://mainnet.base.org"), "base")

    def test_two_urls_on_one_provider_get_numbered(self):
        p = RpcPool([FakeClient(ALCHEMY), FakeClient(ALCHEMY + "x")])
        self.assertEqual([s.name for s in p.slots], ["alchemy", "alchemy2"])


class TestSplit(unittest.TestCase):
    def test_alternates_one_to_one(self):
        p, *_ = pool()
        self.assertEqual([p.rotate() for _ in range(4)], ["alchemy", "ankr", "alchemy", "ankr"])

    def test_weights_two_to_one(self):
        p, *_ = pool(weights=(2, 1))
        self.assertEqual([p.rotate() for _ in range(6)],
                         ["alchemy", "alchemy", "ankr", "alchemy", "alchemy", "ankr"])

    def test_every_call_in_a_scan_goes_to_that_scans_provider(self):
        p, a, b, *_ = pool()
        p.rotate()                                   # alchemy
        self.assertEqual(p.eth_call("0x0", b""), ALCHEMY.encode())
        p.get_code("0x0")
        p.rotate()                                   # ankr
        self.assertEqual(p.eth_call("0x0", b""), ANKR.encode())
        self.assertEqual((a.calls, b.calls), (2, 1))

    def test_status_shows_active_and_split(self):
        p, *_ = pool()
        for _ in range(4):
            p.rotate()
            p.eth_call("0x0", b"")
        p.rotate()
        p.eth_call("0x0", b"")                       # alchemy 3 calls, ankr 2
        self.assertEqual(p.status(), "rpc alchemy (alchemy 60% / ankr 40%)")
        self.assertEqual(p.summary(), "alchemy: 3 calls, 0 errors, ankr: 2 calls, 0 errors")

    def test_single_provider_status(self):
        self.assertEqual(RpcPool([FakeClient(ALCHEMY)]).status(), "rpc alchemy")


class TestFailover(unittest.TestCase):
    def test_one_failure_is_answered_by_the_other(self):
        p, a, b, lines, _ = pool()
        p.rotate()
        a.fail = [RpcError("HTTP 503")]
        self.assertEqual(p.eth_call("0x0", b""), ANKR.encode())   # round not lost
        self.assertEqual(p.slots[0].errors, 1)
        self.assertEqual(lines, [])                                 # 1 < 3: not benched
        self.assertEqual(p.rotate(), "ankr")                        # rotation unchanged

    def test_success_resets_the_count(self):
        p, a, *_ = pool()
        p.rotate()
        for _ in range(4):                                          # fail, ok, fail, ok ...
            a.fail = [RpcError("x")]
            p.eth_call("0x0", b"")
            p.eth_call("0x0", b"")
        self.assertEqual(p.slots[0].benched_until, 0.0)
        self.assertEqual(p.slots[0].in_a_row, 0)

    def test_three_in_a_row_benches_then_it_comes_back(self):
        p, a, b, lines, clock = pool()
        p.rotate()                                                  # alchemy
        a.fail = [RpcError("HTTP 503")] * 3
        for _ in range(3):
            p.eth_call("0x0", b"")
        self.assertEqual(len(lines), 1)
        self.assertIn("RPC alchemy benched for 60s (3 errors in a row: HTTP 503)", lines[0])
        self.assertEqual([p.rotate() for _ in range(3)], ["ankr", "ankr", "ankr"])
        clock.t = 60.0                                              # bench over
        self.assertEqual(p.rotate(), "alchemy")
        p.eth_call("0x0", b"")
        p.eth_call("0x0", b"")
        self.assertEqual(lines[-1], "RPC alchemy is answering again")
        self.assertEqual(lines.count("RPC alchemy is answering again"), 1)   # said once, bench cleared
        self.assertEqual(p.slots[0].benched_until, 0.0)

    def test_rate_limit_benches_at_once(self):
        p, a, b, lines, _ = pool()
        p.rotate()
        a.fail = [RpcRateLimited("eth_call: rate limited (HTTP 429)")]
        self.assertEqual(p.eth_call("0x0", b""), ANKR.encode())
        self.assertIn("RPC alchemy benched for 60s (rate limited", lines[0])
        self.assertEqual(p.rotate(), "ankr")
        self.assertEqual(p.rotate(), "ankr")

    def test_both_failing_raises_for_the_scanner_to_retry(self):
        p, a, b, *_ = pool()
        p.rotate()
        a.fail, b.fail = [RpcError("a down")], [RpcError("b down")]
        with self.assertRaisesRegex(RpcError, "b down"):
            p.eth_call("0x0", b"")
        self.assertEqual((a.calls, b.calls), (1, 1))                # each tried once, no loop

    def test_all_benched_uses_the_one_back_soonest(self):
        p, a, b, lines, clock = pool()
        p.rotate()
        a.fail = [RpcRateLimited("429")]
        p.eth_call("0x0", b"")                                      # alchemy benched to 60
        clock.t = 10.0
        self.assertEqual(p.rotate(), "ankr")
        b.fail = [RpcRateLimited("429")]
        with self.assertRaises(RpcError):                           # ankr benched to 70, and
            p.eth_call("0x0", b"")                                  # alchemy is still benched
        self.assertEqual(p.rotate(), "alchemy")                     # back at 60, before 70
        self.assertEqual(p.rotate(), "alchemy")

    def test_benched_provider_is_not_used_as_fallback(self):
        p, a, b, lines, clock = pool()
        p.rotate()
        a.fail = [RpcRateLimited("429")]
        p.eth_call("0x0", b"")                                      # alchemy benched
        p.rotate()                                                  # ankr
        b.fail = [RpcError("b down")]
        with self.assertRaisesRegex(RpcError, "b down"):
            p.eth_call("0x0", b"")
        self.assertEqual(a.calls, 1)                                # benched one left alone


class TestCallErrors(unittest.TestCase):
    """Nov 27, 2026: out of gas / reverted are the call's fault, not the provider's."""

    def gas_err(self):
        return RpcCallError("eth_call: {'code': -32003, 'message': 'out of gas: gas required exceeds: 550000000'}")

    def test_not_retried_on_the_other_provider(self):
        p, a, b, lines, _ = pool()
        p.rotate()
        a.fail = [self.gas_err()]
        with self.assertRaises(RpcCallError):
            p.eth_call("0x0", b"")
        self.assertEqual((a.calls, b.calls), (1, 0))

    def test_never_benches_and_counts_as_answered(self):
        p, a, b, lines, _ = pool()
        p.rotate()
        a.fail = [self.gas_err()] * 5
        for _ in range(5):
            with self.assertRaises(RpcCallError):
                p.eth_call("0x0", b"")
        self.assertEqual(lines, [])
        self.assertEqual((p.slots[0].errors, p.slots[0].benched_until, p.slots[0].calls), (0, 0.0, 5))
        self.assertEqual(p.rotate(), "ankr")
        self.assertEqual(p.rotate(), "alchemy")                     # still in the rotation

    def test_resets_a_run_of_real_failures(self):
        p, a, *_ = pool()
        p.rotate()
        a.fail = [RpcError("HTTP 503"), RpcError("HTTP 503"), self.gas_err(), RpcError("HTTP 503")]
        for _ in range(4):
            try:
                p.eth_call("0x0", b"")
            except RpcError:
                pass
        self.assertEqual(p.slots[0].benched_until, 0.0)             # 2, answer, 1: not 3 in a row

    def test_recorder_still_sees_a_gas_error(self):
        import recorder
        self.assertTrue(recorder.is_gas_error(self.gas_err()))

    def test_client_classifies_json_errors(self):
        out_of_gas = {"error": {"code": -32003, "message": "out of gas: gas required exceeds: 1"}}
        reverted = {"error": {"code": 3, "message": "execution reverted"}}
        limited = {"error": {"code": -32005, "message": "daily request limit exceeded"}}
        for payload in (out_of_gas, reverted):
            c = RpcClient("http://x", session=Session(Resp(payload=payload)))
            with self.assertRaises(RpcCallError):
                c.call("eth_call", [])
        c = RpcClient("http://x", session=Session(Resp(payload=limited)))
        with self.assertRaises(RpcError) as cm:
            c.call("eth_call", [])
        self.assertNotIsInstance(cm.exception, RpcCallError)        # a provider problem: counts
        self.assertTrue(issubclass(RpcCallError, RpcError))         # scanner still retries the round


class TestRpcClient(unittest.TestCase):
    def test_429_is_rate_limited_and_still_an_rpc_error(self):
        c = RpcClient("http://x", session=Session(Resp(status=429)))
        with self.assertRaises(RpcRateLimited):
            c.call("eth_call", [])
        self.assertTrue(issubclass(RpcRateLimited, RpcError))      # scanner still retries it

    def test_503_is_not_rate_limited(self):
        c = RpcClient("http://x", session=Session(Resp(status=503, text="busy")))
        with self.assertRaises(RpcError) as cm:
            c.call("eth_call", [])
        self.assertNotIsInstance(cm.exception, RpcRateLimited)

    def test_chain_id(self):
        c = RpcClient("http://x", session=Session(Resp(payload={"result": "0x2105"})))
        self.assertEqual(c.chain_id(), 8453)
        self.assertEqual(c.session.bodies[0]["method"], "eth_chainId")


class TestCheckProviders(unittest.TestCase):
    def test_ok_wrong_chain_and_down(self):
        down = FakeClient("https://rpc.ankr.com/eth/K")
        down.fail = [RpcError("network error")]
        ticks = iter([0.0, 0.085, 1.0, 1.0, 2.0])
        res = check_providers([FakeClient(ALCHEMY), FakeClient(ANKR, chain=1), down],
                              clock=lambda: next(ticks))
        self.assertEqual(res[0][0], "alchemy")
        self.assertAlmostEqual(res[0][1], 85.0)
        self.assertIsNone(res[0][2])
        self.assertEqual(res[1][2], "chain id 1, not Base (8453)")
        self.assertEqual(res[2], ("ankr", None, "network error"))


def cfg_with(url2):
    return SimpleNamespace(RPC_URL=ALCHEMY, RPC_URL_2=url2, RPC_TIMEOUT_S=10,
                           RPC_WEIGHTS=[1, 1], RPC_FAIL_LIMIT=3, RPC_BENCH_S=60)


class TestBuildRpc(unittest.TestCase):
    def build(self, url2, chains=None):
        chains = chains or {}
        lines = []
        p = build_rpc(cfg_with(url2), lines.append,
                      make_client=lambda u: FakeClient(u, chains.get(u, BASE_CHAIN_ID)))
        return p, lines

    def test_no_second_url_runs_as_before(self):
        p, lines = self.build(None)
        self.assertEqual([s.name for s in p.slots], ["alchemy"])
        self.assertEqual(p.rotate(), "alchemy")
        self.assertEqual(p.rotate(), "alchemy")

    def test_second_url_splits(self):
        p, lines = self.build(ANKR)
        self.assertEqual([s.name for s in p.slots], ["alchemy", "ankr"])
        self.assertTrue(any(l.startswith("RPC ankr: OK, Base") for l in lines))
        self.assertIn("RPC split: alchemy x1 / ankr x1 scans per turn", lines)

    def test_second_url_on_wrong_chain_is_left_out(self):
        p, lines = self.build(ANKR, chains={ANKR: 1})
        self.assertEqual([s.name for s in p.slots], ["alchemy"])
        self.assertIn("RPC ankr: chain id 1, not Base (8453)  -> left out, running on the main RPC only",
                      lines)

    def test_keys_never_logged(self):
        _, lines = self.build(ANKR)
        self.assertFalse(any("SECRETKEY" in l for l in lines))


class Rotating:
    """Wraps a FakeChain so run_forever's rotate() and status() can be seen."""

    def __init__(self, chain):
        self.chain = chain
        self.rotations = 0

    def rotate(self):
        self.rotations += 1

    def status(self):
        return "rpc ankr (alchemy 50% / ankr 50%)"

    def __getattr__(self, name):
        return getattr(self.chain, name)


class TestScannerWiring(unittest.TestCase):
    def make(self):
        cfg = make_config()
        chain = FakeChain()
        t = [1_000_000.0]
        clock = lambda: t[0]
        mids = {("kraken", "ETHUSD"): 2000.0, ("kraken", "XBTUSD"): 60000.0,
                ("coinbase", "ETH-USD"): 2000.0, ("coinbase", "BTC-USD"): 60000.0}
        rpc = Rotating(chain)
        s = Scanner(cfg, rpc, db.connect(":memory:"), fetchers=FakeFetchers(mids, clock).as_dict(),
                    clock=clock)
        with redirect_stdout(io.StringIO()):
            s.startup()
        return s, chain, rpc

    def test_rotates_once_per_scan_and_status_shows_rpc(self):
        s, chain, rpc = self.make()
        with redirect_stdout(io.StringIO()) as out:
            scanner_mod.run_forever(s, 2.0, sleep=lambda _s: None, clock=lambda: 0.0, max_rounds=3)
        s.shutdown()
        self.assertEqual(rpc.rotations, 3)
        self.assertIn("| rpc ankr (alchemy 50% / ankr 50%)", out.getvalue())

    def test_older_block_from_a_lagging_provider_is_skipped(self):
        s, chain, _ = self.make()
        chain.block = 105
        self.assertIsNotNone(s.step())
        chain.block = 104                                 # other provider one block behind
        self.assertIsNone(s.step())
        self.assertEqual(s.last_block, 105)
        chain.block = 106
        self.assertIsNotNone(s.step())
        s.shutdown()

    def test_status_line_without_rpc_is_unchanged(self):
        self.assertNotIn("rpc", status_line(1, {}))
        self.assertTrue(status_line(1, {}, rpc="rpc alchemy").endswith("| rpc alchemy"))


if __name__ == "__main__":
    unittest.main()
