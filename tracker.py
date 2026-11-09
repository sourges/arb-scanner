"""Turns per-block route results into two things worth storing:

Episodes: a run of consecutive scanned blocks where a route's net was > 0.
    One row per episode: how long it lasted and how good it got. This is
    the "is anything left for a slow bot?" measurement.

Minute stats: per route per minute, best/average net % and how many samples
    were positive. This shows the near-misses and how far below break-even
    routes usually sit.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Episode:
    key: str
    pair: str
    start_block: int
    start_ts: float
    end_block: int
    end_ts: float
    samples: int
    max_net_usd: float
    max_net_pct: float
    best_buy: str
    best_sell: str

    @property
    def blocks(self) -> int:
        return self.end_block - self.start_block + 1

    @property
    def seconds(self) -> float:
        return self.end_ts - self.start_ts


class EpisodeTracker:
    def __init__(self):
        self.open = {}   # (pair, key) -> Episode

    def update(self, block: int, ts: float, results: dict):
        """results: {(pair, key): RouteResult} for this round. Returns closed episodes.
        A route that is missing this round (venue down, quote failed) closes its episode."""
        closed = []
        for k, ep in list(self.open.items()):
            r = results.get(k)
            if r is None or r.net_usd <= 0:
                closed.append(self.open.pop(k))
        for k, r in results.items():
            if r.net_usd <= 0:
                continue
            ep = self.open.get(k)
            if ep is None:
                self.open[k] = Episode(r.key, r.pair, block, ts, block, ts, 1,
                                       r.net_usd, r.net_pct, r.buy_venue, r.sell_venue)
            else:
                ep.end_block, ep.end_ts, ep.samples = block, ts, ep.samples + 1
                if r.net_usd > ep.max_net_usd:
                    ep.max_net_usd, ep.max_net_pct = r.net_usd, r.net_pct
                    ep.best_buy, ep.best_sell = r.buy_venue, r.sell_venue
        return closed

    def flush(self):
        closed = list(self.open.values())
        self.open.clear()
        return closed


@dataclass
class MinuteRow:
    minute: int          # unix time // 60 * 60
    pair: str
    key: str
    samples: int
    positives: int
    best_net_pct: float
    sum_net_pct: float

    @property
    def avg_net_pct(self) -> float:
        return self.sum_net_pct / self.samples


class MinuteStats:
    def __init__(self):
        self.minute = None
        self.rows = {}

    def add(self, ts: float, results: dict):
        """Returns finished MinuteRows when the minute rolls over, else []."""
        minute = int(ts // 60 * 60)
        done = []
        if self.minute is not None and minute != self.minute:
            done = self.flush()
        self.minute = minute
        for (pair, key), r in results.items():
            row = self.rows.get((pair, key))
            if row is None:
                row = self.rows[(pair, key)] = MinuteRow(minute, pair, key, 0, 0, r.net_pct, 0.0)
            row.samples += 1
            row.positives += r.net_usd > 0
            row.best_net_pct = max(row.best_net_pct, r.net_pct)
            row.sum_net_pct += r.net_pct
        return done

    def flush(self):
        done = list(self.rows.values())
        self.rows = {}
        return done
