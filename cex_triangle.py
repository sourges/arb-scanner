"""Triangular arb on Binance.US (all three legs on one exchange). READ-ONLY: it only measures.

For each quote currency Q in BUS_TRIANGLES (USD, USDT, USDC), two loops:
    forward:  Q -> BTC -> ETH -> Q     buy BTC on BTCQ, buy ETH on ETHBTC, sell ETH on ETHQ
    reverse:  Q -> ETH -> BTC -> Q     buy ETH on ETHQ, sell ETH on ETHBTC, sell BTC on BTCQ
All loops share the ETHBTC book.

How a loop is priced: public order books (top BUS_BOOK_LEVELS levels), walked for a real
fill of BUS_SIZE_USD, buying at the asks and selling at the bids. Each leg then pays the
taker fee (BUS_TAKER_FEES, per symbol) on what it receives.
    raw_edge = Q back before fees / size - 1      (spreads and book depth in, no fees)
    net_edge = Q back after fees  / size - 1
There is no gas and nothing moves between venues. Not modeled: Binance's lot-size rounding
(a fraction of a cent at $100) and the few hundred ms between the three book fetches;
book_age (seconds between the first and last of the three book fetches) shows how far
apart they were.

Rate limits: Binance.US allows 1200 request weight a minute per IP, and one book
(limit <= 100) costs 5. 7 books every BUS_SECONDS = 4 is about 525 a minute. The
collector reads Binance's X-MBX-USED-WEIGHT-1M header and skips rounds above
BUS_MAX_WEIGHT, and on a 429 (slow down) or 418 (IP ban) it pauses for the
Retry-After time (at least BUS_BACKOFF_S). Your own Binance.US trading from the same
IP counts against the same limit.

Rows go in cex_tri_quotes, one per loop per round, shared columns first
(ts, strategy='tri_arb', venue='binanceus', path, size_usd, raw_edge, net_edge).
A failure is logged and skipped; it never stops the Base scanner.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests

import cex
import db

DEPTH_URL = "https://api.binance.us/api/v3/depth"
VENUE = "binanceus"
STRATEGY = "tri_arb"


class RateLimited(Exception):
    def __init__(self, status, retry_after):
        super().__init__(f"HTTP {status}, retry after {retry_after}s")
        self.status = status
        self.retry_after = retry_after


def parse_depth(payload: dict) -> cex.Book:
    """Binance depth: {"bids": [["price", "qty"], ...], "asks": [...]} (strings)."""
    if "bids" not in payload or "asks" not in payload:
        raise ValueError(f"binanceus: unexpected reply {str(payload)[:120]}")
    bids = [(float(p), float(q)) for p, q in payload["bids"]]
    asks = [(float(p), float(q)) for p, q in payload["asks"]]
    return cex.Book(VENUE, sorted(bids, reverse=True), sorted(asks))


def fetch_depth(symbol: str, levels=20, session=None, timeout=5):
    """One order book. Returns (Book, used weight or None). Raises RateLimited on 429/418."""
    s = session or requests
    r = s.get(DEPTH_URL, params={"symbol": symbol, "limit": levels}, timeout=timeout)
    if r.status_code in (418, 429):
        raise RateLimited(r.status_code, int(r.headers.get("Retry-After", 0) or 0))
    if r.status_code == 400:
        raise ValueError(f"binanceus {symbol}: {r.text[:120]}")   # e.g. invalid symbol
    r.raise_for_status()
    used = r.headers.get("X-MBX-USED-WEIGHT-1M")
    return parse_depth(r.json()), int(used) if used else None


@dataclass
class CexTriResult:
    path: str              # e.g. "USD>BTC>ETH>USD"
    size_usd: float
    back_raw: float        # Q back before fees
    back_net: float        # Q back after the three taker fees
    fee_usd: float         # fees paid, in Q
    legs: tuple            # the three book symbols, in order
    book_age: float        # seconds between the first and last of the three books

    @property
    def raw_edge(self) -> float:
        return (self.back_raw / self.size_usd - 1) * 100

    @property
    def net_edge(self) -> float:
        return (self.back_net / self.size_usd - 1) * 100


def fee_for(fees: dict, symbol: str) -> float:
    return fees.get(symbol, fees.get("default", 0.0))


def loop_forward(q, size, btc_q, eth_btc, eth_q, fees):
    """Q -> BTC -> ETH -> Q. books: cex.Book. Returns (raw back, net back) or None if too thin."""
    f1, f2, f3 = (fee_for(fees, s) for s in (btc_q[0], eth_btc[0], eth_q[0]))
    btc = cex.walk_buy(btc_q[1].asks, size)
    if btc is None:
        return None
    eth_raw = cex.walk_buy(eth_btc[1].asks, btc)                   # spend BTC on ETH
    eth_net = cex.walk_buy(eth_btc[1].asks, btc * (1 - f1))
    if eth_raw is None or eth_net is None:
        return None
    back_raw = cex.walk_sell(eth_q[1].bids, eth_raw)
    back_net = cex.walk_sell(eth_q[1].bids, eth_net * (1 - f2))
    if back_raw is None or back_net is None:
        return None
    return back_raw, back_net * (1 - f3)


def loop_reverse(q, size, btc_q, eth_btc, eth_q, fees):
    """Q -> ETH -> BTC -> Q."""
    f1, f2, f3 = (fee_for(fees, s) for s in (eth_q[0], eth_btc[0], btc_q[0]))
    eth = cex.walk_buy(eth_q[1].asks, size)
    if eth is None:
        return None
    btc_raw = cex.walk_sell(eth_btc[1].bids, eth)                  # sell ETH for BTC
    btc_net = cex.walk_sell(eth_btc[1].bids, eth * (1 - f1))
    if btc_raw is None or btc_net is None:
        return None
    back_raw = cex.walk_sell(btc_q[1].bids, btc_raw)
    back_net = cex.walk_sell(btc_q[1].bids, btc_net * (1 - f2))
    if back_raw is None or back_net is None:
        return None
    return back_raw, back_net * (1 - f3)


def compute(size, triangles, cross_symbol, books, fees):
    """books: {symbol: Book}. triangles: [{"quote", "btc", "eth"}]. Missing book = loop skipped."""
    out = []
    cross = books.get(cross_symbol)
    for t in triangles:
        btc_q, eth_q = books.get(t["btc"]), books.get(t["eth"])
        if cross is None or btc_q is None or eth_q is None:
            continue
        fetched = [b.fetched_at for b in (btc_q, cross, eth_q)]
        age = max(fetched) - min(fetched)
        q = t["quote"]
        trio = ((t["btc"], btc_q), (cross_symbol, cross), (t["eth"], eth_q))
        fwd = loop_forward(q, size, *trio, fees)
        if fwd:
            out.append(CexTriResult(f"{q}>BTC>ETH>{q}", size, fwd[0], fwd[1], fwd[0] - fwd[1],
                                    (t["btc"], cross_symbol, t["eth"]), age))
        rev = loop_reverse(q, size, *trio, fees)
        if rev:
            out.append(CexTriResult(f"{q}>ETH>BTC>{q}", size, rev[0], rev[1], rev[0] - rev[1],
                                    (t["eth"], cross_symbol, t["btc"]), age))
    return out


def best_line(results):
    if not results:
        return "BUS -"
    r = max(results, key=lambda x: x.net_edge)
    return f"BUS best {r.path} {r.net_edge:+.3f}%"


def print_table(results):
    print(f"{'Binance.US triangle':<22}{'leg 1':<10}{'leg 2':<10}{'leg 3':<10}{'back $':>10}"
          f"{'fees $':>8}{'raw %':>9}{'net %':>9}{'gap s':>7}")
    if not results:
        print("(no Binance.US triangle this round)")
    for r in sorted(results, key=lambda x: -x.net_edge):
        print(f"{r.path:<22}{r.legs[0]:<10}{r.legs[1]:<10}{r.legs[2]:<10}{r.back_net:>10.3f}"
              f"{r.fee_usd:>8.4f}{r.raw_edge:>8.3f}%{r.net_edge:>8.3f}%{r.book_age:>7.2f}")


class CexTriCollector:
    """Fetches the books on its own schedule, prices the loops, stores the rows. Never raises.

    fetch(symbol) -> (Book, used weight or None); defaults to fetch_depth with a shared session."""

    def __init__(self, cfg, conn, log, fetch=None, clock=time.time, pool=None):
        self.cfg = cfg
        self.conn = conn
        self.log = log
        self.clock = clock
        self.every = getattr(cfg, "BUS_SECONDS", None)
        self.triangles = list(getattr(cfg, "BUS_TRIANGLES", []))
        self.cross = getattr(cfg, "BUS_CROSS", "ETHBTC")
        self.fees = dict(getattr(cfg, "BUS_TAKER_FEES", {"default": 0.0002}))
        self.size = getattr(cfg, "BUS_SIZE_USD", 100.0)
        self.max_weight = getattr(cfg, "BUS_MAX_WEIGHT", 800)
        self.backoff_s = getattr(cfg, "BUS_BACKOFF_S", 60)
        self.max_age = getattr(cfg, "BOOK_MAX_AGE_S", 6.0)
        levels = getattr(cfg, "BUS_BOOK_LEVELS", 20)
        session = requests.Session()
        self.fetch = fetch or (lambda sym: fetch_depth(sym, levels, session))
        self.pool = pool or ThreadPoolExecutor(max_workers=8)
        self.last_round = None
        self.paused_until = 0.0
        self.used_weight = None
        self.last = []
        self.failures = 0
        self.rows = 0

    @property
    def enabled(self) -> bool:
        return bool(self.every) and bool(self.triangles)

    def symbols(self):
        out = [self.cross]
        for t in self.triangles:
            out += [t["btc"], t["eth"]]
        return list(dict.fromkeys(out))

    def startup(self):
        """One round of fetches: drop triangles whose books don't exist. Never raises."""
        if not self.enabled:
            return
        books, bad = self._fetch_all()
        if self.cross in bad:
            self.log(f"binance.us triangle off: {self.cross} book failed ({bad[self.cross]})")
            self.triangles = []
            return
        keep = []
        for t in self.triangles:
            missing = [s for s in (t["btc"], t["eth"]) if s in bad]
            if missing:
                self.log(f"binance.us: skipping {t['quote']} loops: " +
                         "; ".join(f"{s} ({bad[s]})" for s in missing))
            else:
                keep.append(t)
        self.triangles = keep
        if keep:
            self.log("binance.us triangle books: " + ", ".join(self.symbols()) +
                     f" every {self.every:g}s")
        else:
            self.log("binance.us triangle off: no loop has all three books")

    def due(self, now):
        if not self.enabled or now < self.paused_until:
            return False
        return self.last_round is None or now - self.last_round >= self.every

    def _fetch_all(self):
        """{symbol: Book} plus {symbol: error text}. Raises RateLimited if any fetch was limited."""
        jobs = {s: self.pool.submit(self.fetch, s) for s in self.symbols()}
        books, bad, limited = {}, {}, None
        for sym, fut in jobs.items():
            try:
                book, used = fut.result()
                books[sym] = book
                if used is not None:
                    self.used_weight = max(used, self.used_weight or 0)
            except RateLimited as e:
                limited = e
            except Exception as e:
                bad[sym] = f"{type(e).__name__}: {e}"
        if limited:
            raise limited
        return books, bad

    def collect(self, now):
        """One round. Returns the results ([] on any failure)."""
        self.last_round = now
        self.used_weight = None
        try:
            books, bad = self._fetch_all()
        except RateLimited as e:
            wait = max(self.backoff_s, e.retry_after)
            self.paused_until = now + wait
            self.failures += 1
            self.log(f"binance.us: rate limited ({e}); pausing {wait}s")
            self.last = []
            return []
        if bad:
            self.failures += 1
            self.log("binance.us: round skipped (" + "; ".join(f"{s} {m}" for s, m in bad.items()) + ")")
            self.last = []
            return []
        fresh = {s: b for s, b in books.items() if now - b.fetched_at <= self.max_age}
        results = compute(self.size, self.triangles, self.cross, fresh, self.fees)
        if results:
            db.insert_cex_tri(self.conn, now, results, venue=VENUE)
            self.rows += len(results)
        if self.used_weight is not None and self.used_weight >= self.max_weight:
            self.paused_until = now + 2 * self.every   # skip the next round to let the weight drain
            self.log(f"binance.us: used weight {self.used_weight} >= {self.max_weight}; skipping a round")
        self.last = results
        return results
