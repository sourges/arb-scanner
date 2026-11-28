"""Full path (Nov 28, 2026): node out-of-gas reply -> RpcClient -> RpcPool -> recorder split.

The pieces are tested separately elsewhere (test_rpc_pool: RpcCallError isn't retried or
counted; test_recorder_gas: the recorder splits on a gas error). This file runs them
together, with nothing faked between the HTTP reply and the recorder:

    two REAL RpcClients (alchemy, ankr), each over a fake HTTP session that answers
    JSON-RPC from its own FakeChain  ->  a REAL RpcPool  ->  a REAL Recorder.

The ankr node has Ankr's gas cap: an eth_call about cbBTC with more than 5 sub-calls gets
the real JSON-RPC error {"code": -32003, "message": "out of gas: gas required exceeds:
100000000"}. Alchemy has no cap. Only cbBTC/WBTC is heavy, so pair order is tested too.

Hand count (depth quotes on, cbBTC/WBTC has 2 pools, USDT/USDC has 2 pools):
    one call per pair = 1 block number + 2 pools x 4 quotes = 9 sub-calls
    minute 1 on ankr:  cbBTC (9) refused -> split into 2 calls of 5, both answered;
                       USDT/USDC (9) answered (the cap only applies to cbBTC calls)
                       -> 3 eth_calls answered + 1 refused, all on ankr, 0 on alchemy
    minutes 2-4:       the split is remembered: 3 calls each, no errors
    Blocks: ankr's node is at block 777, alchemy's at 999 -> every row says 777.
    Before the Nov 27 rpc.py fix, the refusal was retried on alchemy (rows at 999,
    cbBTC never split) and counted against ankr: benched by minute 2.
"""
import json
import unittest

from eth_abi import decode

import db
import recorder
from rpc import RpcPool, RpcClient
from tests.fakes import FakeChain, make_config

REF = {"ETH": 2000.0, "BTC": 60000.0}
ALCHEMY = "https://base-mainnet.g.alchemy.com/v2/SECRETKEY1"
ANKR = "https://rpc.ankr.com/base/SECRETKEY2"
GAS_ERROR = {"code": -32003, "message": "out of gas: gas required exceeds: 100000000"}


class Reply:
    def __init__(self, payload):
        self.status_code = 200
        self.payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


class Node:
    """A JSON-RPC node over HTTP, answered from a FakeChain.
    heavy: token address (bytes) whose eth_calls are capped at cap sub-calls."""

    def __init__(self, chain, heavy=None, cap=None):
        self.chain = chain
        self.heavy = heavy
        self.cap = cap
        self.answered = 0           # eth_calls that got a result
        self.refused = 0            # eth_calls refused as out of gas

    def post(self, url, json=None, timeout=None):
        body = json
        method, params = body["method"], body["params"]
        if method == "eth_call":
            to, data = params[0]["to"], bytes.fromhex(params[0]["data"][2:])
            (inner,) = decode(["(address,bool,bytes)[]"], data[4:])
            if self.cap is not None and self.heavy in data and len(inner) > self.cap:
                self.refused += 1
                return Reply({"jsonrpc": "2.0", "id": body["id"], "error": GAS_ERROR})
            self.answered += 1
            result = "0x" + self.chain.eth_call(to, data).hex()
        elif method == "eth_getCode":
            result = "0x" + self.chain.get_code(params[0]).hex()
        elif method == "eth_chainId":
            result = hex(8453)
        else:
            raise AssertionError(f"unexpected method {method}")
        return Reply({"jsonrpc": "2.0", "id": body["id"], "result": result})


def setup():
    """Recorder started on alchemy (uncapped), then the pool rotated to ankr, which is capped."""
    alchemy_chain, ankr_chain = FakeChain(), FakeChain()
    alchemy_chain.block, ankr_chain.block = 999, 777
    alchemy, ankr = Node(alchemy_chain), Node(ankr_chain)
    lines = []
    pool = RpcPool([RpcClient(ALCHEMY, session=alchemy), RpcClient(ANKR, session=ankr)],
                   [1, 1], fail_limit=3, bench_s=60.0, log=lines.append, clock=lambda: 0.0)
    rec = recorder.Recorder(make_config(RECORD_DEPTH_USD=1000.0), pool, db.connect(":memory:"),
                            lines.append)
    rec.startup()
    cbbtc = bytes.fromhex(rec.pairs[0][0]["a"]["address"][2:].lower())
    ankr.heavy, ankr.cap = cbbtc, 5
    alchemy.answered = ankr.answered = 0
    assert pool.rotate() == "alchemy" and pool.rotate() == "ankr"   # every call now goes to ankr
    return rec, pool, alchemy, ankr, lines


