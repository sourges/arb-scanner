"""Base DEX / Kraken / Coinbase arbitrage scanner. READ-ONLY: it never trades.

Run:  python scanner.py          scan until Ctrl+C, logging to arb_scanner.sqlite
      python scanner.py --once   scan one block, print every route, exit
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import cex
import cex_triangle
import config
import db
import dex
import prices
import probe
import recorder
import tri_probe
import triangle
from routes import compute_routes
import rpc as rpclib
from rpc import RpcClient, RpcError, RpcPool
from tracker import EpisodeTracker, MinuteStats


def log(msg: str):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def token_problem(tok, info):
    """Why a token fails its on-chain check, or None. decimals None in config = take the chain's."""
    if info is None:
        return f"{tok['symbol']} at {tok['address']}: no response (wrong address?)"
    sym, dec = info
    if sym != tok["symbol"]:
        return f"{tok['address']}: expected {tok['symbol']}, chain says {sym}"
    if tok.get("decimals") is not None and dec != tok["decimals"]:
        return f"{tok['address']}: expected {tok['decimals']} decimals, chain says {dec}"
    return None


class Scanner:
    def __init__(self, cfg, rpc, conn, fetchers=None, clock=time.time, bus_fetch=None):
        self.cfg = cfg
        self.rpc = rpc
        self.conn = conn
        self.fetchers = fetchers or {"kraken": cex.fetch_kraken, "coinbase": cex.fetch_coinbase}
        self.clock = clock
        self.books = cex.BookCache(cfg.BOOK_MAX_AGE_S)
        self.pair_pools = {}     # pair name -> (pair cfg, [Pool])
        self.ref_prices = {}     # pair name -> USD price of the pair's base token
        self.pairs = list(cfg.PAIRS)   # active pairs: PAIRS plus the EXTRA_PAIRS that passed startup
        self.extra_names = []    # names of the active extra pairs, in config order
        self.dex_ref = {}        # extra pair name -> last round's median pool price (USD)
        self.tracker = EpisodeTracker()
        self.minutes = MinuteStats()
        self.last_block = None
        self.book_failures = 0
        self.pool = ThreadPoolExecutor(max_workers=8)
        self.recorder = recorder.Recorder(cfg, rpc, conn, log)
        self.prober = probe.Prober(cfg, rpc, conn, log, clock)
        # triangle probes get their own hourly cap, so they can't use up the dex>dex budget
        self.tri_cap = probe.HourlyCap(getattr(cfg, "TRI_PROBE_MAX_PER_HOUR",
                                               getattr(cfg, "PROBE_MAX_PER_HOUR", 60)))
        self.tri_prober = tri_probe.TriProber(cfg, rpc, conn, log, self.tri_cap.allow)
        self.bus = cex_triangle.CexTriCollector(cfg, conn, log, fetch=bus_fetch, clock=clock,
                                                pool=self.pool)
        self.cross = None        # (pair_a cfg, pair_b cfg, [Pool]) for the triangle, or None
        self.last_tri = []       # latest triangle results

    # ---- startup ------------------------------------------------------------------

    def startup(self):
        cfg = self.cfg
        if not self.rpc.get_code(cfg.MULTICALL3):
            raise SystemExit("Multicall3 has no code at that address: is BASE_RPC_URL really Base mainnet?")

        tokens = [cfg.USDC] + [p["base"] for p in cfg.PAIRS]
        problems = dex.verify_tokens(self.rpc, cfg.MULTICALL3, tokens)
        if problems:
            raise SystemExit("Token check failed:\n  " + "\n  ".join(problems))
        log("tokens OK: " + ", ".join(t["symbol"] for t in tokens))

        for pair in cfg.PAIRS:
            pools = dex.discover_pools(self.rpc, cfg.MULTICALL3, cfg.DEXES,
                                       pair["base"]["address"], cfg.USDC["address"])
            self.pair_pools[pair["name"]] = (pair, pools)
            log(f"{pair['name']}/USDC pools: " + (", ".join(p.label for p in pools) or "NONE"))
            found = {p.dex for p in pools}
            for d in cfg.DEXES:
                if d["name"] not in found:
                    log(f"  note: no {pair['name']} pools on {d['name']} (fine if it has none; check factory if it should)")

        self.setup_triangle()

        self.fetch_books()
        self.update_ref_prices()
        missing = [p["name"] for p in cfg.PAIRS if p["name"] not in self.ref_prices]
        if missing:
            raise SystemExit(f"No CEX price for {missing}: Kraken and Coinbase both unreachable?")
        self.setup_extras()
        log("reference prices: " + ", ".join(f"{k} ${v:,.4f}" if v < 10 else f"{k} ${v:,.2f}"
                                             for k, v in self.ref_prices.items()))
        self.recorder.startup()
        self.bus.startup()

    # ---- extra pairs (optional: anything wrong skips the pair, never the scanner) ----

    def quote_usd_for(self, pair):
        """USD value of one quote token: 1.0 for USDC, else the quote_ref reference price."""
        ref = pair.get("quote_ref")
        if ref is None:
            same = dex.pair_quote(self.cfg, pair)["address"].lower() == self.cfg.USDC["address"].lower()
            return 1.0 if same else None
        return self.ref_prices.get(ref)

    def quote_usds(self):
        """{pair name: USD per quote token} for the active pairs not quoted in USDC."""
        out = {}
        for pair in self.pairs:
            if pair.get("quote_ref"):
                out[pair["name"]] = self.quote_usd_for(pair)
        return out

    def base_usd(self, pair, now=None):
        """USD price of the base token from its own CEX book, else from base_ref, else None."""
        now = self.clock() if now is None else now
        for venue in ("kraken", "coinbase"):
            symbol = pair.get(venue)
            book = self.books.get(venue, symbol, now) if symbol else None
            if book and book.mid:
                return book.mid
        if pair.get("base_ref"):
            return self.ref_prices.get(pair["base_ref"])
        return None

    def setup_extras(self):
        cfg = self.cfg
        extras = list(getattr(cfg, "EXTRA_PAIRS", []) or [])
        if not extras:
            return
        quotes = [dex.pair_quote(cfg, p) for p in extras]
        infos = dex.read_tokens(self.rpc, cfg.MULTICALL3, [p["base"] for p in extras] + quotes)
        limit = getattr(cfg, "OFF_MARKET_PCT", 10.0)
        wide = dex.discovery_dexes(cfg.DEXES, getattr(cfg, "EXTRA_DISCOVERY_TIERS", None))
        for i, pair in enumerate(extras):
            name = pair["name"]
            problem = None
            filled = {}
            for role, tok, info in (("base", pair["base"], infos[i]),
                                    ("quote", quotes[i], infos[len(extras) + i])):
                problem = problem or token_problem(tok, info)
                if info:
                    filled[role] = dict(tok, decimals=info[1])
            if problem:
                log(f"extra pair {name} skipped: {problem}")
                continue
            pair = dict(pair, base=filled["base"], quote=filled["quote"])
            label = f"{pair['base']['symbol']}/{pair['quote']['symbol']}"

            pools = dex.discover_pools(self.rpc, cfg.MULTICALL3, wide,
                                       pair["base"]["address"], pair["quote"]["address"])
            if not pools:
                log(f"extra pair {name} skipped: no {label} pools on {', '.join(d['name'] for d in cfg.DEXES)}")
                continue

            for venue in ("kraken", "coinbase"):
                symbol = pair.get(venue)
                if not symbol:
                    continue
                fetch = self.fetchers.get(venue)
                try:
                    if fetch is None:
                        raise LookupError("no fetcher")
                    self.books.put(venue, symbol, fetch(symbol))
                except Exception as e:
                    log(f"  {name}: {venue} {symbol} not available ({type(e).__name__}); not used")
                    pair[venue] = None

            q_usd = self.quote_usd_for(pair)
            if not q_usd:
                log(f"extra pair {name} skipped: no USD price for its quote token {pair['quote']['symbol']}")
                continue
            px = dex.pool_buy_prices(self.rpc, cfg, pair, pools, q_usd, cfg.TRADE_SIZE_USD)
            ref = self.base_usd(pair)
            source = "CEX" if ref and not pair.get("base_ref") else ("base_ref" if ref else "median pool")
            ref = ref or dex.median_price(px.values())
            if not ref:
                log(f"extra pair {name} skipped: no pool could fill a ${cfg.TRADE_SIZE_USD:,.0f} buy")
                continue
            keep, dropped = [], []
            for p in pools:
                got = px.get(p.label)
                if got is None:
                    dropped.append(f"{p.label} (can't fill)")
                elif abs(got / ref - 1) * 100 > limit:
                    dropped.append(f"{p.label} ({(got / ref - 1) * 100:+.1f}%)")
                else:
                    keep.append(p)
            if not keep:
                log(f"extra pair {name} skipped: every pool off-market: " + ", ".join(dropped))
                continue
            self.pair_pools[name] = (pair, keep)
            self.pairs.append(pair)
            self.extra_names.append(name)
            self.ref_prices[name] = ref
            log(f"{name} pools ({label}, ref from {source}): " + ", ".join(p.label for p in keep))
            if dropped:
                log(f"  {name} dropped as off-market (> {limit:g}% from ${ref:,.4f}): " + ", ".join(dropped))

    def setup_triangle(self):
        """Find the pools between the triangle's two tokens. No pools = triangle off, scanner runs on."""
        names = getattr(self.cfg, "TRIANGLE_PAIRS", None)
        if not names:
            return
        pairs = {p["name"]: p for p in self.cfg.PAIRS}
        a, b = pairs.get(names[0]), pairs.get(names[1])
        if a is None or b is None:
            log(f"triangle off: TRIANGLE_PAIRS {names} must both be names in PAIRS")
            return
        pools = dex.discover_pools(self.rpc, self.cfg.MULTICALL3, self.cfg.DEXES,
                                   a["base"]["address"], b["base"]["address"])
        label = f"{a['base']['symbol']}/{b['base']['symbol']}"
        if not pools:
            log(f"triangle off: no {label} pools found")
            return
        self.cross = (a, b, pools)
        log(f"triangle {label} pools: " + ", ".join(p.label for p in pools))

    def compute_triangle(self, rd, eth_usd):
        a, b, _pools = self.cross
        a_to_b, b_to_a = triangle.cross_legs(self.cfg, rd, a, b, eth_usd)
        return triangle.compute(self.cfg.TRADE_SIZE_USD, a["base"]["symbol"], b["base"]["symbol"],
                                dex.to_dex_quotes(self.cfg, rd, a, eth_usd),
                                dex.to_dex_quotes(self.cfg, rd, b, eth_usd),
                                a_to_b, b_to_a)

    # ---- per round -----------------------------------------------------------------

    def fetch_books(self):
        jobs = {}
        for pair in self.pairs:
            for venue, fetch in self.fetchers.items():
                symbol = pair.get(venue)
                if symbol and (venue, symbol) not in jobs:
                    jobs[(venue, symbol)] = self.pool.submit(fetch, symbol)
        for (venue, symbol), fut in jobs.items():
            try:
                self.books.put(venue, symbol, fut.result())
            except Exception:
                self.book_failures += 1   # cached book (if fresh) keeps the venue alive

    def update_ref_prices(self):
        now = self.clock()
        for pair in self.pairs:              # PAIRS first, so base_ref "ETH"/"BTC" is fresh
            name = pair["name"]
            usd = self.base_usd(pair, now)
            if usd is None and name in self.extra_names:
                usd = self.dex_ref.get(name)     # no CEX book: last round's median pool price
            if usd:
                self.ref_prices[name] = usd

    def step(self):
        """One scan. Returns (block, results) or None if the block hasn't changed."""
        cfg = self.cfg
        # Quote the chain and fetch the books at the same time.
        rd_job = self.pool.submit(dex.quote_round, self.rpc, cfg, self.pair_pools,
                                  dict(self.ref_prices), cfg.TRADE_SIZE_USD, self.cross,
                                  self.quote_usds())
        self.fetch_books()
        rd = rd_job.result()
        # Same block as last scan = nothing new. An OLDER block can happen when scans
        # alternate between providers and one is a block behind: skip it too.
        if self.last_block is not None and rd.block <= self.last_block:
            return None
        self.last_block = rd.block
        now = self.clock()
        self.update_ref_prices()
        eth_usd = self.ref_prices.get("ETH")
        quote_usd = self.quote_usds()

        results = {}
        price_rows = []
        for name, (pair, _pools) in self.pair_pools.items():
            quotes = dex.to_dex_quotes(cfg, rd, pair, eth_usd, quote_usd.get(name) or 1.0)
            if name in self.extra_names:
                mid = dex.dex_mid(quotes, cfg.TRADE_SIZE_USD)
                if mid:
                    self.dex_ref[name] = mid
            books = {v: self.books.get(v, pair.get(v), now) if pair.get(v) else None
                     for v in ("kraken", "coinbase")}
            if getattr(cfg, "PRICE_LOG", False):
                row = prices.build(name, cfg.TRADE_SIZE_USD, quotes, books, now)
                if not row.empty:
                    price_rows.append(row)
            # CEX routes only where the pools price in USDC (the CEX books are USD markets)
            usdc_quoted = name not in quote_usd
            for key, r in compute_routes(name, cfg.TRADE_SIZE_USD, quotes,
                                         books if usdc_quoted else {},
                                         cfg.CEX_FEES, cfg.REBALANCE_COST_USD).items():
                results[(name, key)] = r

        if price_rows:
            db.insert_prices(self.conn, now, rd.block, price_rows)

        tri_opening = []
        if self.cross:
            self.last_tri = self.compute_triangle(rd, eth_usd)
            if self.last_tri:
                db.insert_tri(self.conn, now, rd.block, self.last_tri)
            tri_opening = self.tri_prober.openings(self.last_tri)

        # episodes opening this round, found before the tracker marks them open
        opening = self.prober.openings(results, self.tracker.open)
        closed = self.tracker.update(rd.block, now, results, dict(self.ref_prices))
        if closed:
            db.insert_episodes(self.conn, closed)
            for e in closed:
                log(episode_line(e))
        rows = self.minutes.add(now, results)
        if rows:
            db.insert_minutes(self.conn, rows)
        if opening:
            self.prober.probe(now, rd.block, opening, self.pair_pools, dict(self.ref_prices),
                              rd.basefee_wei, rd.l1_fee_wei, quote_usd)
        if tri_opening:
            self.tri_prober.probe(now, rd.block, tri_opening, self.cross, self.pair_pools,
                                  dict(self.ref_prices), rd.basefee_wei, rd.l1_fee_wei)
        if self.bus.due(now):
            self.bus.collect(now)
        if self.recorder.due(now):
            quotes = self.recorder.record(now, dict(self.ref_prices))
            if quotes:
                log(recorder.summary_line(quotes))
        return rd.block, results

    def shutdown(self):
        db.insert_episodes(self.conn, self.tracker.flush())
        db.insert_minutes(self.conn, self.minutes.flush())
        self.pool.shutdown(wait=False)


