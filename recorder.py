"""Pegged-pair recorder: once a minute, log what each pegged pair trades at on every pool.

This is data for the stat-arb project. A pegged pair (cbBTC/WBTC, USDT/USDC, ...)
is two tokens meant to be worth the same, so their price ratio hovers near 1.0.
How far it wanders, for how long, and how fast it comes back is what stat arb trades.

For a pair A/B, price means "B per 1 A":
    bid = what you get selling A:  B_out / A_in    (selling $SIZE worth of A)
    ask = what you pay buying A:   B_in / A_out    (spending $SIZE worth of B)
Both include the pool fee, so on any one pool ask > bid.

Across pools, each minute:
    best bid = highest bid, best ask = lowest ask
    mid      = (best bid + best ask) / 2       <- the series stat arb studies
    gap      = best bid / best ask - 1         <- > 0 means buy A on one pool and sell
                                                  it on another nets a profit before gas

One row per pool per minute goes in the pegged_quotes table.

Pools come from two places:
    config.DEXES                 v3-style pools (QuoterV2), shared with the main scan
    config.RECORD_CLASSIC_DEXES  Aerodrome classic (v2-style) stable/volatile pools,
                                 recorder-only, quoted with the pool's own getAmountOut
Classic pools were added Nov 14, 2026: earlier minutes don't have them.

Depth: each pool is also quoted at RECORD_DEPTH_USD (e.g. $1,000) in the same call.
    round trip at a size = ask / bid - 1     (fees both ways + price impact)
A deep pool's $1,000 round trip is about its $100 round trip; a thin pool's is much
worse, or it can't fill at all. The best bid/ask (and so mid and gap) still use $100.

Read-only and best-effort: a token that fails its check disables only that pair,
and a failed minute is logged and skipped. The scanner keeps going either way.
Resolution: amounts are whole token units, so at $100 an 8-decimal BTC token is
about 166,000 sats and prices are good to roughly 0.1 bp.
"""
from __future__ import annotations

from dataclasses import dataclass

import abi
import db
import dex


@dataclass
class PegQuote:
    pair: str
    pool: str
    bid: float | None
    ask: float | None
    depth_bid: float | None = None   # same pool at RECORD_DEPTH_USD (None = off, or can't fill)
    depth_ask: float | None = None


@dataclass
class PegSummary:
    pair: str
    best_bid: float | None
    bid_pool: str | None
    best_ask: float | None
    ask_pool: str | None

    @property
    def mid(self):
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def mid_bp(self):
        """How far mid is from 1.0, in basis points (1 bp = 0.01%)."""
        return None if self.mid is None else (self.mid - 1.0) * 1e4

    @property
    def gap_bp(self):
        """Buy A at the best ask, sell at the best bid: gross result in bp, before gas."""
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid / self.best_ask - 1.0) * 1e4


def usd_per_unit(pair, ref_prices):
    """USD value of one token of the pair. Raises KeyError if the reference price is missing."""
    ref = pair["usd_ref"]
    return 1.0 if ref is None else ref_prices[ref]


def discover_classic_pools(rpc, multicall_addr, classic_dexes, token_a, token_b):
    """Aerodrome classic: ask each factory for the stable and/or volatile pool.
    Missing pools (zero address) are skipped. The pool itself is the quote target."""
    wanted = [(d, stable) for d in classic_dexes for stable in d["stable"]]
    calls = [(d["factory"], abi.encode_get_pool_classic(token_a, token_b, stable)) for d, stable in wanted]
    pools = []
    for (d, stable), (ok, ret) in zip(wanted, dex.multicall(rpc, multicall_addr, calls)):
        if not ok or len(ret) < 32:
            continue
        addr = abi.decode_address(ret)
        if addr != abi.ZERO_ADDRESS:
            pools.append(dex.Pool(d["name"], "classic", 1 if stable else 0, addr, addr))
    return pools


