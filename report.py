"""Summarize what the scanner logged.

Run:  python report.py                      whole history
      python report.py --hours 24           last 24 hours only
      python report.py --min-usd 0.10       count only episodes that reached $0.10
      python report.py --top 30             list the 30 best episodes (default 15)

The last section summarizes the pegged-pair recorder (one quote per minute per
pool): where each pair's mid sits versus 1.0, how much it wanders, and how often
the best bid on one pool beat the best ask on another (a gross gap, before gas).
TRIANGLE summarizes both triangle loops (every scan at $100), and TRIANGLE SIZE
PROBES shows how the loops that opened held up at bigger sizes.
PEGGED POOLS then lists every pool: how often it set the best price, and its
round trip at $100 and at the bigger depth size (a thin pool's blows up).

Episodes below --min-usd (default $0.05) are counted as dust and left out of
the episode tables, so the few real opportunities aren't buried.
"""
import argparse
import sqlite3
import statistics
import time

import config

DEFAULT_MIN_USD = 0.05


def route_summary(conn, since_ts):
    """Per route: how often net % was above zero, and how close it usually got."""
    rows = conn.execute(
        "SELECT pair, route, SUM(samples), SUM(positives), MAX(best_net_pct),"
        " SUM(avg_net_pct * samples) / SUM(samples)"
        " FROM minute_stats WHERE minute >= ? GROUP BY pair, route", (since_ts,)).fetchall()
    return sorted(rows, key=lambda r: -r[4])


def dust_count(conn, since_ts, min_usd):
    (n,) = conn.execute("SELECT COUNT(*) FROM episodes WHERE start_ts >= ? AND max_net_usd < ?",
                        (since_ts, min_usd)).fetchone()
    return n


def episode_summary(conn, since_ts, min_usd):
    """Per route, episodes that reached min_usd: count, median and max seconds, sum of best $."""
    out = {}
    for pair, route, seconds, max_usd in conn.execute(
            "SELECT pair, route, seconds, max_net_usd FROM episodes"
            " WHERE start_ts >= ? AND max_net_usd >= ?", (since_ts, min_usd)):
        out.setdefault((pair, route), []).append((seconds, max_usd))
    summary = []
    for (pair, route), eps in out.items():
        summary.append((pair, route, len(eps),
                        statistics.median(e[0] for e in eps),
                        max(e[0] for e in eps),
                        sum(e[1] for e in eps)))
    return sorted(summary, key=lambda r: -r[5])


def top_episodes(conn, since_ts, min_usd, limit):
    """The best individual episodes, with the prices behind them (NULL for pre-v2 rows)."""
    return conn.execute(
        "SELECT start_ts, pair, route, samples, seconds, max_net_usd, max_net_pct,"
        " best_buy, best_buy_px, best_sell, best_sell_px, start_mid, best_mid"
        " FROM episodes WHERE start_ts >= ? AND max_net_usd >= ?"
        " ORDER BY max_net_usd DESC LIMIT ?", (since_ts, min_usd, limit)).fetchall()


def pegged_summary(conn, since_ts):
    """Per pair from the recorder: minutes, mid vs 1.0 (avg/min/max/std in bp),
    minutes where gap > 0, and the best gap in bp. Best bid/ask are taken across
    pools within each minute, as in recorder.summarize."""
    minutes = {}
    for pair, ts, bid, ask in conn.execute(
            "SELECT pair, ts, bid, ask FROM pegged_quotes WHERE ts >= ?", (since_ts,)):
        best = minutes.setdefault((pair, ts), [None, None])
        if bid is not None and (best[0] is None or bid > best[0]):
            best[0] = bid
        if ask is not None and (best[1] is None or ask < best[1]):
            best[1] = ask
    per_pair = {}
    for (pair, _ts), (bid, ask) in minutes.items():
        if bid is None or ask is None:
            continue
        mid_bp = ((bid + ask) / 2 - 1.0) * 1e4
        gap_bp = (bid / ask - 1.0) * 1e4
        per_pair.setdefault(pair, []).append((mid_bp, gap_bp))
    out = []
    for pair, vals in per_pair.items():
        mids = [v[0] for v in vals]
        gaps = [v[1] for v in vals]
        out.append((pair, len(vals), statistics.fmean(mids), min(mids), max(mids),
                    statistics.pstdev(mids), sum(1 for g in gaps if g > 0), max(gaps)))
    return sorted(out)


