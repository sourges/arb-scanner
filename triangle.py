"""Triangular (three-way) arbitrage on Base. READ-ONLY: it only measures.

Two loops, both starting and ending in USDC (a = WETH, b = cbBTC by default):
    forward:  USDC -> a -> b -> USDC
    reverse:  USDC -> b -> a -> USDC

Every leg may use a different pool (Uniswap, PancakeSwap, Aerodrome...). For each
leg we take the pool with the best rate, chain the three rates, and compare the
USDC that comes back with the USDC that went in.

All three legs come from the scanner's ONE per-block eth_call:
    leg 1  USDC -> a    the pair's buy quote (exact: it was quoted for the trade size)
    leg 2  a -> b       the cross pool quote, scaled to the amount leg 1 produced
    leg 3  b -> USDC    the pair's sell quote, scaled to the amount leg 2 produced
Scaling assumes the rate is flat around $100, which holds in deep pools. A loop
that shows a profit should be re-checked with chained quotes before believing it.

Edges are in % of the trade size:
    raw_edge = USDC back / size - 1         (pool fees and slippage included, gas not)
    net_edge = (USDC back - gas) / size - 1
Gas counts each leg as its own transaction (its own overhead and L1 fee). A single
contract doing all three swaps would pay less, so net_edge is on the cautious side.
"""
from __future__ import annotations

from dataclasses import dataclass

import dex

STRATEGY = "tri_arb"
VENUE = "base"


@dataclass
class Leg:
    pool: str
    rate: float       # tokens out per token in (human units)
    gas_usd: float


@dataclass
class TriResult:
    path: str         # e.g. "USDC>WETH>cbBTC>USDC"
    size_usd: float
    usd_back: float
    gas_usd: float
    legs: tuple       # pool label per leg, in order

    @property
    def raw_edge(self) -> float:
        return (self.usd_back / self.size_usd - 1) * 100

    @property
    def net_edge(self) -> float:
        return ((self.usd_back - self.gas_usd) / self.size_usd - 1) * 100


def best(legs):
    """Highest-rate leg (ties go to lower gas), or None if no pool quoted."""
    legs = [x for x in legs if x is not None and x.rate > 0]
    if not legs:
        return None
    return max(legs, key=lambda x: (x.rate, -x.gas_usd))


def buy_legs(quotes, size_usd):
    """USDC -> base legs from DexQuotes. rate = base per USDC."""
    return [Leg(q.label, q.buy_base_out / size_usd, q.buy_gas_usd)
            for q in quotes if q.buy_base_out]


def sell_legs(quotes):
    """base -> USDC legs from DexQuotes. rate = USDC per base."""
    return [Leg(q.label, q.sell_usd_out / q.x_base, q.sell_gas_usd)
            for q in quotes if q.sell_usd_out and q.x_base > 0]


def cross_legs(cfg, rd, pair_a, pair_b, eth_usd):
    """(a -> b legs, b -> a legs) from the round's cross quotes."""
    dec_a, dec_b = pair_a["base"]["decimals"], pair_b["base"]["decimals"]

    def gas(units):
        if units is None:
            return 0.0
        return dex.leg_gas_usd(units, rd.basefee_wei, cfg.GAS_PRIORITY_WEI, cfg.SWAP_GAS_OVERHEAD,
                               rd.l1_fee_wei, cfg.FALLBACK_L1_FEE_USD, eth_usd)

    a_to_b, b_to_a = [], []
    for c in rd.cross:
        if c.a_to_b_out_raw and c.a_in_raw:
            rate = (c.a_to_b_out_raw / 10 ** dec_b) / (c.a_in_raw / 10 ** dec_a)
            a_to_b.append(Leg(c.pool.label, rate, gas(c.a_to_b_gas)))
        if c.b_to_a_out_raw and c.b_in_raw:
            rate = (c.b_to_a_out_raw / 10 ** dec_a) / (c.b_in_raw / 10 ** dec_b)
            b_to_a.append(Leg(c.pool.label, rate, gas(c.b_to_a_gas)))
    return a_to_b, b_to_a


def loop(path, size_usd, leg1, leg2, leg3):
    """Chain three best legs. None if any leg has no quote this block."""
    if leg1 is None or leg2 is None or leg3 is None:
        return None
    usd_back = size_usd * leg1.rate * leg2.rate * leg3.rate
    return TriResult(path, size_usd, usd_back, leg1.gas_usd + leg2.gas_usd + leg3.gas_usd,
                     (leg1.pool, leg2.pool, leg3.pool))


def compute(size_usd, sym_a, sym_b, quotes_a, quotes_b, a_to_b, b_to_a):
    """Both loops for one block. quotes_a / quotes_b: DexQuotes of the a/USDC and b/USDC pools."""
    out = []
    fwd = loop(f"USDC>{sym_a}>{sym_b}>USDC", size_usd,
               best(buy_legs(quotes_a, size_usd)), best(a_to_b), best(sell_legs(quotes_b)))
    rev = loop(f"USDC>{sym_b}>{sym_a}>USDC", size_usd,
               best(buy_legs(quotes_b, size_usd)), best(b_to_a), best(sell_legs(quotes_a)))
    for r in (fwd, rev):
        if r is not None:
            out.append(r)
    return out


def best_line(results):
    """Short text for the status line: the better loop's net edge."""
    if not results:
        return "TRI -"
    r = max(results, key=lambda x: x.net_edge)
    return f"TRI best {r.path} {r.net_edge:+.3f}%"


def print_table(results):
    print(f"{'triangle':<26}{'leg 1':<16}{'leg 2':<16}{'leg 3':<16}{'back $':>10}{'gas':>8}"
          f"{'raw %':>9}{'net %':>9}")
    if not results:
        print("(no triangle this block: a leg had no quote)")
    for r in sorted(results, key=lambda x: -x.net_edge):
        print(f"{r.path:<26}{r.legs[0]:<16}{r.legs[1]:<16}{r.legs[2]:<16}{r.usd_back:>10.3f}"
              f"{r.gas_usd:>8.4f}{r.raw_edge:>8.3f}%{r.net_edge:>8.3f}%")
