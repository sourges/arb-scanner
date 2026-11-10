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


def _amount_out(result):
    ok, ret = result
    if not ok or len(ret) < 128:
        return None
    out, _gas = abi.decode_quote(ret)
    return out or None


def quote_pegged(rpc, cfg, pairs, ref_prices, size_usd):
    """pairs: [(pair cfg, [Pool])]. ONE eth_call for everything. Returns (block, [PegQuote]).

    Pools that can't quote either direction are left out.
    """
    calls = [(cfg.MULTICALL3, abi.encode_no_args(abi.SIG_BLOCK_NUMBER))]
    layout = []
    for pair, pools in pairs:
        a, b = pair["a"], pair["b"]
        unit_usd = usd_per_unit(pair, ref_prices)
        x_a = int(size_usd / unit_usd * 10 ** a["decimals"])   # A to sell
        y_b = int(size_usd / unit_usd * 10 ** b["decimals"])   # B to spend
        for p in pools:
            calls.append((p.quoter, abi.encode_quote(p.kind, a["address"], b["address"], x_a, p.tier)))
            calls.append((p.quoter, abi.encode_quote(p.kind, b["address"], a["address"], y_b, p.tier)))
            layout.append((pair, p, x_a, y_b))

    results = dex.multicall(rpc, cfg.MULTICALL3, calls)
    ok_b, ret_b = results[0]
    if not ok_b:
        raise RuntimeError("multicall could not read block number")
    block = abi.decode_uint(ret_b)

    quotes = []
    for i, (pair, p, x_a, y_b) in enumerate(layout):
        a_dec, b_dec = pair["a"]["decimals"], pair["b"]["decimals"]
        b_out = _amount_out(results[1 + 2 * i])   # selling A
        a_out = _amount_out(results[2 + 2 * i])   # buying A
        bid = (b_out / 10 ** b_dec) / (x_a / 10 ** a_dec) if b_out else None
        ask = (y_b / 10 ** b_dec) / (a_out / 10 ** a_dec) if a_out else None
        if bid is None and ask is None:
            continue
        quotes.append(PegQuote(pair["name"], p.label, bid, ask))
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


def print_table(quotes):
    """Every pool's quote (used by scanner.py --once for a sanity check)."""
    print(f"{'pegged pair':<14}{'pool':<16}{'bid':>12}{'ask':>12}{'pool spread bp':>16}")
    for q in quotes:
        bid = f"{q.bid:.6f}" if q.bid is not None else "-"
        ask = f"{q.ask:.6f}" if q.ask is not None else "-"
        spread = f"{(q.ask / q.bid - 1) * 1e4:.1f}" if q.bid and q.ask else "-"
        print(f"{q.pair:<14}{q.pool:<16}{bid:>12}{ask:>12}{spread:>16}")


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
                                         self.cfg.RECORD_SIZE_USD)
        except Exception as e:      # RPC error, missing reference price, bad reply...
            self.failures += 1
            self.log(f"recorder: minute skipped ({type(e).__name__}: {e})")
            return []
        db.insert_pegged(self.conn, now, block, quotes)
        self.last_quotes = quotes
        return quotes