def slot(pool, name):
    return next(s for s in pool.slots if s.name == name)


class TestFullPath(unittest.TestCase):
    def start(self):
        rec, pool, alchemy, ankr, lines = setup()
        self.addCleanup(rec.conn.close)
        return rec, pool, alchemy, ankr, lines

    def test_gas_refusal_on_ankr_splits_instead_of_failing_over(self):
        rec, pool, alchemy, ankr, lines = self.start()
        quotes = rec.record(1_000_020.0, REF)

        # 1-2) the node's reply reaches the recorder as a call error, not a provider failure
        self.assertEqual((ankr.refused, ankr.answered), (1, 3))
        self.assertEqual(alchemy.answered, 0)                    # never retried on alchemy
        a = slot(pool, "ankr")
        self.assertEqual((a.errors, a.in_a_row, a.benched_until), (0, 0, 0.0))
        # 3) the recorder split cbBTC/WBTC and kept USDT/USDC whole
        self.assertEqual([len(g) for g in rec.groups["cbBTC/WBTC"]], [1, 1])
        self.assertEqual([len(g) for g in rec.groups["USDT/USDC"]], [2])
        self.assertIn("recorder: cbBTC/WBTC too heavy for one call: now 2 calls", lines)
        # 4) quotes recorded correctly (same values as the no-cap fake, see test_recorder)
        q = {(x.pair, x.pool): x for x in quotes}
        self.assertEqual(set(q), {("cbBTC/WBTC", "uni-500"), ("cbBTC/WBTC", "aero-ts100"),
                                  ("USDT/USDC", "uni-500")})
        self.assertAlmostEqual(q[("cbBTC/WBTC", "uni-500")].bid, 0.99970, delta=1e-5)
        self.assertAlmostEqual(q[("cbBTC/WBTC", "aero-ts100")].ask, 1.00040, delta=1e-5)
        self.assertAlmostEqual(q[("USDT/USDC", "uni-500")].bid, 0.9999, delta=1e-6)
        self.assertEqual((rec.failures, rec.pair_failures), (0, 0))
        # 5) pair order kept (cbBTC's halves came back after USDT/USDC) and ankr's block stored
        self.assertEqual([x.pair for x in quotes], ["cbBTC/WBTC", "cbBTC/WBTC", "USDT/USDC"])
        rows = rec.conn.execute("SELECT pair, block FROM pegged_quotes ORDER BY rowid").fetchall()
        self.assertEqual(rows, [("cbBTC/WBTC", 777), ("cbBTC/WBTC", 777), ("USDT/USDC", 777)])

    def test_ankr_is_never_benched_over_several_minutes(self):
        rec, pool, alchemy, ankr, lines = self.start()
        for minute in range(4):
            quotes = rec.record(1_000_020.0 + 60 * minute, REF)
            self.assertEqual(len(quotes), 3)
        # 6) one refusal in minute 1, then the remembered split: 3 answered every minute
        self.assertEqual((ankr.refused, ankr.answered, alchemy.answered), (1, 12, 0))
        self.assertEqual(slot(pool, "ankr").benched_until, 0.0)
        self.assertFalse(any("benched" in line for line in lines))
        blocks = {r[0] for r in rec.conn.execute("SELECT DISTINCT block FROM pegged_quotes")}
        self.assertEqual(blocks, {777})

    def test_non_gas_node_error_still_fails_over(self):
        # the narrow exemption: an ordinary node error is still retried on the other provider
        rec, pool, alchemy, ankr, lines = self.start()
        ankr.cap = None
        real = ankr.post

        def unwell(url, json=None, timeout=None):
            if json["method"] == "eth_call":
                return Reply({"jsonrpc": "2.0", "id": json["id"],
                              "error": {"code": -32000, "message": "header not found"}})
            return real(url, json=json, timeout=timeout)
        ankr.post = unwell
        quotes = rec.record(1_000_020.0, REF)
        self.assertEqual(len(quotes), 3)
        self.assertEqual(alchemy.answered, 2)                    # both pair calls failed over
        self.assertEqual(slot(pool, "ankr").in_a_row, 2)
        self.assertEqual([len(g) for g in rec.groups["cbBTC/WBTC"]], [2])   # no split


if __name__ == "__main__":
    unittest.main()