def fmt_px(px):
    return f"{px:,.2f}" if px else "?"


def episode_line(e):
    """One log line per closed episode: duration, best profit, and the prices behind it."""
    return (f"EPISODE {e.pair} {e.key}: {e.samples} samples / {e.seconds:.0f}s, "
            f"best ${e.max_net_usd:+.3f} ({e.max_net_pct:+.3f}%) "
            f"buy {e.best_buy} @{fmt_px(e.best_buy_px)} -> sell {e.best_sell} @{fmt_px(e.best_sell_px)}, "
            f"cex mid {fmt_px(e.best_mid)}")


def status_line(block, results, tri=None, bus=None, extras=(), rpc=None):
    """extras: names of the extra pairs. They share one 'EXTRA best' entry so the line
    stays short: the single best route across all of them. rpc: RpcPool.status() text."""
    parts = [f"blk {block}"]
    by_pair = {}
    best_extra = None
    for (pair, _key), r in results.items():
        if pair in extras:
            if best_extra is None or r.net_pct > best_extra.net_pct:
                best_extra = r
            continue
        if pair not in by_pair or r.net_pct > by_pair[pair].net_pct:
            by_pair[pair] = r
    for pair, r in by_pair.items():
        parts.append(f"{pair} best {r.key} {r.net_pct:+.3f}%")
    if best_extra is not None:
        parts.append(f"EXTRA best {best_extra.pair} {best_extra.key} {best_extra.net_pct:+.3f}%")
    if tri is not None:
        parts.append(triangle.best_line(tri))
    if bus is not None:
        parts.append(cex_triangle.best_line(bus))
    if rpc:
        parts.append(rpc)
    return " | ".join(parts)


