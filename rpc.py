"""Minimal JSON-RPC client: the scanner only needs eth_call, eth_getCode and eth_chainId.

Anything wrong with a reply (network, HTTP status, not JSON, no result, bad hex)
is raised as RpcError. The scanner's loop retries RpcError with a back-off, so a
bad reply costs one round and never stops the scanner.

RpcPool spreads the load over two (or more) providers, e.g. Alchemy + Ankr, so
neither runs out of its free monthly quota:
  - Load split: rotate() is called once per scan and picks the next provider in the
    weighted rotation (RPC_WEIGHTS). Every call in that scan goes to that provider,
    so one scan's quotes, probes and recorder rows all come from the same node.
  - Failover: a failed call is tried once on the next healthy provider, so a bad
    reply from one doesn't cost the round. A provider that rate-limits (HTTP 429)
    or fails fail_limit calls in a row is benched for bench_s seconds and skipped
    by the rotation, then tried again. If every provider is benched, the one whose
    bench ends first is used anyway (the scanner never stalls on its own).
  - Names: a provider is shown by its domain ("alchemy", "ankr"), never its full
    URL, because the API key is in the URL.
"""
import itertools
import threading
import time
from urllib.parse import urlparse

import requests

BASE_CHAIN_ID = 8453


class RpcError(Exception):
    pass


class RpcRateLimited(RpcError):
    """HTTP 429: the provider wants us to slow down. Benches it at once in RpcPool."""


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
            raise RpcRateLimited(f"{method}: rate limited (HTTP 429)")
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

    def chain_id(self) -> int:
        result = self.call("eth_chainId", [])
        return int.from_bytes(hex_to_bytes("eth_chainId", result), "big")


def hex_to_bytes(method: str, result) -> bytes:
    """'0x...' -> bytes. A result that isn't a hex string is an RpcError, not a crash."""
    if not isinstance(result, str) or not result.startswith("0x"):
        raise RpcError(f"{method}: result is not hex: {str(result)[:80]!r}")
    try:
        return bytes.fromhex(result[2:])
    except ValueError as e:
        raise RpcError(f"{method}: result is not valid hex ({len(result)} chars)") from e


def provider_name(url: str) -> str:
    """Short, key-free name: base-mainnet.g.alchemy.com -> alchemy, rpc.ankr.com -> ankr."""
    host = urlparse(url).hostname or url
    parts = host.split(".")
    return parts[-2] if len(parts) >= 2 else host


class _Slot:
    def __init__(self, client, name):
        self.client = client
        self.name = name
        self.scans = 0           # scans this provider was picked for
        self.calls = 0           # calls it answered
        self.errors = 0          # calls it failed (all time)
        self.in_a_row = 0        # failures since its last success
        self.benched_until = 0.0