def pegged_pools(conn, since_ts):
    """Per pair and pool: rows, % of minutes it set the best bid / best ask, and its
    median round trip (ask / bid - 1, in bp) at $100 and at the depth size.
    A thin pool shows a big jump from the $100 round trip to the bigger one, or
    'filled' well under 100% (it couldn't fill the bigger size).
    Depth columns are NULL before Nov 14, 2026 and show as '-'."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(pegged_quotes)")}
    depth = "depth_usd, depth_bid, depth_ask" if "depth_bid" in cols else "NULL, NULL, NULL"
    rows = conn.execute(f"SELECT pair, ts, pool, bid, ask, {depth} FROM pegged_quotes"
                        " WHERE ts >= ?", (since_ts,)).fetchall()
    best = {}   # (pair, ts) -> [best bid, its pool, best ask, its pool]
    for pair, ts, pool, bid, ask, *_ in rows:
        b = best.setdefault((pair, ts), [None, None, None, None])
        if bid is not None and (b[0] is None or bid > b[0]):
            b[0], b[1] = bid, pool
        if ask is not None and (b[2] is None or ask < b[2]):
            b[2], b[3] = ask, pool
    minutes = {}
    for (pair, _ts) in best:
        minutes[pair] = minutes.get(pair, 0) + 1
    stats_ = {}
    for pair, ts, pool, bid, ask, d_usd, d_bid, d_ask in rows:
        s = stats_.setdefault((pair, pool), {"n": 0, "bid_wins": 0, "ask_wins": 0, "rt": [],
                                             "rt_big": [], "big_tried": 0, "size": None})
        s["n"] += 1
        b = best[(pair, ts)]
        s["bid_wins"] += b[1] == pool
        s["ask_wins"] += b[3] == pool
        if bid and ask:
            s["rt"].append((ask / bid - 1) * 1e4)
        if d_usd:
            s["big_tried"] += 1
            s["size"] = d_usd
            if d_bid and d_ask:
                s["rt_big"].append((d_ask / d_bid - 1) * 1e4)
    out = []
    for (pair, pool), s in stats_.items():
        m = minutes[pair]
        out.append((pair, pool, s["n"], 100 * s["bid_wins"] / m, 100 * s["ask_wins"] / m,
                    statistics.median(s["rt"]) if s["rt"] else None,
                    statistics.median(s["rt_big"]) if s["rt_big"] else None,
                    100 * len(s["rt_big"]) / s["big_tried"] if s["big_tried"] else None,
                    s["size"]))
    # per pair: pools that set prices first, then by $100 round trip
    return sorted(out, key=lambda r: (r[0], -(r[3] + r[4]), r[5] if r[5] is not None else float("inf")))


def fmt_bp(v):
    if v is None:
        return "-"
    return f"{v:,.1f}" if v < 100_000 else "dead"


def has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def probe_summary(conn, since_ts, table="episode_probes"):
    """Per probe size: probes, how many filled, how many netted > 0, median and best net %.
    table: episode_probes (dex>dex) or tri_probes (triangle)."""
    if not has_table(conn, table):
        return []
    per_size = {}
    for size, net in conn.execute(
            f"SELECT size_usd, net_edge FROM {table} WHERE ts >= ?", (since_ts,)):
        per_size.setdefault(size, []).append(net)
    out = []
    for size in sorted(per_size):
        nets = per_size[size]
        filled = [n for n in nets if n is not None]
        out.append((size, len(nets), len(filled), sum(1 for n in filled if n > 0),
                    statistics.median(filled) if filled else None,
                    max(filled) if filled else None))
    return out


def top_probes(conn, since_ts, limit, table="episode_probes"):
    """Best probed gaps: [(ts, path, {size: net % or None})], ordered by the
    smallest size's net %, best first."""
    if not has_table(conn, table):
        return []
    probes = {}
    for ts, path, size, net in conn.execute(
            f"SELECT ts, path, size_usd, net_edge FROM {table} WHERE ts >= ?", (since_ts,)):
        probes.setdefault((ts, path), {})[size] = net

    def first_net(item):
        sizes = item[1]
        net = sizes[min(sizes)]
        return net if net is not None else float("-inf")

    ranked = sorted(probes.items(), key=first_net, reverse=True)[:limit]
    return [(ts, path, sizes) for (ts, path), sizes in ranked]


