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

Blocks (Nov 27, 2026): `block` is the scan that triggered the probe, `probe_block` is
the block the probe itself was quoted at (read in the same eth_call). Gas still uses
the triggering scan's base fee and L1 fee: a block later they differ by a fraction of
a cent at these sizes.

When to probe (Nov 27, 2026): the first scan of an episode at or above
PROBE_MIN_NET_PCT, whether that is the scan it opened on or a later one (an episode
that opens at +0.01% and climbs to +0.3% is probed when it crosses). Each episode is
probed at most once. A probe refused by the hourly cap is tried again on the next scan
while the episode stays above the threshold, and the first refusal per episode is
stored in probe_skips so the report can count what the cap hid.
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
    probe_block: int | None = None   # block the probe was quoted at

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


def build_calls(cfg, pair, buy_pool, sell_pool, sizes, ref_price, quote_usd=1.0):
    """Two quoter calls per size: buy on buy_pool, sell on sell_pool. Returns (calls, x_raws).
    Sizes are USD; the buy spends size / quote_usd of the pair's quote token (USDC = 1.0)."""
    usdc, base = dex.pair_quote(cfg, pair), pair["base"]
    calls, x_raws = [], []
    for size in sizes:
        usdc_in = int(size / quote_usd * 10 ** usdc["decimals"])
        x_raw = int(size / ref_price * 10 ** base["decimals"])
        x_raws.append(x_raw)
        calls.append(dex.quote_call(buy_pool, usdc["address"], base["address"], usdc_in))
        calls.append(dex.quote_call(sell_pool, base["address"], usdc["address"], x_raw))
    return calls, x_raws


def _quote(ok_ret, kind="uni"):
    """(amount out, gas) or (None, None) if the quote failed or came back 0 ("can't fill").
    Any pool kind: see dex.read_quote."""
    return dex.read_quote(kind, ok_ret)


def quote_sizes(rpc, cfg, pair, buy_pool, sell_pool, sizes, ref_price, basefee_wei, l1_fee_wei, eth_usd,
                quote_usd=1.0):
    """Price the buy_pool -> sell_pool round trip at every size in one eth_call.
    quote_usd: USD value of one quote token; usd_back is converted to USD."""
    calls, x_raws = build_calls(cfg, pair, buy_pool, sell_pool, sizes, ref_price, quote_usd)
    calls.append((cfg.MULTICALL3, abi.encode_no_args(abi.SIG_BLOCK_NUMBER)))
    results = dex.multicall(rpc, cfg.MULTICALL3, calls)
    probe_block = read_block(results.pop())
    base_dec, usdc_dec = pair["base"]["decimals"], dex.pair_quote(cfg, pair)["decimals"]

    def gas(units):
        return dex.leg_gas_usd(units, basefee_wei, cfg.GAS_PRIORITY_WEI, cfg.SWAP_GAS_OVERHEAD,
                               l1_fee_wei, cfg.FALLBACK_L1_FEE_USD, eth_usd)

    out = []
    for i, size in enumerate(sizes):
        buy_out, buy_gas = _quote(results[2 * i], buy_pool.kind)
        sell_out, sell_gas = _quote(results[2 * i + 1], sell_pool.kind)
        base_amount = buy_out / 10 ** base_dec if buy_out else None
        usd_back = None
        if base_amount and sell_out and x_raws[i]:
            usd_back = sell_out / 10 ** usdc_dec * (buy_out / x_raws[i]) * quote_usd
        gas_usd = (gas(buy_gas) if buy_gas else 0.0) + (gas(sell_gas) if sell_gas else 0.0)
        out.append(ProbeResult(pair["name"], buy_pool.label, sell_pool.label, size,
                               base_amount, usd_back, gas_usd, probe_block))
    return out


def read_block(ok_ret):
    """Block number from a Multicall3 getBlockNumber() result, or None."""
    ok, ret = ok_ret
    return abi.decode_uint(ret) if ok and len(ret) >= 32 else None


def fmt_size(size):
    return f"${size:,.0f}"


def probe_line(results):
    """One log line: 'PROBE ETH univ3-500>pcsv3-500: $100 +0.620% | ... | $5,000 can't fill'."""
    parts = []
    for r in results:
        parts.append(f"{fmt_size(r.size_usd)} " + (f"{r.net_edge:+.3f}%" if r.filled else "can't fill"))
    return f"PROBE {results[0].path}: " + " | ".join(parts)


