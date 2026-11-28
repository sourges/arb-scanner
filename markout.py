"""Markouts for CEX<->DEX episodes: does a gap survive if the CEX leg is done later?

Idea from Wu, Sui, Thiery, Pai (AFT 2025), see research-notes. The scanner scores a
CEX<->DEX route as if both legs trade at the same instant. A real hedge lands later.
For every CEX<->DEX episode this module re-prices the CEX leg 0, 2, 4, 10 and 30
seconds after the episode opened, from the scan_prices table (no new data needed).

The DEX leg is fixed at the opening scan:
    dex>cex:  bought on the best Base pool at dex_buy_px (USDC per base, $100, pool fee in)
    cex>dex:  sold on the best Base pool at dex_sell_px
The CEX leg at time t uses that scan's book, the same way routes.py does:
    sell on the CEX:  taker at the bid, maker at the ask (a resting ask that fills)
    buy on the CEX:   taker at the ask, maker at the bid (a resting bid that fills)
Net % at t, the same formulas as routes.py (fee on what the CEX side trades):
    dex>cex:  cex_px(t) * (1 - fee) / dex_buy_px(0) - 1
    cex>dex:  dex_sell_px(0) / cex_px(t) - 1 - fee
Gas (~0.0003% at $100) and rebalance cost (0) are left out.

Who closed the gap: the gap is g = DEX mid / CEX mid - 1 (CEX = the route's book).
Between the open and +10s (or the last horizon there is a row for):
    pool part = how far the DEX mid moved TOWARD the CEX
    cex part  = how far the CEX mid moved TOWARD the DEX
If |g| shrank by at least half: "pool" if the pool part is >= 2/3 of the closing,
"cex" if the CEX part is, else "mixed". Otherwise "open".
    pool = the pool was mispriced: a real gap, quick hedging captures it
    cex  = the CEX was lagging: the trade was really a directional bet

Limits: 2-second scans, so +2s is the finest step; $100 size, no hedge price impact;
the best pool at a later scan can differ from the episode's pool (mid only).
"""
from __future__ import annotations

import statistics

HORIZONS = (0, 2, 4, 10, 30)
TOLERANCE_S = 1.5          # a scan within this of the horizon counts for it
CLOSE_AT = 10              # horizon used for "who closed the gap"
MAJORS = ("ETH", "BTC")


def parse_route(route, cex_fees):
    """'dex>kraken_reduced_taker' -> ('dex>cex', 'kraken', 'taker', 0.0038).
    None for routes that aren't CEX<->DEX (dex>dex)."""
    left, _, right = route.partition(">")
    if left == "dex" and right != "dex":
        direction, sched_side = "dex>cex", right
    elif right == "dex" and left != "dex":
        direction, sched_side = "cex>dex", left
    else:
        return None
    schedule, _, side = sched_side.rpartition("_")
    fees = cex_fees.get(schedule)
    if not fees or side not in ("maker", "taker"):
        return None
    return direction, fees["book"], side, fees[side]


def cex_px(row, book, direction, side):
    """The CEX price the route trades at in this scan's row, or None."""
    bid, ask = row[f"{book}_bid"], row[f"{book}_ask"]
    if direction == "dex>cex":                  # selling on the CEX
        return bid if side == "taker" else ask
    return ask if side == "taker" else bid      # buying on the CEX


def net_pct(direction, fee, dex_px, px):
    if not dex_px or not px:
        return None
    if direction == "dex>cex":
        return (px * (1 - fee) / dex_px - 1) * 100
    return (dex_px / px - 1 - fee) * 100


def mids(row, book):
    bid, ask = row[f"{book}_bid"], row[f"{book}_ask"]
    cex = (bid + ask) / 2 if bid and ask else None
    b, s = row["dex_buy_px"], row["dex_sell_px"]
    dex = (b + s) / 2 if b and s else None
    return dex, cex


def who_closed(row0, row_t, book):
    """'pool', 'cex', 'mixed', 'open', or None if a mid is missing."""
    d0, c0 = mids(row0, book)
    dt, ct = mids(row_t, book)
    if not (d0 and c0 and dt and ct):
        return None
    g0, gt = d0 / c0 - 1, dt / ct - 1
    if g0 == 0 or abs(gt) > abs(g0) / 2:
        return "open"
    sign = 1 if g0 > 0 else -1
    pool_part = -(dt / d0 - 1) * sign            # DEX above CEX: pool falling closes it
    cex_part = (ct / c0 - 1) * sign              # DEX above CEX: CEX rising closes it
    total = pool_part + cex_part
    if total <= 0:
        return "mixed"
    if pool_part >= 2 / 3 * total:
        return "pool"
    if cex_part >= 2 / 3 * total:
        return "cex"
    return "mixed"