def tri_summary(conn, since_ts):
    """Per triangle loop: scans, scans with net > 0, median and best net %."""
    if not has_table(conn, "tri_quotes"):
        return []
    per_loop = {}
    for path, net in conn.execute(
            "SELECT path, net_edge FROM tri_quotes WHERE ts >= ?", (since_ts,)):
        per_loop.setdefault(path, []).append(net)
    return [(path, len(nets), sum(1 for n in nets if n > 0), statistics.median(nets), max(nets))
            for path, nets in sorted(per_loop.items())]


def print_probe_table(conn, since, top, table, path_label, path_width):
    """The size table plus the top probed gaps, for either probe table."""
    sizes = probe_summary(conn, since, table)
    if not sizes:
        print("  none yet")
        return
    print(f"{'size':>8}{'probes':>8}{'filled':>8}{'net>0':>7}{'median net':>13}{'best net':>12}")
    for size, n, filled, pos, med, best in sizes:
        print(f"{'$' + format(size, ',.0f'):>8}{n:>8}{filled:>8}{pos:>7}{fmt_pct(med):>13}{fmt_pct(best):>12}")
    all_sizes = [row[0] for row in sizes]
    print(f"\nTOP {top} PROBED GAPS (local time)")
    print(f"{'start':<16}{path_label:<{path_width}}" + "".join(f"{'$' + format(z, ',.0f'):>12}" for z in all_sizes))
    for ts, path, by_size in top_probes(conn, since, top, table):
        start = time.strftime("%m-%d %H:%M:%S", time.localtime(ts))
        print(f"{start:<16}{path:<{path_width}}" + "".join(f"{fmt_pct(by_size[z]) if z in by_size else '-':>12}" for z in all_sizes))


def fmt_pct(v):
    return f"{v:+.3f}%" if v is not None else "can't fill"


def fmt_px(px):
    return f"{px:,.2f}" if px else "?"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=None)
    ap.add_argument("--min-usd", type=float, default=DEFAULT_MIN_USD)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args(argv)
    since = time.time() - args.hours * 3600 if args.hours else 0

    conn = sqlite3.connect(config.DB_PATH)
    try:
        print_report(conn, since, args.min_usd, args.top)
    finally:
        conn.close()   # Windows can't delete or move an open database file