def build_rpc(cfg, log, make_client=None):
    """RpcPool over RPC_URL and, if set, RPC_URL_2. Each is checked once (eth_chainId,
    timed). A second provider that fails the check is left out and logged; the scanner
    then runs on RPC_URL alone, as before. RPC_URL itself is always kept: if it is
    wrong, startup stops with its own message."""
    make_client = make_client or (lambda url: RpcClient(url, cfg.RPC_TIMEOUT_S))
    urls = [cfg.RPC_URL] + ([cfg.RPC_URL_2] if getattr(cfg, "RPC_URL_2", None) else [])
    clients = [make_client(u) for u in urls]
    keep = [clients[0]]
    for k, (name, ms, problem) in enumerate(rpclib.check_providers(clients)):
        if problem is None:
            log(f"RPC {name}: OK, Base, {ms:.0f}ms")
            if k:
                keep.append(clients[k])
        else:
            log(f"RPC {name}: {problem}" + ("  -> left out, running on the main RPC only" if k else ""))
    weights = list(getattr(cfg, "RPC_WEIGHTS", [1, 1]))
    if len(keep) < len(clients):
        weights = weights[:1]
    pool = RpcPool(keep, weights, getattr(cfg, "RPC_FAIL_LIMIT", 3), getattr(cfg, "RPC_BENCH_S", 60.0), log)
    if len(keep) > 1:
        log("RPC split: " + " / ".join(f"{s.name} x{weights[i] if i < len(weights) else 1}"
                                       for i, s in enumerate(pool.slots)) + " scans per turn")
    return pool


