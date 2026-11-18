"""Triangle size probes: could a triangle gap absorb more than $100?

Every scan prices the two triangle loops at $100 (triangle.py). When a loop OPENS,
meaning its net edge reaches TRI_PROBE_MIN_NET_PCT after being below it on the
previous scan, the same three pools are quoted again at each of PROBE_SIZES_USD,
in ONE extra eth_call.

How one size S is priced (same method as the main scan and probe.py):
    leg 1  quoter, "S USDC in -> how much token 1 out"              (exact)
    leg 2  quoter, "x1 token 1 in -> token 2 out", x1 = S / CEX price of token 1,
           scaled to the amount leg 1 produced
    leg 3  quoter, "x2 token 2 in -> USDC out",    x2 = S / CEX price of token 2,
           scaled to the amount leg 2 produced
Each leg is quoted at about S, so slippage at that size is included. The scaling
only covers the small difference between x and the real amount (a fraction of a
percent), so it is accurate.
    raw_edge = usd_back / S - 1                (pool fees and slippage in, no gas)
    net_edge = (usd_back - gas, 3 legs) / S - 1
A size any leg can't fill (quoter reverts or returns 0) is stored with NULL
edges: "can't fill" is part of the answer.

The probe uses the pools the loop just used at $100. It answers "how much could
THIS gap absorb", not "what is the best loop at $5,000".

Rows go in tri_probes, one per size, with the shared columns first
(ts, strategy, venue, path, size_usd, raw_edge, net_edge). path is the loop plus
its three pools, e.g. "USDC>WETH>cbBTC>USDC pcsv3-100/aero3-ts1/pcsv3-100".
Probes share probe.py's hourly cap (PROBE_MAX_PER_HOUR). A probe that fails is
logged and skipped; it never stops the scanner.
"""
from __future__ import annotations

from dataclasses import dataclass

import abi
import db
import dex

STRATEGY = "tri_arb_probe"


@dataclass
class TriProbeResult:
    loop: str            # e.g. "USDC>WETH>cbBTC>USDC"
    legs: tuple          # pool label per leg
    size_usd: float
    usd_back: float | None   # None = a leg couldn't fill
    gas_usd: float
    trigger_net: float   # the $100 net % from the scan that opened it

    @property
    def path(self) -> str:
        return f"{self.loop} {'/'.join(self.legs)}"

    @property
    def filled(self) -> bool:
        return self.usd_back is not None

    @property
    def raw_edge(self):
        return (self.usd_back / self.size_usd - 1) * 100 if self.filled else None

    @property
    def net_edge(self):
        return ((self.usd_back - self.gas_usd) / self.size_usd - 1) * 100 if self.filled else None


def _quote(ok_ret):
    ok, ret = ok_ret
    if not ok or len(ret) < 128:
        return None, None
    return abi.decode_quote(ret)


def build_calls(cfg, tok1, tok2, pools, sizes, ref1, ref2):
    """Three quoter calls per size. tok1/tok2: token dicts in loop order.
    pools: (leg1, leg2, leg3) Pool objects. Returns (calls, [(x1_raw, x2_raw)])."""
    usdc = cfg.USDC
    p1, p2, p3 = pools
    calls, xs = [], []
    for size in sizes:
        usdc_in = int(size * 10 ** usdc["decimals"])
        x1 = int(size / ref1 * 10 ** tok1["decimals"])
        x2 = int(size / ref2 * 10 ** tok2["decimals"])
        xs.append((x1, x2))
        calls.append((p1.quoter, abi.encode_quote(p1.kind, usdc["address"], tok1["address"], usdc_in, p1.tier)))
        calls.append((p2.quoter, abi.encode_quote(p2.kind, tok1["address"], tok2["address"], x1, p2.tier)))
        calls.append((p3.quoter, abi.encode_quote(p3.kind, tok2["address"], usdc["address"], x2, p3.tier)))
    return calls, xs


