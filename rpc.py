"""Minimal JSON-RPC client: the scanner only needs eth_call and eth_getCode."""
import itertools

import requests


class RpcError(Exception):
    pass


class RpcClient:
    def __init__(self, url: str, timeout: float = 10, session=None):
        self.url = url
        self.timeout = timeout
        self.session = session or requests.Session()
        self._ids = itertools.count(1)

    def call(self, method: str, params: list):
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        try:
            resp = self.session.post(self.url, json=body, timeout=self.timeout)
        except requests.RequestException as e:
            raise RpcError(f"{method}: network error: {e}") from e
        if resp.status_code == 429:
            raise RpcError(f"{method}: rate limited (HTTP 429)")
        if resp.status_code != 200:
            raise RpcError(f"{method}: HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if "error" in data:
            raise RpcError(f"{method}: {data['error']}")
        return data["result"]

    def eth_call(self, to: str, data: bytes, block: str = "latest") -> bytes:
        result = self.call("eth_call", [{"to": to, "data": "0x" + data.hex()}, block])
        return bytes.fromhex(result[2:])

    def get_code(self, address: str) -> bytes:
        result = self.call("eth_getCode", [address, "latest"])
        return bytes.fromhex(result[2:])