def print_table(results):
    print(f"{'route':<34}{'buy':<24}{'sell':<24}{'back $':>10}{'cex fee':>9}{'gas':>8}{'net $':>9}{'net %':>10}")
    for (_pair, _key), r in sorted(results.items(), key=lambda kv: -kv[1].net_pct):
        print(f"{r.pair + ' ' + r.key:<34}{r.buy_venue:<24}{r.sell_venue:<24}{r.usd_back:>10.3f}"
              f"{r.cex_fee_usd:>9.3f}{r.gas_usd:>8.4f}{r.net_usd:>9.3f}{r.net_pct:>9.3f}%")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="scan one block, print all routes, exit")
    args = ap.parse_args(argv)

    log(f"RPC: {urlparse(config.RPC_URL).netloc}  size ${config.TRADE_SIZE_USD:.0f}")
    scanner = Scanner(config, build_rpc(config, log), db.connect(config.DB_PATH))
    scanner.startup()

    if args.once:
        started = time.time()
        out = scanner.step()
        took = time.time() - started
        if out:
            n_pools = sum(len(pools) for _pair, pools in scanner.pair_pools.values())
            print(f"one scan took {took:.2f}s for {len(scanner.pair_pools)} pairs / {n_pools} pools "
                  f"(must stay well under POLL_SECONDS = {config.POLL_SECONDS:g}s)")
            print_table(out[1])
            print()
            if scanner.cross:
                triangle.print_table(scanner.last_tri)
                print()
            if scanner.bus.enabled:
                cex_triangle.print_table(scanner.bus.last)
                print()
            recorder.print_table(scanner.recorder.last_quotes, scanner.recorder.depth_usd)
        scanner.shutdown()
        return

    try:
        run_forever(scanner, config.POLL_SECONDS)
    except KeyboardInterrupt:
        log("stopping: saving open episodes")
    finally:
        scanner.shutdown()