def print_report(conn, since, min_usd, top):
    print("ROUTES (from minute stats)")
    print(f"{'pair route':<34}{'samples':>9}{'% >0':>8}{'best %':>9}{'avg %':>9}")
    for pair, route, n, pos, best, avg in route_summary(conn, since):
        print(f"{pair + ' ' + route:<34}{n:>9}{100 * pos / n:>7.2f}%{best:>8.3f}%{avg:>8.3f}%")

    print(f"\nEPISODES >= ${min_usd:.2f}  ({dust_count(conn, since, min_usd)} smaller ones skipped as dust)")
    print(f"{'pair route':<34}{'count':>7}{'med secs':>10}{'max secs':>10}{'sum best $':>12}")
    eps = episode_summary(conn, since, min_usd)
    if not eps:
        print("  none")
    for pair, route, count, med_secs, max_secs, total in eps:
        print(f"{pair + ' ' + route:<34}{count:>7}{med_secs:>10.0f}{max_secs:>10.0f}{total:>12.3f}")

    print(f"\nTOP {top} EPISODES (local time, prices at the best moment)")
    print(f"{'start':<16}{'pair route':<32}{'samp':>5}{'secs':>6}{'net $':>8}{'net %':>8}"
          f"  {'buy @ price':<28}{'sell @ price':<28}{'cex mid':>12}")
    for (ts, pair, route, samples, secs, usd, pct, buy, buy_px, sell, sell_px,
         _start_mid, best_mid) in top_episodes(conn, since, min_usd, top):
        start = time.strftime("%m-%d %H:%M:%S", time.localtime(ts))
        print(f"{start:<16}{pair + ' ' + route:<32}{samples:>5}{secs:>6.0f}{usd:>8.3f}{pct:>7.3f}%"
              f"  {buy + ' @' + fmt_px(buy_px):<28}{sell + ' @' + fmt_px(sell_px):<28}{fmt_px(best_mid):>12}")

    print("\n'sum best $' adds each episode's best moment once: an upper bound, not a P&L.")
    print("Prices show '?' for episodes logged before price tracking was added.")

    print("\nSIZE PROBES (dex>dex gaps re-quoted at bigger sizes when they open; net after gas)")
    print_probe_table(conn, since, top, "episode_probes", "pair  buy>sell pool", 30)

    print("\nTRIANGLE (every scan at $100, net after fees and gas)")
    loops = tri_summary(conn, since)
    if not loops:
        print("  none yet")
    else:
        print(f"{'loop':<26}{'scans':>9}{'% >0':>8}{'median net':>12}{'best net':>11}")
        for path, n, pos, med, best in loops:
            print(f"{path:<26}{n:>9}{100 * pos / n:>7.2f}%{fmt_pct(med):>12}{fmt_pct(best):>11}")

    print("\nTRIANGLE SIZE PROBES (loops re-quoted at bigger sizes when they open; net after gas)")
    print_probe_table(conn, since, top, "tri_probes", "loop  leg1/leg2/leg3 pools", 52)

    print("\nPEGGED PAIRS (recorder, once a minute; mid in bp away from 1.0, gap before gas)")
    print(f"{'pair':<14}{'minutes':>9}{'avg mid':>9}{'min':>8}{'max':>8}{'std':>7}{'gap>0':>8}{'best gap':>10}")
    pegs = pegged_summary(conn, since)
    if not pegs:
        print("  none yet")
    for pair, n, avg, lo, hi, std, pos, best in pegs:
        print(f"{pair:<14}{n:>9}{avg:>+9.1f}{lo:>+8.1f}{hi:>+8.1f}{std:>7.1f}{pos:>8}{best:>+10.1f}")

    pools = pegged_pools(conn, since)
    if pools:
        size = next((r[8] for r in pools if r[8]), None)
        big = f"rt ${size:,.0f}" if size else "rt big"
        print("\nPEGGED POOLS (who sets the price, and how thin each pool is; rt = round trip in bp)")
        print(f"{'pair':<14}{'pool':<16}{'% best bid':>11}{'% best ask':>11}{'rt $100':>10}{big:>10}{'filled':>8}")
        for pair, pool, _n, bw, aw, rt, rt_big, filled, _size in pools:
            fill = f"{filled:.0f}%" if filled is not None else "-"
            print(f"{pair:<14}{pool:<16}{bw:>10.1f}%{aw:>10.1f}%{fmt_bp(rt):>10}{fmt_bp(rt_big):>10}{fill:>8}")


if __name__ == "__main__":
    main()
