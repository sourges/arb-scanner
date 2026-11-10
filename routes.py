"""Turns one block's quotes and books into net profit for every route.

Every route is a round trip that starts and ends in USD(C):
    net = USD back - USD spent - CEX fee - gas - rebalance cost

DEX swap fees are already inside the quoter amounts, so they are never added again.

Route keys (13 per pair):
    dex>dex                          buy on the best DEX pool, sell on another
    <schedule>_<maker|taker>>dex     buy on the CEX, sell on the best DEX pool
    dex><schedule>_<maker|taker>     buy on the best DEX pool, sell on the CEX
where <schedule> is a key of config.CEX_FEES (kraken, kraken_reduced, coinbase).

Maker routes assume the resting order fills at the current best bid/ask. That is
optimistic (it may never fill), which is exactly what the scanner is here to test.
"""
from __future__ import annotations

from dataclasses import dataclass

import cex


@dataclass
class RouteResult:
    key: str
    pair: str
    buy_venue: str
    sell_venue: str
    size_usd: float
    usd_back: float       # before CEX fee, gas, rebalance
    cex_fee_usd: float
    gas_usd: float
    rebalance_usd: float
    base_amount: float = 0.0   # ETH/BTC bought on the buy leg and sold on the sell leg

    @property
    def buy_px(self):
        """Effective price paid per ETH/BTC on the buy leg (fees and slippage included)."""
        return self.size_usd / self.base_amount if self.base_amount else None

    @property
    def sell_px(self):
        """Effective price received per ETH/BTC on the sell leg (before CEX fee and gas)."""
        return self.usd_back / self.base_amount if self.base_amount else None

    @property
    def gross_usd(self) -> float:
        return self.usd_back - self.size_usd

    @property
    def net_usd(self) -> float:
        return self.gross_usd - self.cex_fee_usd - self.gas_usd - self.rebalance_usd

    @property
    def net_pct(self) -> float:
        return self.net_usd / self.size_usd * 100


def dex_sell_usd(q, base_amount: float):
    """USDC from selling `base_amount` on pool q, scaled from its quote for q.x_base.
    Linear scaling is accurate when base_amount is close to x_base (same trade size)."""
    if q.sell_usd_out is None or q.x_base <= 0:
        return None
    return q.sell_usd_out * (base_amount / q.x_base)


def best_dex_sell(quotes, base_amount: float):
    """Pool giving the most USD after gas for selling base_amount. Returns (quote, usd) or (None, None)."""
    best, best_usd = None, None
    for q in quotes:
        usd = dex_sell_usd(q, base_amount)
        if usd is None:
            continue
        if best is None or usd - q.sell_gas_usd > best_usd - best.sell_gas_usd:
            best, best_usd = q, usd
    return best, best_usd


def best_dex_buy(quotes):
    """Pool giving the most base for the trade size (gas compared via base value is
    second-order at these sizes, so pick on base out, ties broken by lower gas)."""
    candidates = [q for q in quotes if q.buy_base_out]
    if not candidates:
        return None
    return max(candidates, key=lambda q: (q.buy_base_out, -q.buy_gas_usd))


def dex_dex_route(pair: str, size: float, quotes):
    """Best buy-on-A / sell-on-B over all ordered pool pairs (A != B)."""
    best = None
    for a in quotes:
        if not a.buy_base_out:
            continue
        for b in quotes:
            if b is a:
                continue
            usd = dex_sell_usd(b, a.buy_base_out)
            if usd is None:
                continue
            r = RouteResult("dex>dex", pair, a.label, b.label, size, usd, 0.0,
                            a.buy_gas_usd + b.sell_gas_usd, 0.0, a.buy_base_out)
            if best is None or r.net_usd > best.net_usd:
                best = r
    return best


def cex_buy_dex_sell(pair, size, quotes, book, schedule, fees, side, rebalance):
    """Buy base on the CEX with `size` USD, sell it on the best DEX pool."""
    rate = fees[side]
    if side == "taker":
        base = cex.walk_buy(book.asks, size)
    else:  # maker: resting bid fills at the best bid
        base = size / book.best_bid if book.best_bid else None
    if not base:
        return None
    q, usd = best_dex_sell(quotes, base)
    if q is None:
        return None
    return RouteResult(f"{schedule}_{side}>dex", pair, f"{schedule}_{side}", q.label,
                       size, usd, size * rate, q.sell_gas_usd, rebalance, base)


def dex_buy_cex_sell(pair, size, quotes, book, schedule, fees, side, rebalance):
    """Buy base on the best DEX pool with `size` USDC, sell it on the CEX."""
    q = best_dex_buy(quotes)
    if q is None:
        return None
    base = q.buy_base_out
    rate = fees[side]
    if side == "taker":
        usd = cex.walk_sell(book.bids, base)
    else:  # maker: resting ask fills at the best ask
        usd = base * book.best_ask if book.best_ask else None
    if usd is None:
        return None
    return RouteResult(f"dex>{schedule}_{side}", pair, q.label, f"{schedule}_{side}",
                       size, usd, usd * rate, q.buy_gas_usd, rebalance, base)


def compute_routes(pair: str, size: float, quotes, books: dict, cex_fees: dict, rebalance: float):
    """books: {"kraken": Book|None, "coinbase": Book|None}. Returns {route key: RouteResult}."""
    results = {}
    r = dex_dex_route(pair, size, quotes)
    if r:
        results[r.key] = r
    for schedule, fees in cex_fees.items():
        book = books.get(fees["book"])
        if book is None:
            continue
        for side in ("maker", "taker"):
            for fn in (cex_buy_dex_sell, dex_buy_cex_sell):
                r = fn(pair, size, quotes, book, schedule, fees, side, rebalance)
                if r:
                    results[r.key] = r
    return results
