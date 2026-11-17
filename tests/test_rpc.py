"""rpc.py: every kind of bad reply must come out as RpcError (the scanner retries those)."""
import unittest

import requests

from rpc import RpcClient, RpcError, hex_to_bytes


class Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class Session:
    def __init__(self, resp=None, error=None):
        self.resp, self.error, self.bodies = resp, error, []

    def post(self, url, json, timeout):
        self.bodies.append(json)
        if self.error:
            raise self.error
        return self.resp


def client(resp=None, error=None):
    return RpcClient("http://x", session=Session(resp, error))


class TestRpc(unittest.TestCase):
    def test_good_reply_is_decoded(self):
        c = client(Resp(payload={"jsonrpc": "2.0", "id": 1, "result": "0x00ff"}))
        self.assertEqual(c.eth_call("0x" + "11" * 20, b"\x01\x02"), b"\x00\xff")
        body = c.session.bodies[0]
        self.assertEqual(body["method"], "eth_call")
        self.assertEqual(body["params"][0]["data"], "0x0102")

    def test_empty_result_is_empty_bytes_not_an_error(self):
        # "0x" is a valid answer (e.g. no code at an address); callers decide what it means
        self.assertEqual(client(Resp(payload={"result": "0x"})).get_code("0x" + "11" * 20), b"")

    def test_network_error_429_and_http_error(self):
        with self.assertRaisesRegex(RpcError, "network error"):
            client(error=requests.ConnectionError("down")).call("eth_call", [])
        with self.assertRaisesRegex(RpcError, "rate limited"):
            client(Resp(status=429)).call("eth_call", [])
        with self.assertRaisesRegex(RpcError, "HTTP 503"):
            client(Resp(status=503, text="busy")).call("eth_call", [])

    def test_error_field_is_rpc_error(self):
        with self.assertRaisesRegex(RpcError, "execution reverted"):
            client(Resp(payload={"error": {"code": 3, "message": "execution reverted"}})).call("eth_call", [])

    def test_reply_that_is_not_json(self):
        with self.assertRaisesRegex(RpcError, "not JSON"):
            client(Resp(text="<html>Bad Gateway</html>")).call("eth_call", [])

    def test_reply_without_result(self):
        with self.assertRaisesRegex(RpcError, "no result"):
            client(Resp(payload={"jsonrpc": "2.0", "id": 1})).call("eth_call", [])
        with self.assertRaisesRegex(RpcError, "unexpected reply"):
            client(Resp(payload=[])).call("eth_call", [])

    def test_result_that_is_not_hex(self):
        for bad in (None, 5, "", "abc", "0xzz", "0xabc"):      # "0xabc" = odd number of digits
            with self.assertRaises(RpcError, msg=repr(bad)):
                client(Resp(payload={"result": bad})).eth_call("0x" + "11" * 20, b"")
        self.assertEqual(hex_to_bytes("m", "0x"), b"")


if __name__ == "__main__":
    unittest.main()
