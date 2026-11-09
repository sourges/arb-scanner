"""Summarize what the scanner logged.

Run:  python report.py            whole history
      python report.py --hours 24 last 24 hours only
"""
import argparse
import sqlite3
import statistics
import time

import config


def route_summary(conn, since_ts):
    """Per route: how often net % was above zero, and how close it usually got."""
    rows = conn.execute(
        "SELECT pair, route, SUM(samples), SUM(positives), MAX(best_net_pct),"
        " SUM(avg_net_pct * samples) / SUM(samples)"
        " FROM minute_stats WHERE minute >= ? GROUP BY pair, route", (since_ts,)).fetchall()
    return sorted(rows, key=lambda r: -r[4])


def episode_summary(conn, since_ts):
    """Per route: how many profitable episodes, how long they lasted, best profit."""
    out = {}
    for pair, route, blocks, seconds, max_usd in conn.execute(
            "SELECT pair, route, blocks, seconds, max_net_usd FROM episodes WHERE start_ts >= ?", (since_ts,)):
        out.setdefault((pair, route), []).append((blocks, seconds, max_usd))
    summary = []
    for (pair, route), eps in out.items():
        summary.append((pair, route, len(eps),
                        statistics.median(e[0] for e in eps),
                        max(e[1] for e in eps),
                        sum(e[2] for e in eps)))
    return sorted(summary, key=lambda r: -r[5])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=None)
    args = ap.parse_args(argv)
    since = time.time() - args.hours * 3600 if args.hours else 0

    conn = sqlite3.connect(config.DB_PATH)
    print("ROUTES (from minute stats)")
    print(f"{'pair route':<30}{'samples':>9}{'% >0':>8}{'best %':>9}{'avg %':>9}")
    for pair, route, n, pos, best, avg in route_summary(conn, since):
        print(f"{pair + ' ' + route:<30}{n:>9}{100 * pos / n:>7.2f}%{best:>8.3f}%{avg:>8.3f}%")

    print("\nPROFITABLE EPISODES")
    print(f"{'pair route':<30}{'count':>7}{'med blocks':>12}{'max secs':>10}{'sum best $':>12}")
    eps = episode_summary(conn, since)
    if not eps:
        print("  none yet")
    for pair, route, count, med_blocks, max_secs, total in eps:
        print(f"{pair + ' ' + route:<30}{count:>7}{med_blocks:>12.1f}{max_secs:>10.0f}{total:>12.3f}")
    print("\n'sum best $' adds each episode's best moment once: an upper bound, not a P&L.")


if __name__ == "__main__":
    main()
