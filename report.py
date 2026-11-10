"""Summarize what the scanner logged.

Run:  python report.py                      whole history
      python report.py --hours 24           last 24 hours only
      python report.py --min-usd 0.10       count only episodes that reached $0.10
      python report.py --top 30             list the 30 best episodes (default 15)

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


if __name__ == "__main__":
    main()