def _quote_call(p, token_in, token_out, amount):
    """(target, calldata) for one quote: QuoterV2 for v3-style pools, getAmountOut for classic."""
    if p.kind == "classic":
        return p.address, abi.encode_get_amount_out(amount, token_in)
    return p.quoter, abi.encode_quote(p.kind, token_in, token_out, amount, p.tier)


def _amount_out(result, kind):
    """Amount out in raw units, or None if the call failed or returned 0."""
    ok, ret = result
    if not ok:
        return None
    if kind == "classic":
        if len(ret) < 32:
            return None
        return abi.decode_uint(ret) or None
    if len(ret) < 128:
        return None
    out, _gas = abi.decode_quote(ret)
    return out or None


def _price(pair, x_a, y_b, b_out, a_out):
    """(bid, ask) in B per 1 A from raw amounts; None where a quote is missing."""
    a_dec, b_dec = pair["a"]["decimals"], pair["b"]["decimals"]
    bid = (b_out / 10 ** b_dec) / (x_a / 10 ** a_dec) if b_out else None
    ask = (y_b / 10 ** b_dec) / (a_out / 10 ** a_dec) if a_out else None
    return bid, ask


def quote_pegged(rpc, cfg, pairs, ref_prices, size_usd, depth_usd=None):
    """pairs: [(pair cfg, [Pool])]. ONE eth_call for everything. Returns (block, [PegQuote]).

    Each pool is quoted both ways at size_usd and, if depth_usd is set, both ways again
    at depth_usd (4 quotes per pool, still one eth_call). Pools that can't quote either
    direction at size_usd are left out.
    """
    sizes = [size_usd] + ([depth_usd] if depth_usd else [])
    calls = [(cfg.MULTICALL3, abi.encode_no_args(abi.SIG_BLOCK_NUMBER))]
    layout = []   # (pair, pool, [(x_a, y_b, sell index, buy index) per size])
    for pair, pools in pairs:
        a, b = pair["a"], pair["b"]
        unit_usd = usd_per_unit(pair, ref_prices)
        for p in pools:
            per_size = []
            for usd in sizes:
                x_a = int(usd / unit_usd * 10 ** a["decimals"])   # A to sell
                y_b = int(usd / unit_usd * 10 ** b["decimals"])   # B to spend
                calls.append(_quote_call(p, a["address"], b["address"], x_a))
                calls.append(_quote_call(p, b["address"], a["address"], y_b))
                per_size.append((x_a, y_b, len(calls) - 2, len(calls) - 1))
            layout.append((pair, p, per_size))

    results = dex.multicall(rpc, cfg.MULTICALL3, calls)
    ok_b, ret_b = results[0]
    if not ok_b:
        raise RuntimeError("multicall could not read block number")
    block = abi.decode_uint(ret_b)

    quotes = []
    for pair, p, per_size in layout:
        prices = [_price(pair, x_a, y_b, _amount_out(results[i_sell], p.kind),
                         _amount_out(results[i_buy], p.kind))
                  for x_a, y_b, i_sell, i_buy in per_size]
        bid, ask = prices[0]
        if bid is None and ask is None:
            continue
        depth_bid, depth_ask = prices[1] if len(prices) > 1 else (None, None)
        quotes.append(PegQuote(pair["name"], p.label, bid, ask, depth_bid, depth_ask))
    return block, quotes


def summarize(quotes):
    """Best bid and best ask across pools, per pair, in first-seen pair order."""
    out = {}
    for q in quotes:
        s = out.setdefault(q.pair, PegSummary(q.pair, None, None, None, None))
        if q.bid is not None and (s.best_bid is None or q.bid > s.best_bid):
            s.best_bid, s.bid_pool = q.bid, q.pool
        if q.ask is not None and (s.best_ask is None or q.ask < s.best_ask):
            s.best_ask, s.ask_pool = q.ask, q.pool
    return list(out.values())


