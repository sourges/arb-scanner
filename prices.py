"""Price rows: what each venue's price was at every scan. READ-ONLY, no extra RPC calls.

The scanner's other tables store results (was a route profitable, for how long).
This one stores the prices behind them, one row per pair per scan, so later you can
ask things the results can't answer:
    - when Kraken moves, how many seconds until the Base pools follow?
    - was a CEX gap real, or was the order book a few seconds old?
    - what would the routes have made with different fees?

What goes in a row (all from data the scan already has):
    kraken_bid / kraken_ask       best bid and ask on Kraken (None if no fresh book)
    kraken_age                    seconds between fetching that book and this scan
    coinbase_bid / _ask / _age    same for Coinbase
    dex_buy_px,  dex_buy_pool     cheapest pool to BUY on:  USDC paid per ETH/BTC
    dex_sell_px, dex_sell_pool    best pool to SELL on:     USDC received per ETH/BTC
DEX prices are for the trade size ($100) with the pool fee and slippage inside, and
no gas. On one pool buy_px is above sell_px (you pay the fee both ways). Across
pools, dex_sell_px above dex_buy_px is a dex>dex gap before gas.

Shared-format columns (same names as every new collector):
    raw_edge = DEX mid / CEX mid - 1, in %     where DEX mid = (dex_buy_px + dex_sell_px) / 2
               and CEX mid is Kraken's, or Coinbase's if Kraken has no fresh book.
               Positive = the pools are priced above the exchange.
    net_edge = always NULL: no costs are applied here, this table is prices only.
"""
from __future__ import annotations

from dataclasses import dataclass

STRATEGY = "px"
VENUE = "base"


@dataclass
class PriceRow:
    pair: str
    size_usd: float
    kraken_bid: float | None = None
    kraken_ask: float | None = None
    kraken_age: float | None = None
    coinbase_bid: float | None = None
    coinbase_ask: float | None = None
    coinbase_age: float | None = None
    dex_buy_px: float | None = None
    dex_buy_pool: str | None = None
    dex_sell_px: float | None = None
    dex_sell_pool: str | None = None

    @property
    def cex_mid(self):
        """Kraken's mid, or Coinbase's if Kraken has no fresh two-sided book."""
        for bid, ask in ((self.kraken_bid, self.kraken_ask), (self.coinbase_bid, self.coinbase_ask)):
            if bid and ask:
                return (bid + ask) / 2
        return None

    @property
    def dex_mid(self):
        if not self.dex_buy_px or not self.dex_sell_px:
            return None
        return (self.dex_buy_px + self.dex_sell_px) / 2

    @property
    def raw_edge(self):
        """How far the pools sit from the exchange, in %. None if either side is missing."""
        if self.dex_mid is None or self.cex_mid is None:
            return None
        return (self.dex_mid / self.cex_mid - 1) * 100

    @property
    def empty(self) -> bool:
        return self.cex_mid is None and self.dex_buy_px is None and self.dex_sell_px is None


def best_dex_prices(quotes, size_usd):
    """(buy_px, buy_pool, sell_px, sell_pool) across one pair's DexQuotes.
    buy  = lowest  USDC per base among pools that quoted a buy
    sell = highest USDC per base among pools that quoted a sell"""
    buy_px = buy_pool = sell_px = sell_pool = None
    for q in quotes:
        if q.buy_base_out:
            px = size_usd / q.buy_base_out
            if buy_px is None or px < buy_px:
                buy_px, buy_pool = px, q.label
        if q.sell_usd_out and q.x_base > 0:
            px = q.sell_usd_out / q.x_base
            if sell_px is None or px > sell_px:
                sell_px, sell_pool = px, q.label
    return buy_px, buy_pool, sell_px, sell_pool


def build(pair, size_usd, quotes, books, now):
    """One PriceRow for one pair. books: {"kraken": Book|None, "coinbase": Book|None}
    (None = no fresh book, so that venue's columns stay None)."""
    row = PriceRow(pair, size_usd)
    for venue in ("kraken", "coinbase"):
        book = books.get(venue)
        if book is None:
            continue
        setattr(row, f"{venue}_bid", book.best_bid)
        setattr(row, f"{venue}_ask", book.best_ask)
        setattr(row, f"{venue}_age", now - book.fetched_at)
    row.dex_buy_px, row.dex_buy_pool, row.dex_sell_px, row.dex_sell_pool = best_dex_prices(quotes, size_usd)
    return row
