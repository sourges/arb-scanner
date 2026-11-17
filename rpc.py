"""Minimal JSON-RPC client: the scanner only needs eth_call and eth_getCode.

Anything wrong with a reply (network, HTTP status, not JSON, no result, bad hex)
is raised as RpcError. The scanner's loop retries RpcError with a back-off, so a
bad reply costs one round and never stops the scanner.
"""
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
        try:
            data = resp.json()
        except ValueError as e:
            raise RpcError(f"{method}: reply is not JSON: {resp.text[:200]!r}") from e
        if not isinstance(data, dict):
            raise RpcError(f"{method}: unexpected reply: {str(data)[:200]}")
        if data.get("error"):
            raise RpcError(f"{method}: {data['error']}")
        if "result" not in data:
            raise RpcError(f"{method}: reply has no result: {str(data)[:200]}")
        return data["result"]

    def eth_call(self, to: str, data: bytes, block: str = "latest") -> bytes:
        result = self.call("eth_call", [{"to": to, "data": "0x" + data.hex()}, block])
        return hex_to_bytes("eth_call", result)

    def get_code(self, address: str) -> bytes:
        result = self.call("eth_getCode", [address, "latest"])
        return hex_to_bytes("eth_getCode", result)


def hex_to_bytes(method: str, result) -> bytes:
    """'0x...' -> bytes. A result that isn't a hex string is an RpcError, not a crash."""
    if not isinstance(result, str) or not result.startswith("0x"):
        raise RpcError(f"{method}: result is not hex: {str(result)[:80]!r}")
    try:
        return bytes.fromhex(result[2:])
    except ValueError as e:
        raise RpcError(f"{method}: result is not valid hex ({len(result)} chars)") from e