class HourlyCap:
    """At most max_per_hour allowed in any rolling hour. allow(now) counts one if allowed."""

    def __init__(self, max_per_hour):
        self.max_per_hour = max_per_hour
        self.recent = deque()      # timestamps allowed in the last hour
        self.skipped = 0           # refused because the cap was reached

    def allow(self, now):
        while self.recent and now - self.recent[0] >= 3600:
            self.recent.popleft()
        if len(self.recent) >= self.max_per_hour:
            self.skipped += 1
            return False
        self.recent.append(now)
        return True


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
        self.cap = HourlyCap(getattr(cfg, "PROBE_MAX_PER_HOUR", 60))   # dex>dex probes only
        self.probed = {}      # (pair, route) -> start_ts of the episode already probed
        self.noted = {}       # (pair, route) -> start_ts of the episode whose refusal is stored
        self.pending = {}     # (pair, route) -> start_ts of the episode openings() picked

    def openings(self, results, open_eps):
        """Routes at or above the threshold whose current episode hasn't been probed yet.

        Call it AFTER the tracker has updated, with tracker.open, so this scan's new
        episodes are in open_eps. That covers an episode that opens big AND one that
        opens small and crosses the threshold later. A route not in open_eps (no tracker)
        counts as a new episode."""
        if not self.sizes:
            return []
        out = []
        for k, r in results.items():
            if k[1] not in self.routes or r.net_pct < self.min_pct:
                continue
            ep_id = getattr(open_eps.get(k), "start_ts", None)
            if k in self.probed and self.probed[k] == ep_id:
                continue                     # this episode was already probed
            self.pending[k] = ep_id
            out.append(r)
        for d in (self.probed, self.noted):   # forget episodes that have closed
            for k in [k for k in d if k not in open_eps and k not in self.pending]:
                del d[k]
        return out

    @property
    def skipped(self):
        return self.cap.skipped

    def allow(self, now):
        """This prober's own hourly cap (triangle probes have a separate one)."""
        return self.cap.allow(now)

    def probe(self, now, block, opening, pair_pools, ref_prices, basefee_wei, l1_fee_wei,
              quote_usd=None):
        """opening: RouteResults from openings(). quote_usd: {pair: USD per quote token}
        for pairs not quoted in USDC. Returns {path: [ProbeResult]}."""
        quote_usd = quote_usd or {}
        done = {}
        for r in opening:
            k = (r.pair, r.key)
            ep_id = self.pending.pop(k, None)
            if not self.allow(now):
                # not marked probed, so it is tried again next scan if still above the
                # threshold; the first refusal per episode is stored for the report
                if k not in self.noted or self.noted[k] != ep_id:
                    self.noted[k] = ep_id
                    self._note_skip(now, block, r)
                continue
            self.probed[k] = ep_id
            try:
                pair, pools = pair_pools[r.pair]
                by_label = {p.label: p for p in pools}
                buy_pool, sell_pool = by_label[r.buy_venue], by_label[r.sell_venue]
                results = quote_sizes(self.rpc, self.cfg, pair, buy_pool, sell_pool, self.sizes,
                                      ref_prices[r.pair], basefee_wei, l1_fee_wei, ref_prices["ETH"],
                                      quote_usd.get(r.pair, 1.0))
                # ts = the episode's start, so the probe joins its episode even when it ran
                # at a later crossing; probe_block says when it actually ran
                db.insert_probes(self.conn, ep_id if ep_id is not None else now, block, r.key, results)
                self.log(probe_line(results))
                done[results[0].path] = results
            except Exception as e:      # a failed probe is skipped, never fatal
                self.log(f"probe failed for {r.pair} {r.buy_venue}>{r.sell_venue} "
                         f"({type(e).__name__}: {e})")
        self.pending.clear()
        return done

    def _note_skip(self, now, block, r):
        try:
            db.insert_probe_skip(self.conn, now, block, "dex_arb_probe",
                                 f"{r.pair} {r.buy_venue}>{r.sell_venue}", r.size_usd, r.net_pct)
        except Exception as e:          # bookkeeping only, never fatal
            self.log(f"probe skip not stored ({type(e).__name__}: {e})")
