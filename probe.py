"""Size probes: how much money could a dex>dex gap absorb?

Every route is scored at $100. The gaps that show up are mostly on thin pools,
where a bigger trade moves the price against itself. So when a dex>dex episode
OPENS at or above PROBE_MIN_NET_PCT, the same buy pool -> sell pool trade is
quoted again at each of PROBE_SIZES_USD, in ONE extra eth_call.

How one size is priced (same method as the main scan):
    buy : quoter, "USDC in -> how much ETH/BTC out", for the full size
    sell: quoter, "x ETH/BTC in -> how much USDC out", for x = size / CEX price,
          then scaled to the amount the buy actually produced (they differ by the
          gap, a fraction of a percent, so the scaling is accurate)
    raw_edge = usd_back / size - 1                  (pool fees and slippage in)
    net_edge = (usd_back - gas both legs) / size - 1
A size the pool can't fill (quoter reverts or returns 0) is stored with NULL
edges: "can't fill" is part of the answer.

The probe runs a moment after the main scan (one more RPC round trip, ~0.2s
or about one Base block later), so the $100 probe can differ a little from the
episode's own $100 number. That's why $100 is probed too: every size in a probe
comes from the same block and compares fairly.

Rows go in episode_probes, one per size, using the shared columns
(ts, strategy, venue, path, size_usd, raw_edge, net_edge) like tri_quotes.
A probe that fails is logged and skipped; it never stops the scanner.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

import abi
import db
import dex


@dataclass
class ProbeResult:
    pair: str
    buy_pool: str
    sell_pool: str
    size_usd: float
    base_amount: float | None    # ETH/BTC bought (None = buy couldn't fill)
    usd_back: float | None       # USDC from selling it (None = couldn't fill)
    gas_usd: float

    @property
    def path(self) -> str:
        return f"{self.pair} {self.buy_pool}>{self.sell_pool}"

    @property
    def filled(self) -> bool:
        return self.usd_back is not None

    @property
    def raw_edge(self):
        return (self.usd_back / self.size_usd - 1) * 100 if self.filled else None

    @property
    def net_edge(self):
        return ((self.usd_back - self.gas_usd) / self.size_usd - 1) * 100 if self.filled else None

    @property
    def buy_px(self):
        return self.size_usd / self.base_amount if self.base_amount else None

    @property
    def sell_px(self):
        return self.usd_back / self.base_amount if self.filled and self.base_amount else None


def build_calls(cfg, pair, buy_pool, sell_pool, sizes, ref_price):
    """Two quoter calls per size: buy on buy_pool, sell on sell_pool. Returns (calls, x_raws)."""
    usdc, base = cfg.USDC, pair["base"]
    calls, x_raws = [], []
    for size in sizes:
        usdc_in = int(size * 10 ** usdc["decimals"])
        x_raw = int(size / ref_price * 10 ** base["decimals"])
        x_raws.append(x_raw)
        calls.append((buy_pool.quoter, abi.encode_quote(
            buy_pool.kind, usdc["address"], base["address"], usdc_in, buy_pool.tier)))
        calls.append((sell_pool.quoter, abi.encode_quote(
            sell_pool.kind, base["address"], usdc["address"], x_raw, sell_pool.tier)))
    return calls, x_raws


def _quote(ok_ret):
    """(amount out, gas) or (None, None) if the quoter reverted. A 0 amount comes back
    as 0 and is treated as "can't fill" by quote_sizes."""
    ok, ret = ok_ret
    if not ok or len(ret) < 128:
        return None, None
    return abi.decode_quote(ret)


def quote_sizes(rpc, cfg, pair, buy_pool, sell_pool, sizes, ref_price, basefee_wei, l1_fee_wei, eth_usd):
    """Price the buy_pool -> sell_pool round trip at every size in one eth_call."""
    calls, x_raws = build_calls(cfg, pair, buy_pool, sell_pool, sizes, ref_price)
    results = dex.multicall(rpc, cfg.MULTICALL3, calls)
    base_dec, usdc_dec = pair["base"]["decimals"], cfg.USDC["decimals"]

    def gas(units):
        return dex.leg_gas_usd(units, basefee_wei, cfg.GAS_PRIORITY_WEI, cfg.SWAP_GAS_OVERHEAD,
                               l1_fee_wei, cfg.FALLBACK_L1_FEE_USD, eth_usd)

    out = []
    for i, size in enumerate(sizes):
        buy_out, buy_gas = _quote(results[2 * i])
        sell_out, sell_gas = _quote(results[2 * i + 1])
        base_amount = buy_out / 10 ** base_dec if buy_out else None
        usd_back = None
        if base_amount and sell_out and x_raws[i]:
            usd_back = sell_out / 10 ** usdc_dec * (buy_out / x_raws[i])
        gas_usd = (gas(buy_gas) if buy_gas else 0.0) + (gas(sell_gas) if sell_gas else 0.0)
        out.append(ProbeResult(pair["name"], buy_pool.label, sell_pool.label, size,
                               base_amount, usd_back, gas_usd))
    return out


def fmt_size(size):
    return f"${size:,.0f}"


def probe_line(results):
    """One log line: 'PROBE ETH univ3-500>pcsv3-500: $100 +0.620% | ... | $5,000 can't fill'."""
    parts = []
    for r in results:
        parts.append(f"{fmt_size(r.size_usd)} " + (f"{r.net_edge:+.3f}%" if r.filled else "can't fill"))
    return f"PROBE {results[0].path}: " + " | ".join(parts)