COLS = ("ts", "kraken_bid", "kraken_ask", "coinbase_bid", "coinbase_ask", "dex_buy_px", "dex_sell_px")


def price_rows(conn, pair, start, end):
    rows = conn.execute(f"SELECT {', '.join(COLS)} FROM scan_prices WHERE path = ? AND ts BETWEEN ? AND ?"
                        " ORDER BY ts", (pair, start, end)).fetchall()
    return [dict(zip(COLS, r)) for r in rows]


def nearest(rows, target):
    best = min(rows, key=lambda r: abs(r["ts"] - target), default=None)
    return best if best is not None and abs(best["ts"] - target) <= TOLERANCE_S else None


def episode_markout(conn, pair, route, start_ts, cex_fees):
    """{'nets': {horizon: net % or None}, 'closed': str or None}, or None if the episode
    isn't CEX<->DEX or there's no price row at its start."""
    parsed = parse_route(route, cex_fees)
    if parsed is None:
        return None
    direction, book, side, fee = parsed
    rows = price_rows(conn, pair, start_ts - TOLERANCE_S, start_ts + max(HORIZONS) + TOLERANCE_S)
    row0 = nearest(rows, start_ts)
    if row0 is None:
        return None
    dex_px = row0["dex_buy_px"] if direction == "dex>cex" else row0["dex_sell_px"]
    nets = {}
    for h in HORIZONS:
        r = row0 if h == 0 else nearest(rows, start_ts + h)
        nets[h] = net_pct(direction, fee, dex_px, cex_px(r, book, direction, side)) if r else None
    closed = None
    for h in sorted((h for h in HORIZONS if 0 < h <= CLOSE_AT), reverse=True):
        r = nearest(rows, start_ts + h)
        if r is not None:
            closed = who_closed(row0, r, book)
            break
    return {"nets": nets, "closed": closed}


def summary(conn, since_ts, min_usd, cex_fees):
    """Per (pair, route) of CEX<->DEX episodes that reached min_usd:
    (pair, route, n, {h: median net %}, % of episodes > 0 at +4s, Counter of who closed)."""
    eps = conn.execute("SELECT pair, route, start_ts FROM episodes WHERE start_ts >= ?"
                       " AND max_net_usd >= ? ORDER BY start_ts", (since_ts, min_usd)).fetchall()
    groups = {}
    for pair, route, start in eps:
        m = episode_markout(conn, pair, route, start, cex_fees)
        if m is not None:
            groups.setdefault((pair, route), []).append(m)
    out = []
    for (pair, route), ms in groups.items():
        med = {}
        for h in HORIZONS:
            vals = [m["nets"][h] for m in ms if m["nets"][h] is not None]
            med[h] = statistics.median(vals) if vals else None
        at4 = [m["nets"][4] for m in ms if m["nets"][4] is not None]
        pos4 = 100 * sum(1 for v in at4 if v > 0) / len(at4) if at4 else None
        closed = {k: sum(1 for m in ms if m["closed"] == k) for k in ("pool", "cex", "mixed", "open")}
        out.append((pair, route, len(ms), med, pos4, closed))
    major = lambda p: 0 if p in MAJORS else 1
    return sorted(out, key=lambda r: (major(r[0]), r[0], -r[2]))


def fmt(v):
    return f"{v:+.3f}" if v is not None else "-"


def print_section(conn, since, min_usd, cex_fees):
    print(f"\nMARKOUTS (CEX<->DEX episodes >= ${min_usd:.2f}: median net % with the CEX leg done later)")
    rows = summary(conn, since, min_usd, cex_fees)
    if not rows:
        print("  none (no CEX<->DEX episodes with price rows in this window)")
        return
    head = "".join(f"{('net@0' if h == 0 else f'+{h}s'):>8}" for h in HORIZONS)
    print(f"{'pair route':<36}{'n':>5}{head}{'>0 @+4s':>9}   pool/cex/mixed/open")
    last_group = None
    for pair, route, n, med, pos4, closed in rows:
        group = "majors" if pair in MAJORS else "long tail"
        if group != last_group:
            print(f"  -- {group} --")
            last_group = group
        cells = "".join(f"{fmt(med[h]):>8}" for h in HORIZONS)
        pos = f"{pos4:.0f}%" if pos4 is not None else "-"
        print(f"{pair + ' ' + route:<36}{n:>5}{cells}{pos:>9}   "
              f"{closed['pool']}/{closed['cex']}/{closed['mixed']}/{closed['open']}")
    print("pool = the pool moved to the CEX price (a real gap); cex = the CEX moved to the pool's"
          " (a lagging CEX: directional).")
    print("2-second scans: +2s is the finest step. $100, no hedge price impact, gas left out.")