class RpcPool:
    """Same interface as RpcClient (call, eth_call, get_code, chain_id), over several clients.

    clients: [RpcClient]; weights: one int per client (e.g. [1, 1] = alternate,
    [2, 1] = two scans on the first for every one on the second)."""

    def __init__(self, clients, weights=None, fail_limit=3, bench_s=60.0, log=None, clock=time.time):
        if not clients:
            raise ValueError("RpcPool needs at least one client")
        weights = list(weights or [1] * len(clients))[:len(clients)]
        weights += [1] * (len(clients) - len(weights))
        base = [provider_name(c.url) for c in clients]
        names = [n if base[:i].count(n) == 0 else f"{n}{base[:i].count(n) + 1}"   # alchemy, alchemy2
                 for i, n in enumerate(base)]
        self.slots = [_Slot(c, n) for c, n in zip(clients, names)]
        self.order = [i for i, w in enumerate(weights) for _ in range(max(0, int(w)))] or [0]
        self.fail_limit = fail_limit
        self.bench_s = bench_s
        self.log = log or (lambda _msg: None)
        self.clock = clock
        self.lock = threading.Lock()
        self.turn = -1
        self.active = self.order[0]
        self.url = clients[0].url     # for code that prints the main RPC

    # ---- choosing a provider ----------------------------------------------------
    def _healthy(self, i, now):
        return self.slots[i].benched_until <= now

    def rotate(self):
        """Called once per scan: move to the next healthy provider in the rotation."""
        with self.lock:
            now = self.clock()
            for _ in range(len(self.order)):
                self.turn = (self.turn + 1) % len(self.order)
                i = self.order[self.turn]
                if self._healthy(i, now):
                    break
            else:                                 # all benched: the one back soonest
                i = min(range(len(self.slots)), key=lambda k: self.slots[k].benched_until)
            self.active = i
            self.slots[i].scans += 1
            return self.slots[i].name

    def _fallback(self, tried, now):
        """Next healthy provider not tried yet in this call, or None."""
        n = len(self.slots)
        for step in range(1, n):
            i = (self.active + step) % n
            if i not in tried and self._healthy(i, now):
                return i
        return None

    # ---- bookkeeping ------------------------------------------------------------
    def _ok(self, i):
        with self.lock:
            s = self.slots[i]
            if s.benched_until:
                self.log(f"RPC {s.name} is answering again")
                s.benched_until = 0.0
            s.calls += 1
            s.in_a_row = 0

    def _failed(self, i, err):
        with self.lock:
            s = self.slots[i]
            s.errors += 1
            s.in_a_row += 1
            now = self.clock()
            if s.benched_until > now:
                return
            if isinstance(err, RpcRateLimited) or s.in_a_row >= self.fail_limit:
                why = "rate limited" if isinstance(err, RpcRateLimited) else f"{s.in_a_row} errors in a row"
                s.benched_until = now + self.bench_s
                self.log(f"RPC {s.name} benched for {self.bench_s:.0f}s ({why}: {err})")

    # ---- RpcClient interface ----------------------------------------------------
    def _run(self, fn):
        with self.lock:
            i = self.active
        tried = set()
        while True:
            tried.add(i)
            try:
                out = fn(self.slots[i].client)
            except RpcError as e:
                self._failed(i, e)
                with self.lock:
                    nxt = self._fallback(tried, self.clock())
                if nxt is None:
                    raise
                i = nxt
                continue
            self._ok(i)
            return out

    def call(self, method, params):
        return self._run(lambda c: c.call(method, params))

    def eth_call(self, to, data, block="latest"):
        return self._run(lambda c: c.eth_call(to, data, block))

    def get_code(self, address):
        return self._run(lambda c: c.get_code(address))

    def chain_id(self):
        return self._run(lambda c: c.chain_id())

    # ---- reporting --------------------------------------------------------------
    @property
    def active_name(self):
        return self.slots[self.active].name

    def status(self):
        """'rpc ankr (alchemy 51% / ankr 49%)'. One provider: 'rpc alchemy'."""
        if len(self.slots) == 1:
            return f"rpc {self.slots[0].name}"
        total = sum(s.calls for s in self.slots) or 1
        split = " / ".join(f"{s.name} {100 * s.calls / total:.0f}%" for s in self.slots)
        return f"rpc {self.active_name} ({split})"

    def summary(self):
        """One line per provider, for the log."""
        return ", ".join(f"{s.name}: {s.calls} calls, {s.errors} errors" for s in self.slots)


def check_providers(clients, clock=time.perf_counter):
    """For --once and startup: [(name, ms or None, problem or None)] per client.
    A provider must answer eth_chainId with Base (8453)."""
    out = []
    for c in clients:
        name = provider_name(c.url)
        t0 = clock()
        try:
            cid = c.chain_id()
        except RpcError as e:
            out.append((name, None, str(e)))
            continue
        ms = (clock() - t0) * 1000
        out.append((name, ms, None if cid == BASE_CHAIN_ID else f"chain id {cid}, not Base ({BASE_CHAIN_ID})"))
    return out