class Prober:
    """Decides when to probe, runs it, stores it. Never raises into the scanner."""

    def __init__(self, cfg, rpc, conn, log, clock=time.time):
        self.cfg = cfg
        self.rpc = rpc
        self.conn = conn
        self.log = log
        self.clock = clock
        self.sizes = list(getattr(cfg, "PROBE_SIZES_USD", []))
        self.min_pct = getattr(cfg, "PROBE_MIN_NET_PCT", 0.05)
        self.routes = tuple(getattr(cfg, "PROBE_ROUTES", ("dex>dex",)))
        self.max_per_hour = getattr(cfg, "PROBE_MAX_PER_HOUR", 60)
        self.recent = deque()      # timestamps of probes in the last hour
        self.skipped = 0           # probes not run because of the hourly cap

    def openings(self, results, already_open):
        """Route results that START an episode this round and are big enough to probe."""
        if not self.sizes:
            return []
        return [r for k, r in results.items()
                if k[1] in self.routes and k not in already_open and r.net_pct >= self.min_pct]

    def allow(self, now):
        """Shared hourly cap: True (and counted) if one more probe may run now.
        tri_probe.py uses the same cap, so both kinds together stay under it."""
        while self.recent and now - self.recent[0] >= 3600:
            self.recent.popleft()
        if len(self.recent) >= self.max_per_hour:
            self.skipped += 1
            return False
        self.recent.append(now)
        return True

    def probe(self, now, block, opening, pair_pools, ref_prices, basefee_wei, l1_fee_wei):
        """opening: RouteResults from openings(). Returns {path: [ProbeResult]}."""
        done = {}
        for r in opening:
            if not self.allow(now):
                continue
            try:
                pair, pools = pair_pools[r.pair]
                by_label = {p.label: p for p in pools}
                buy_pool, sell_pool = by_label[r.buy_venue], by_label[r.sell_venue]
                results = quote_sizes(self.rpc, self.cfg, pair, buy_pool, sell_pool, self.sizes,
                                      ref_prices[r.pair], basefee_wei, l1_fee_wei, ref_prices["ETH"])
                db.insert_probes(self.conn, now, block, r.key, results)
                self.log(probe_line(results))
                done[results[0].path] = results
            except Exception as e:      # a failed probe is skipped, never fatal
                self.log(f"probe failed for {r.pair} {r.buy_venue}>{r.sell_venue} "
                         f"({type(e).__name__}: {e})")
        return done
