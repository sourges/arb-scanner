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
import config
import db
import dex
import recorder
from routes import compute_routes
from rpc import RpcClient, RpcError
from tracker import EpisodeTracker, MinuteStats


def log(msg: str):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


class Scanner:
    def __init__(self, cfg, rpc, conn, fetchers=None, clock=time.time):
        self.cfg = cfg
        self.rpc = rpc
        self.conn = conn
        self.fetchers = fetchers or {"kraken": cex.fetch_kraken, "coinbase": cex.fetch_coinbase}
        self.clock = clock
        self.books = cex.BookCache(cfg.BOOK_MAX_AGE_S)
        self.pair_pools = {}     # pair name -> (pair cfg, [Pool])
        self.ref_prices = {}     # pair name -> USD
        self.tracker = EpisodeTracker()
        self.minutes = MinuteStats()
        self.last_block = None
        self.book_failures = 0
        self.pool = ThreadPoolExecutor(max_workers=8)
        self.recorder = recorder.Recorder(cfg, rpc, conn, log)

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

        self.fetch_books()
        self.update_ref_prices()
        missing = [p["name"] for p in cfg.PAIRS if p["name"] not in self.ref_prices]
        if missing:
            raise SystemExit(f"No CEX price for {missing}: Kraken and Coinbase both unreachable?")
        log("reference prices: " + ", ".join(f"{k} ${v:,.2f}" for k, v in self.ref_prices.items()))
        self.recorder.startup()

    # ---- per round -----------------------------------------------------------------

    def fetch_books(self):
        jobs = {}
        for pair in self.cfg.PAIRS:
            for venue, fetch in self.fetchers.items():
                symbol = pair[venue]
                jobs[(venue, symbol)] = self.pool.submit(fetch, symbol)
        for (venue, symbol), fut in jobs.items():
            try:
                self.books.put(venue, symbol, fut.result())
            except Exception:
                self.book_failures += 1   # cached book (if fresh) keeps the venue alive

    def update_ref_prices(self):
        now = self.clock()
        for pair in self.cfg.PAIRS:
            for venue in ("kraken", "coinbase"):
                book = self.books.get(venue, pair[venue], now)
                if book and book.mid:
                    self.ref_prices[pair["name"]] = book.mid
                    break

    def step(self):
        """One scan. Returns (block, results) or None if the block hasn't changed."""
        cfg = self.cfg
        # Quote the chain and fetch the books at the same time.
        rd_job = self.pool.submit(dex.quote_round, self.rpc, cfg, self.pair_pools,
                                  dict(self.ref_prices), cfg.TRADE_SIZE_USD)
        self.fetch_books()
        rd = rd_job.result()
        if rd.block == self.last_block:
            return None
        self.last_block = rd.block
        now = self.clock()
        self.update_ref_prices()
        eth_usd = self.ref_prices.get("ETH")

        results = {}
        for name, (pair, _pools) in self.pair_pools.items():
            quotes = dex.to_dex_quotes(cfg, rd, pair, eth_usd)
            books = {v: self.books.get(v, pair[v], now) for v in ("kraken", "coinbase")}
            for key, r in compute_routes(name, cfg.TRADE_SIZE_USD, quotes, books,
                                         cfg.CEX_FEES, cfg.REBALANCE_COST_USD).items():
                results[(name, key)] = r

        closed = self.tracker.update(rd.block, now, results, dict(self.ref_prices))
        if closed:
            db.insert_episodes(self.conn, closed)
            for e in closed:
                log(episode_line(e))
        rows = self.minutes.add(now, results)
        if rows:
            db.insert_minutes(self.conn, rows)
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


def status_line(block, results):
    parts = [f"blk {block}"]
    by_pair = {}
    for (pair, _key), r in results.items():
        if pair not in by_pair or r.net_pct > by_pair[pair].net_pct:
            by_pair[pair] = r
    for pair, r in by_pair.items():
        parts.append(f"{pair} best {r.key} {r.net_pct:+.3f}%")
    return " | ".join(parts)


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
    scanner = Scanner(config, RpcClient(config.RPC_URL, config.RPC_TIMEOUT_S), db.connect(config.DB_PATH))
    scanner.startup()

    if args.once:
        out = scanner.step()
        if out:
            print_table(out[1])
            print()
            recorder.print_table(scanner.recorder.last_quotes)
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
        try:
            out = scanner.step()
            fails = 0
            if out:
                log(status_line(*out))
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