# Errors that skip a round instead of stopping the scanner. sqlite3.OperationalError
# covers "database is locked" if something holds the file longer than db.BUSY_TIMEOUT_S.
RETRYABLE = (RpcError, RuntimeError, ValueError, sqlite3.OperationalError)


def run_forever(scanner, poll_seconds, sleep=time.sleep, clock=time.time, max_rounds=None):
    """Scan until Ctrl+C. A failed round waits 2, 4, 8 ... up to 30s, then tries again.
    max_rounds is for tests; normal runs never stop on their own."""
    fails = 0
    rounds = 0
    while max_rounds is None or rounds < max_rounds:
        rounds += 1
        started = clock()
        rotate = getattr(getattr(scanner, "rpc", None), "rotate", None)
        if rotate:
            rotate()                 # this scan's provider (load split); no-op with one RPC
        try:
            out = scanner.step()
            fails = 0
            if out:
                tri = getattr(scanner, "last_tri", None) if getattr(scanner, "cross", None) else None
                bus = getattr(scanner, "bus", None)
                bus = bus.last if bus is not None and bus.enabled else None
                status = getattr(getattr(scanner, "rpc", None), "status", None)
                log(status_line(*out, tri=tri, bus=bus, extras=getattr(scanner, "extra_names", ()),
                                rpc=status() if callable(status) else None))
        except RETRYABLE as e:
            fails += 1
            wait = min(30, 2 ** fails)
            log(f"round failed ({type(e).__name__}: {e}); retrying in {wait}s")
            sleep(wait)
            continue
        sleep(max(0.0, poll_seconds - (clock() - started)))
    return fails


if __name__ == "__main__":
    sys.exit(main())
