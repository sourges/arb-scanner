"""The Dec 11, 2026 dex>dex decision, applied mechanically to the data.

The rule was written down on Nov 27, before looking at the data (research-notes,
projects/dex-arbitrage.md). This module only does the arithmetic; read its verdict
as a starting point and check it by hand.

Data: dex>dex size probes (episode_probes) from --since (the Nov 27 fix) onward.
    A probe counts only if its episode lasted 2 or more samples (one-scan flashes
    are reported, not counted). A probe joins its episode on (pair, route, start_ts = ts).
At each probe size S, per probe:
    net $         = net_edge % / 100 * S                 (after gas, as probed)
    Aero haircut  = net $ * 0.5 if either pool is aerov3  (MEV tax not modeled)
    attempted     only if net $ > COST_PER_SUCCESS ($0.15), else a bot wouldn't bother
    expected $    = SUCCESS_RATE (0.425) * (net $ - $0.15)
Per size: expected $ per day over the window, days with expected $ > 0, and the
biggest single day's share of the total.
    PASS     at $500 or $1,000: >= $3/day, positive on >= 10 of every 14 days,
             no day more than 75% of the total
    SHELVE   < $1/day at every size, or one day > 75% of the total at every size
             that clears $1/day, or one-scan flashes are most of the value
    else     IN BETWEEN: keep collecting until Dec 31
"""
from __future__ import annotations

import math
import time

SUCCESS_RATE = 0.425          # Wu & Oz: targeted bots' success rate (a best case for a newcomer)
COST_PER_SUCCESS = 0.15       # Wu & Oz: ~0.000095 ETH per successful arb, failures included
AERO_HAIRCUT = 0.5
PASS_SIZES = (500.0, 1000.0)
PASS_PER_DAY, SHELVE_PER_DAY = 3.0, 1.0
POSITIVE_DAYS = (10, 14)      # at least 10 of every 14 days
MAX_DAY_SHARE = 0.75


def is_aero(pool):
    return bool(pool) and pool.startswith("aerov3")


def probe_values(conn, since_ts, until_ts):
    """[(ts, size, net $ after haircut, samples of its episode or None)] for dex>dex probes."""
    rows = conn.execute(
        "SELECT p.ts, p.size_usd, p.net_edge, p.buy_pool, p.sell_pool, e.samples"
        " FROM episode_probes p LEFT JOIN episodes e"
        "   ON e.pair = p.pair AND e.route = p.route AND e.start_ts = p.ts"
        " WHERE p.route = 'dex>dex' AND p.ts >= ? AND p.ts < ? AND p.net_edge IS NOT NULL",
        (since_ts, until_ts)).fetchall()
    out = []
    for ts, size, net, buy, sell, samples in rows:
        usd = net / 100 * size
        if usd > 0 and (is_aero(buy) or is_aero(sell)):
            usd *= AERO_HAIRCUT
        out.append((ts, size, usd, samples))
    return out


def expected(usd):
    return SUCCESS_RATE * (usd - COST_PER_SUCCESS) if usd > COST_PER_SUCCESS else 0.0


def evaluate(conn, since_ts, until_ts):
    """Per size: dict(size, per_day, days_pos, days, need, max_share, flash_share, n).
    Plus the verdict string."""
    first = conn.execute("SELECT MIN(ts) FROM episode_probes WHERE route = 'dex>dex' AND ts >= ?",
                         (since_ts,)).fetchone()[0]
    if first is not None and since_ts < first - 86400:
        since_ts = first              # whole-history runs: count days from the first probe
    days = max(1, round((until_ts - since_ts) / 86400))
    need = math.ceil(POSITIVE_DAYS[0] / POSITIVE_DAYS[1] * days)
    values = probe_values(conn, since_ts, until_ts)
    sizes = sorted({v[1] for v in values})
    per_size = []
    for size in sizes:
        by_day, counted, flash, n = {}, 0.0, 0.0, 0
        for ts, s, usd, samples in values:
            if s != size:
                continue
            e = expected(usd)
            if samples is not None and samples >= 2:
                if e > 0:
                    day = time.strftime("%Y-%m-%d", time.localtime(ts))
                    by_day[day] = by_day.get(day, 0.0) + e
                    counted += e
                    n += 1
            else:
                flash += e
        per_size.append({
            "size": size, "n": n, "per_day": counted / days, "days": days, "need": need,
            "days_pos": sum(1 for v in by_day.values() if v > 0),
            "max_share": max(by_day.values()) / counted if counted else None,
            "flash_share": flash / (flash + counted) if flash + counted else None,
        })
    return per_size, verdict(per_size)


def verdict(per_size):
    if not per_size:
        return "NO DATA (no dex>dex probes in the window)"
    for r in per_size:
        if (r["size"] in PASS_SIZES and r["per_day"] >= PASS_PER_DAY and r["days_pos"] >= r["need"]
                and r["max_share"] is not None and r["max_share"] <= MAX_DAY_SHARE):
            return f"PASS at ${r['size']:,.0f} (build an atomic SIMULATION, not a live contract)"
    clearing = [r for r in per_size if r["per_day"] >= SHELVE_PER_DAY]
    if not clearing:
        return "SHELVE (< $1/day at every size)"
    if all(r["max_share"] is not None and r["max_share"] > MAX_DAY_SHARE for r in clearing):
        return "SHELVE (the value that clears $1/day is mostly one day)"
    if all(r["flash_share"] is not None and r["flash_share"] > 0.5 for r in clearing):
        return "SHELVE (most of the value is one-scan flashes)"
    return "IN BETWEEN (keep collecting until Dec 31, decide then)"


def print_section(conn, since_ts, until_ts):
    start = time.strftime("%Y-%m-%d %H:%M", time.localtime(since_ts))
    end = time.strftime("%Y-%m-%d %H:%M", time.localtime(until_ts))
    print(f"\nDEC 11 RULE (dex>dex probes {start} -> {end}; episodes of 2+ samples; Aero x{AERO_HAIRCUT};"
          f" {SUCCESS_RATE:.1%} success, ${COST_PER_SUCCESS:.2f} per success)")
    per_size, v = evaluate(conn, since_ts, until_ts)
    if per_size:
        print(f"{'size':>8}{'counted':>9}{'exp $/day':>11}{'days >0':>10}{'top day':>9}{'flashes':>9}")
        for r in per_size:
            share = f"{r['max_share']:.0%}" if r["max_share"] is not None else "-"
            flash = f"{r['flash_share']:.0%}" if r["flash_share"] is not None else "-"
            print(f"{'$' + format(r['size'], ',.0f'):>8}{r['n']:>9}{r['per_day']:>11.2f}"
                  f"{str(r['days_pos']) + '/' + str(r['days']):>10}{share:>9}{flash:>9}")
        print(f"days >0 needed: {per_size[0]['need']} of {per_size[0]['days']}."
              " 'flashes' = share of the value from one-scan episodes (not counted).")
    print(f"VERDICT (mechanical, check by hand): {v}")
