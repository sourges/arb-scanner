"""CEX side: public order books from Kraken and Coinbase, plus book-walking math.

No API keys needed; both endpoints are public. Books are (price, size) lists,
bids sorted high->low, asks low->high.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests

KRAKEN_DEPTH_URL = "https://api.kraken.com/0/public/Depth"
COINBASE_BOOK_URL = "https://api.coinbase.com/api/v3/brokerage/market/product_book"


@dataclass
class Book:
    venue: str
    bids: list            # [(price, size)], best (highest) first
    asks: list            # [(price, size)], best (lowest) first
    fetched_at: float = field(default_factory=time.time)

    @property
    def best_bid(self):
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self):
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self):
        if not self.bids or not self.asks:
            return None
        return (self.best_bid + self.best_ask) / 2


def parse_kraken(payload: dict) -> Book:
    if payload.get("error"):
        raise ValueError(f"kraken error: {payload['error']}")
    # Result key is Kraken's internal pair name (ETHUSD -> XETHZUSD), so take the only entry.
    (book,) = payload["result"].values()
    bids = [(float(p), float(s)) for p, s, *_ in book["bids"]]
    asks = [(float(p), float(s)) for p, s, *_ in book["asks"]]
    return Book("kraken", sorted(bids, reverse=True), sorted(asks))


def parse_coinbase(payload: dict) -> Book:
    pb = payload["pricebook"]
    bids = [(float(x["price"]), float(x["size"])) for x in pb["bids"]]
    asks = [(float(x["price"]), float(x["size"])) for x in pb["asks"]]
    return Book("coinbase", sorted(bids, reverse=True), sorted(asks))


def fetch_kraken(pair: str, session=None, timeout=5) -> Book:
    s = session or requests
    r = s.get(KRAKEN_DEPTH_URL, params={"pair": pair, "count": 100}, timeout=timeout)
    r.raise_for_status()
    return parse_kraken(r.json())


def fetch_coinbase(product: str, session=None, timeout=5) -> Book:
    s = session or requests
    r = s.get(COINBASE_BOOK_URL, params={"product_id": product, "limit": 100}, timeout=timeout)
    r.raise_for_status()
    return parse_coinbase(r.json())


def walk_buy(asks, usd: float):
    """Spend `usd` lifting asks. Returns base bought, or None if the book is too thin."""
    remaining, base = usd, 0.0
    for price, size in asks:
        level_cost = price * size
        if level_cost >= remaining:
            return base + remaining / price
        base += size
        remaining -= level_cost
    return None


def walk_sell(bids, base: float):
    """Sell `base` into bids. Returns USD received, or None if the book is too thin."""
    remaining, usd = base, 0.0
    for price, size in bids:
        if size >= remaining:
            return usd + remaining * price
        usd += price * size
        remaining -= size
    return None


class BookCache:
    """Keeps the last good book per (venue, symbol) so one failed fetch doesn't drop a venue."""

    def __init__(self, max_age_s: float):
        self.max_age_s = max_age_s
        self._books = {}

    def put(self, venue: str, symbol: str, book: Book):
        self._books[(venue, symbol)] = book

    def get(self, venue: str, symbol: str, now: float | None = None):
        book = self._books.get((venue, symbol))
        if book is None:
            return None
        now = time.time() if now is None else now
        return book if now - book.fetched_at <= self.max_age_s else None