def summary_line(quotes):
    """One log line per minute, e.g.  PEG cbBTC/WBTC mid +0.5bp gap +3.0bp (buy univ3-500 > sell aero1-ts1)"""
    parts = []
    for s in summarize(quotes):
        if s.mid is None:
            parts.append(f"{s.pair} one-sided")
            continue
        part = f"{s.pair} mid {s.mid_bp:+.1f}bp gap {s.gap_bp:+.1f}bp"
        if s.gap_bp > 0:
            part += f" (buy {s.ask_pool} > sell {s.bid_pool})"
        parts.append(part)
    return "PEG " + " | ".join(parts)


def round_trip_bp(bid, ask):
    """Cost of buying then selling on one pool, in bp (fees both ways + price impact)."""
    return (ask / bid - 1) * 1e4 if bid and ask else None


def print_table(quotes, depth_usd=None):
    """Every pool's quote (used by scanner.py --once for a sanity check).
    'rt' = round trip on that pool in bp; a thin pool's bigger-size round trip blows up."""
    big = f"rt bp ${depth_usd:,.0f}" if depth_usd else "rt bp big"
    print(f"{'pegged pair':<14}{'pool':<16}{'bid':>12}{'ask':>12}{'rt bp $100':>12}{big:>14}")
    for q in quotes:
        bid = f"{q.bid:.6f}" if q.bid is not None else "-"
        ask = f"{q.ask:.6f}" if q.ask is not None else "-"
        rt = round_trip_bp(q.bid, q.ask)
        rt_big = round_trip_bp(q.depth_bid, q.depth_ask)
        print(f"{q.pair:<14}{q.pool:<16}{bid:>12}{ask:>12}"
              f"{f'{rt:.1f}' if rt is not None else '-':>12}"
              f"{f'{rt_big:.1f}' if rt_big is not None else '-':>14}")


class Recorder:
    def __init__(self, cfg, rpc, conn, log):
        self.cfg = cfg
        self.rpc = rpc
        self.conn = conn
        self.log = log
        self.pairs = []          # [(pair cfg, [Pool])] that passed the checks
        self.last_minute = None
        self.last_quotes = []
        self.failures = 0
        self.depth_usd = getattr(cfg, "RECORD_DEPTH_USD", None)

    def startup(self):
        cfg = self.cfg
        for pair in getattr(cfg, "RECORD_PAIRS", []):
            # Any failure here skips this pair only: the recorder must never stop the scanner.
            try:
                problems = dex.verify_tokens(self.rpc, cfg.MULTICALL3, [pair["a"], pair["b"]])
                if problems:
                    self.log(f"recorder: skipping {pair['name']}: " + "; ".join(problems))
                    continue
                pools = dex.discover_pools(self.rpc, cfg.MULTICALL3, cfg.DEXES,
                                           pair["a"]["address"], pair["b"]["address"])
                classic = getattr(cfg, "RECORD_CLASSIC_DEXES", [])
                if classic:
                    pools += discover_classic_pools(self.rpc, cfg.MULTICALL3, classic,
                                                    pair["a"]["address"], pair["b"]["address"])
            except Exception as e:
                self.log(f"recorder: skipping {pair['name']} ({type(e).__name__}: {e}); "
                         "restart the scanner to retry it")
                continue
            if not pools:
                self.log(f"recorder: skipping {pair['name']}: no pools found")
                continue
            self.pairs.append((pair, pools))
            self.log(f"recorder {pair['name']} pools: " + ", ".join(p.label for p in pools))

    def due(self, now):
        return bool(self.pairs) and int(now // self.cfg.RECORD_SECONDS) != self.last_minute

    def record(self, now, ref_prices):
        """Quote every pair once and store the rows. Never raises: a failure skips this minute."""
        self.last_minute = int(now // self.cfg.RECORD_SECONDS)   # set first: a failure waits a minute
        try:
            block, quotes = quote_pegged(self.rpc, self.cfg, self.pairs, ref_prices,
                                         self.cfg.RECORD_SIZE_USD, self.depth_usd)
        except Exception as e:      # RPC error, missing reference price, bad reply...
            self.failures += 1
            self.log(f"recorder: minute skipped ({type(e).__name__}: {e})")
            return []
        db.insert_pegged(self.conn, now, block, quotes, self.depth_usd)
        self.last_quotes = quotes
        return quotes
