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

Coinbase (Nov 25, 2026): for the pairs in config.RECORD_CEX, the same minute also logs
Coinbase's best bid/ask (table cex_quotes). Coinbase charges 0% maker on these limit-only
books, so "Coinbase vs Base" is a stat-arb question. raw_edge = Coinbase mid / that minute's
best Base mid - 1, in %. The Coinbase requests run in parallel with the Base eth_call; one
failing or slow (over RECORD_CEX_TIMEOUT_S) is logged and skipped, and never stops the
Base rows (or the scanner).

Read-only and best-effort: a token that fails its check disables only that pair,
and a failed minute is logged and skipped. The scanner keeps going either way.
Resolution: amounts are whole token units, so at $100 an 8-decimal BTC token is
about 166,000 sats and prices are good to roughly 0.1 bp.
"""
from __future__ import annotations

import time
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
class CexQuote:
    ts: float
    venue: str
    pair: str            # the RECORD_PAIRS name it matches, e.g. "cbETH/WETH"
    product: str         # the exchange's name, e.g. "CBETH-ETH"
    bid: float | None
    ask: float | None
    bid_size: float | None
    ask_size: float | None
    fetch_s: float       # how long the request took
    block: int | None    # that minute's Base block (None if the Base quotes failed)
    raw_edge: float | None   # % : CEX mid / best Base mid - 1 (None without both mids)

    @property
    def mid(self):
        return None if self.bid is None or self.ask is None else (self.bid + self.ask) / 2


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
    """(target, calldata) for one quote on any pool kind (uni, slip, aero, classic)."""
    return dex.quote_call(p, token_in, token_out, amount)


def _amount_out(result, kind):
    """Amount out in raw units, or None if the call failed or returned 0 (any pool kind)."""
    out, _gas = dex.read_quote(kind, result)
    return out


def _price(pair, x_a, y_b, b_out, a_out):
    """(bid, ask) in B per 1 A from raw amounts; None where a quote is missing."""
    a_dec, b_dec = pair["a"]["decimals"], pair["b"]["decimals"]
    bid = (b_out / 10 ** b_dec) / (x_a / 10 ** a_dec) if b_out else None
    ask = (y_b / 10 ** b_dec) / (a_out / 10 ** a_dec) if a_out else None
    return bid, ask


def _quote_one_pair(rpc, cfg, pair, pools, ref_prices, sizes):
    """ONE eth_call for one pair: (block, [PegQuote]). Raises on an RPC error."""
    a, b = pair["a"], pair["b"]
    unit_usd = usd_per_unit(pair, ref_prices)
    calls = [(cfg.MULTICALL3, abi.encode_no_args(abi.SIG_BLOCK_NUMBER))]
    layout = []   # (pool, [(x_a, y_b, sell index, buy index) per size])
    for p in pools:
        per_size = []
        for usd in sizes:
            x_a = int(usd / unit_usd * 10 ** a["decimals"])   # A to sell
            y_b = int(usd / unit_usd * 10 ** b["decimals"])   # B to spend
            calls.append(_quote_call(p, a["address"], b["address"], x_a))
            calls.append(_quote_call(p, b["address"], a["address"], y_b))
            per_size.append((x_a, y_b, len(calls) - 2, len(calls) - 1))
        layout.append((p, per_size))

    results = dex.multicall(rpc, cfg.MULTICALL3, calls)
    ok_b, ret_b = results[0]
    if not ok_b:
        raise RuntimeError("multicall could not read block number")
    quotes = []
    for p, per_size in layout:
        prices = [_price(pair, x_a, y_b, _amount_out(results[i_sell], p.kind),
                         _amount_out(results[i_buy], p.kind))
                  for x_a, y_b, i_sell, i_buy in per_size]
        bid, ask = prices[0]
        if bid is None and ask is None:
            continue
        depth_bid, depth_ask = prices[1] if len(prices) > 1 else (None, None)
        quotes.append(PegQuote(pair["name"], p.label, bid, ask, depth_bid, depth_ask))
    return abi.decode_uint(ret_b), quotes


def is_gas_error(err):
    """True for the node refusing a call as too heavy ("out of gas: gas required exceeds ...")."""
    text = str(err).lower()
    return "out of gas" in text or "gas required exceeds" in text


def quote_pegged(rpc, cfg, pairs, ref_prices, size_usd, depth_usd=None, executor=None):
    """pairs: [(pair cfg, [Pool])], one entry per eth_call (a pair can appear more than once,
    each time with a different group of its pools). Returns (block, [PegQuote], failed) with
    failed = [(index into pairs, pair name, exception)]. Never raises.

    Why not one call for everything (Nov 25, 2026): it passed the node's gas cap ("out of gas:
    gas required exceeds: 100000000"), because quotes into near-empty pools burn a lot of gas
    walking empty price ranges. Recorder.record splits heavy groups further as needed.
    With an executor the calls run in parallel, so the minute takes no longer.
    block = the lowest block among the calls (they're a few blocks apart at most), or None.
    Each pool is quoted both ways at size_usd and, if depth_usd is set, both ways again
    at depth_usd (4 quotes per pool). Pools that can't quote either direction at size_usd
    are left out.
    """
    sizes = [size_usd] + ([depth_usd] if depth_usd else [])
    if executor is not None:
        futures = [(pair, executor.submit(_quote_one_pair, rpc, cfg, pair, pools, ref_prices, sizes))
                   for pair, pools in pairs]
        outcomes = []
        for pair, fut in futures:
            try:
                outcomes.append((pair, fut.result(), None))
            except Exception as e:
                outcomes.append((pair, None, e))
    else:
        outcomes = []
        for pair, pools in pairs:
            try:
                outcomes.append((pair, _quote_one_pair(rpc, cfg, pair, pools, ref_prices, sizes), None))
            except Exception as e:
                outcomes.append((pair, None, e))

    blocks, quotes, failed = [], [], []
    for i, (pair, result, err) in enumerate(outcomes):    # call order is kept
        if err is not None:
            failed.append((i, pair["name"], err))
            continue
        blocks.append(result[0])
        quotes.extend(result[1])
    return (min(blocks) if blocks else None), quotes, failed


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


DEAD_RT_BP = 100_000   # a round trip this big means the pool is empty, not just thin


def fmt_px(px):
    """Price for the table: 6 decimals, or compact (2.761e+06) when absurdly large. '-' if none."""
    if px is None:
        return "-"
    return f"{px:.6f}" if px < 1000 else f"{px:.3e}"


def fmt_rt(bp):
    """Round trip for the table: '-' if missing, 'dead' at DEAD_RT_BP or more."""
    if bp is None:
        return "-"
    return "dead" if bp >= DEAD_RT_BP else f"{bp:,.1f}"


def print_table(quotes, depth_usd=None):
    """Every pool's quote (used by scanner.py --once for a sanity check).
    'rt' = round trip on that pool in bp; a thin pool's bigger-size round trip blows up.
    Columns are separated by a space, so a missing bid ('-') can't run into a huge ask."""
    big = f"rt bp ${depth_usd:,.0f}" if depth_usd else "rt bp big"
    print(f"{'pegged pair':<14} {'pool':<16} {'bid':>12} {'ask':>12} {'rt bp $100':>11} {big:>13}")
    for q in quotes:
        rt = round_trip_bp(q.bid, q.ask)
        rt_big = round_trip_bp(q.depth_bid, q.depth_ask)
        print(f"{q.pair:<14} {q.pool:<16} {fmt_px(q.bid):>12} {fmt_px(q.ask):>12}"
              f" {fmt_rt(rt):>11} {fmt_rt(rt_big):>13}")


def print_cex_table(rows):
    """Coinbase rows for scanner.py --once: book, round trip in bp, and edge vs Base in bp."""
    if not rows:
        return
    print(f"{'cex pair':<14} {'venue/product':<20} {'bid':>12} {'ask':>12} {'rt bp':>8} {'vs base bp':>11}")
    for r in rows:
        rt = round_trip_bp(r.bid, r.ask)
        edge = f"{r.raw_edge * 100:+.1f}" if r.raw_edge is not None else "-"
        print(f"{r.pair:<14} {r.venue + ' ' + r.product:<20} {fmt_px(r.bid):>12} {fmt_px(r.ask):>12}"
              f" {fmt_rt(rt):>8} {edge:>11}")


class Recorder:
    def __init__(self, cfg, rpc, conn, log, cex_fetchers=None, executor=None):
        self.cfg = cfg
        self.rpc = rpc
        self.conn = conn
        self.log = log
        self.pairs = []          # [(pair cfg, [Pool])] that passed the checks
        self.last_minute = None
        self.last_quotes = []
        self.failures = 0
        self.depth_usd = getattr(cfg, "RECORD_DEPTH_USD", None)
        self.cex_fetchers = cex_fetchers or {}   # venue -> fetch(product) -> cex.Book
        self.executor = executor                 # runs CEX requests alongside the eth_call
        self.last_cex = []
        self.cex_failures = 0
        self.pair_failures = 0                   # one pair's call failed, the others recorded
        # Call groups, learned as we go: pair name -> [[Pool, ...], ...], one eth_call per
        # group. Starts as one group per pair; a group the node refuses as too heavy is
        # split in half (and remembered). A single pool that's too heavy even alone is
        # parked: pool key -> (pair name, Pool, minute to try it again).
        self.groups = {}
        self.parked = {}

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
            self.groups[pair["name"]] = [list(pools)]
            self.log(f"recorder {pair['name']} pools: " + ", ".join(p.label for p in pools))

    def due(self, now):
        return bool(self.pairs) and int(now // self.cfg.RECORD_SECONDS) != self.last_minute

    def record(self, now, ref_prices):
        """Quote every pair once and store the rows. Never raises: a failure skips this minute
        (Base and CEX separately: a Coinbase problem never costs the Base rows, or vice versa)."""
        self.last_minute = int(now // self.cfg.RECORD_SECONDS)   # set first: a failure waits a minute
        jobs = self._start_cex()
        block, quotes = None, []
        try:
            block, quotes, errors = self._quote_groups(ref_prices)
        except Exception as e:      # a bug in the grouping itself: never stop the scanner
            errors = [("recorder", e)]
        if quotes:
            for name, e in errors:  # some pairs failed: the rest are still recorded
                self.pair_failures += 1
                self.log(f"recorder: {name} skipped this minute ({type(e).__name__}: {e})")
            db.insert_pegged(self.conn, now, block, quotes, self.depth_usd)
            self.last_quotes = quotes
        elif errors:                # every pair failed: RPC down, missing prices, bad replies...
            self.failures += 1
            e = errors[0][1]
            self.log(f"recorder: minute skipped ({type(e).__name__}: {e})")
        rows = self._finish_cex(jobs, now, block, quotes)
        if rows:
            db.insert_cex_quotes(self.conn, rows)
            self.last_cex = rows
        return quotes

    # ---- call groups: stay under the node's gas cap -----------------------------------

    def _quote_groups(self, ref_prices):
        """Quote every group; split groups the node refuses as too heavy and retry them in the
        same minute. Returns (block, quotes, [(pair name, error)] for non-gas failures)."""
        minute = self.last_minute
        recheck = getattr(self.cfg, "RECORD_RECHECK_MIN", 60)
        for key, (name, pool, until) in list(self.parked.items()):   # parked pools come back
            if minute >= until:
                self.groups.setdefault(name, []).append([pool])
                del self.parked[key]
        by_name = {p["name"]: p for p, _ in self.pairs}
        units = [(by_name[n], g) for n in by_name for g in self.groups.get(n, []) if g]
        blocks, quotes, errors, gas = [], [], [], None
        for _ in range(16):                       # pools per group at least halve each pass
            if not units:
                break
            block, got, failed = quote_pegged(self.rpc, self.cfg, units, ref_prices,
                                              self.cfg.RECORD_SIZE_USD, self.depth_usd,
                                              self.executor)
            if block is not None:
                blocks.append(block)
            quotes.extend(got)
            retry = []
            for i, name, err in failed:
                pair, group = units[i]
                if not is_gas_error(err):
                    if name not in [n for n, _ in errors]:
                        errors.append((name, err))
                    continue
                gas = gas or (name, err)
                groups = self.groups[name]
                at = next(k for k, g in enumerate(groups) if g is group)
                if len(group) > 1:
                    half = len(group) // 2
                    a, b = group[:half], group[half:]
                    groups[at:at + 1] = [a, b]
                    retry += [(pair, a), (pair, b)]
                    self.log(f"recorder: {name} too heavy for one call: now {len(groups)} calls")
                else:
                    (pool,) = group
                    del groups[at]
                    self.parked[(name, pool.label)] = (name, pool, minute + recheck)
                    self.log(f"recorder: {name} {pool.label} too heavy even alone: "
                             f"skipped for {recheck} min")
            units = retry
        if not quotes and not errors and gas:     # everything too heavy: the minute is skipped
            errors.append(gas)
        quotes.sort(key=lambda q: list(by_name).index(q.pair))   # pair order, like before
        return (min(blocks) if blocks else None), quotes, errors

    # ---- Coinbase (or any CEX) side ---------------------------------------------------

    def _cex_items(self):
        """RECORD_CEX entries we have a fetcher for, in config order."""
        return [c for c in getattr(self.cfg, "RECORD_CEX", []) if c["venue"] in self.cex_fetchers]

    def _fetch_one(self, item):
        start = time.perf_counter()
        book = self.cex_fetchers[item["venue"]](item["product"])
        return book, time.perf_counter() - start

    def _start_cex(self):
        """Start the CEX requests (in the executor if there is one) before the eth_call."""
        jobs = []
        for item in self._cex_items():
            if self.executor is not None:
                jobs.append((item, self.executor.submit(self._fetch_one, item)))
            else:
                jobs.append((item, None))           # fetched in _finish_cex instead
        return jobs

    def _finish_cex(self, jobs, now, block, quotes):
        """Collect CEX books into CexQuote rows. A failed or slow request is logged and skipped."""
        mids = {s.pair: s.mid for s in summarize(quotes)}
        timeout = getattr(self.cfg, "RECORD_CEX_TIMEOUT_S", 4.0)
        rows = []
        for item, future in jobs:
            try:
                book, secs = future.result(timeout=timeout) if future else self._fetch_one(item)
            except Exception as e:       # timeout, HTTP error, bad JSON, unknown product...
                self.cex_failures += 1
                self.log(f"recorder: {item['venue']} {item['product']} skipped "
                         f"({type(e).__name__}: {e})")
                continue
            bid, bid_size = book.bids[0] if book.bids else (None, None)
            ask, ask_size = book.asks[0] if book.asks else (None, None)
            base_mid = mids.get(item["pair"])
            edge = None
            if bid is not None and ask is not None and base_mid:
                edge = ((bid + ask) / 2 / base_mid - 1) * 100
            rows.append(CexQuote(now, item["venue"], item["pair"], item["product"], bid, ask,
                                 bid_size, ask_size, secs, block, edge))
        return rows