def quote_sizes(rpc, cfg, loop, tok1, tok2, pools, sizes, ref1, ref2,
                basefee_wei, l1_fee_wei, eth_usd, trigger_net):
    """Price the loop at every size in one eth_call. Returns [TriProbeResult]."""
    calls, xs = build_calls(cfg, tok1, tok2, pools, sizes, ref1, ref2)
    results = dex.multicall(rpc, cfg.MULTICALL3, calls)

    def gas(units):
        if not units:
            return 0.0
        return dex.leg_gas_usd(units, basefee_wei, cfg.GAS_PRIORITY_WEI, cfg.SWAP_GAS_OVERHEAD,
                               l1_fee_wei, cfg.FALLBACK_L1_FEE_USD, eth_usd)

    labels = tuple(p.label for p in pools)
    out = []
    for i, size in enumerate(sizes):
        (o1, g1), (o2, g2), (o3, g3) = (_quote(results[3 * i + k]) for k in range(3))
        x1, x2 = xs[i]
        usd_back = None
        if o1 and o2 and o3 and x1 and x2:
            got1 = o1                       # raw token 1 from leg 1 (exact)
            got2 = o2 * (got1 / x1)         # raw token 2, scaled to what leg 1 produced
            usd_back = o3 * (got2 / x2) / 10 ** cfg.USDC["decimals"]
        out.append(TriProbeResult(loop, labels, size, usd_back, gas(g1) + gas(g2) + gas(g3), trigger_net))
    return out


def fmt_size(size):
    return f"${size:,.0f}"


def probe_line(results):
    """'PROBE TRI USDC>WETH>cbBTC>USDC a/b/c: $100 +0.031% | ... | $5,000 can't fill'."""
    parts = [f"{fmt_size(r.size_usd)} " + (f"{r.net_edge:+.3f}%" if r.filled else "can't fill")
             for r in results]
    return f"PROBE TRI {results[0].path}: " + " | ".join(parts)


class TriProber:
    """Watches each loop's $100 net edge and probes it when it crosses the threshold.

    allow(now) -> bool is the shared hourly cap (probe.Prober.allow). Never raises."""

    def __init__(self, cfg, rpc, conn, log, allow):
        self.cfg = cfg
        self.rpc = rpc
        self.conn = conn
        self.log = log
        self.allow = allow
        self.sizes = list(getattr(cfg, "PROBE_SIZES_USD", []))
        self.min_pct = getattr(cfg, "TRI_PROBE_MIN_NET_PCT", None)
        self.prev = {}           # loop path -> net % on the previous scan
        self.probes = 0
        self.failures = 0

    @property
    def enabled(self) -> bool:
        return self.min_pct is not None and bool(self.sizes)

    def openings(self, tri_results):
        """Loops at or above the threshold now that were below it (or unseen) last scan."""
        if not self.enabled:
            return []
        out = []
        for r in tri_results:
            before = self.prev.get(r.path)
            if r.net_edge >= self.min_pct and (before is None or before < self.min_pct):
                out.append(r)
        # a loop missing this scan (a leg had no quote) counts as below the threshold
        self.prev = {r.path: r.net_edge for r in tri_results}
        return out

    def probe(self, now, block, opening, cross, pair_pools, ref_prices, basefee_wei, l1_fee_wei):
        """opening: TriResults from openings(). cross: scanner.cross. Returns [[TriProbeResult]]."""
        done = []
        a, b, cross_pools = cross
        sym_a = a["base"]["symbol"]
        for r in opening:
            if not self.allow(now):
                continue
            try:
                first, second = (a, b) if r.path.startswith(f"USDC>{sym_a}>") else (b, a)
                p1 = {p.label: p for p in pair_pools[first["name"]][1]}[r.legs[0]]
                p2 = {p.label: p for p in cross_pools}[r.legs[1]]
                p3 = {p.label: p for p in pair_pools[second["name"]][1]}[r.legs[2]]
                results = quote_sizes(self.rpc, self.cfg, r.path, first["base"], second["base"],
                                      (p1, p2, p3), self.sizes,
                                      ref_prices[first["name"]], ref_prices[second["name"]],
                                      basefee_wei, l1_fee_wei, ref_prices["ETH"], r.net_edge)
                db.insert_tri_probes(self.conn, now, block, results)
                self.log(probe_line(results))
                self.probes += 1
                done.append(results)
            except Exception as e:      # a failed probe is skipped, never fatal
                self.failures += 1
                self.log(f"tri probe failed for {r.path} ({type(e).__name__}: {e})")
        return done
