"""On-chain side: find pools, check tokens, and quote every pool in ONE eth_call per block.

How the batching works: every quoter call, plus the block number, base fee and
L1 fee, is packed into a single Multicall3.aggregate3 eth_call. Calls are
allowed to fail individually (a quoter reverts if a pool can't fill the size),
so one bad pool never kills the round.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import abi


@dataclass
class Pool:
    dex: str
    kind: str        # "uni" or "slip"
    tier: int        # fee tier (uni) or tick spacing (slip)
    address: str
    quoter: str

    @property
    def label(self) -> str:
        return f"{self.dex}-{'ts' if self.kind == 'slip' else ''}{self.tier}"


@dataclass
class PoolQuoteRaw:
    pool: Pool
    x_base_raw: int                 # base amount used for the sell-side quote
    buy_out_raw: int | None = None  # base received for TRADE_SIZE USDC
    buy_gas: int | None = None
    sell_out_raw: int | None = None # USDC received for x_base_raw base
    sell_gas: int | None = None


@dataclass
class RoundData:
    block: int
    basefee_wei: int
    l1_fee_wei: int | None
    quotes: dict = field(default_factory=dict)   # pair name -> [PoolQuoteRaw]


@dataclass
class DexQuote:
    """One pool's quotes, converted to human units for routes.py."""
    label: str
    x_base: float                   # base units used for the sell quote
    buy_base_out: float | None      # base received for the trade size in USDC
    sell_usd_out: float | None      # USDC received for selling x_base
    buy_gas_usd: float
    sell_gas_usd: float


def multicall(rpc, multicall_addr: str, calls):
    """calls: list of (target, calldata). Returns list of (success, return_bytes)."""
    if not calls:
        return []
    data = rpc.eth_call(multicall_addr, abi.encode_aggregate3(calls))
    return abi.decode_aggregate3(data)


def verify_tokens(rpc, multicall_addr: str, tokens):
    """tokens: list of dicts with address/symbol/decimals. Returns list of problem strings."""
    calls = []
    for t in tokens:
        calls.append((t["address"], abi.encode_no_args(abi.SIG_SYMBOL)))
        calls.append((t["address"], abi.encode_no_args(abi.SIG_DECIMALS)))
    results = multicall(rpc, multicall_addr, calls)
    problems = []
    for i, t in enumerate(tokens):
        (ok_s, ret_s), (ok_d, ret_d) = results[2 * i], results[2 * i + 1]
        if not (ok_s and ok_d and ret_s and ret_d):
            problems.append(f"{t['symbol']} at {t['address']}: no response (wrong address?)")
            continue
        sym, dec = abi.decode_string(ret_s), abi.decode_uint(ret_d)
        if sym != t["symbol"] or dec != t["decimals"]:
            problems.append(f"{t['address']}: expected {t['symbol']}/{t['decimals']}, chain says {sym}/{dec}")
    return problems


def discover_pools(rpc, multicall_addr: str, dexes, token_a: str, token_b: str):
    """Ask each factory for the pool at each tier. Missing pools are skipped."""
    wanted = [(d, tier) for d in dexes for tier in d["tiers"]]
    calls = [(d["factory"], abi.encode_get_pool(d["kind"], token_a, token_b, tier)) for d, tier in wanted]
    pools = []
    for (d, tier), (ok, ret) in zip(wanted, multicall(rpc, multicall_addr, calls)):
        if not ok or len(ret) < 32:
            continue
        addr = abi.decode_address(ret)
        if addr != abi.ZERO_ADDRESS:
            pools.append(Pool(d["name"], d["kind"], tier, addr, d["quoter"]))
    return pools


def quote_round(rpc, cfg, pair_pools, ref_prices, size_usd):
    """pair_pools: {pair name: (pair cfg, [Pool])}. ref_prices: {pair name: USD price}.

    For each pool we quote both directions:
      buy  = TRADE_SIZE USDC -> base
      sell = x_base -> USDC, where x_base = TRADE_SIZE / reference price
    routes.py rescales the sell quote to the exact amount a buy produced.
    """
    usdc = cfg.USDC
    calls = [
        (cfg.MULTICALL3, abi.encode_no_args(abi.SIG_BLOCK_NUMBER)),
        (cfg.MULTICALL3, abi.encode_no_args(abi.SIG_BASEFEE)),
        (cfg.GAS_PRICE_ORACLE, abi.encode_l1_fee_upper(cfg.L1_TX_SIZE_BYTES)),
    ]
    usdc_in = int(size_usd * 10 ** usdc["decimals"])
    layout = []   # parallel to calls[3:], two entries per pool
    raws = {}
    for name, (pair, pools) in pair_pools.items():
        base = pair["base"]
        x_base_raw = int(size_usd / ref_prices[name] * 10 ** base["decimals"])
        raws[name] = []
        for p in pools:
            r = PoolQuoteRaw(pool=p, x_base_raw=x_base_raw)
            raws[name].append(r)
            calls.append((p.quoter, abi.encode_quote(p.kind, usdc["address"], base["address"], usdc_in, p.tier)))
            layout.append((r, "buy"))
            calls.append((p.quoter, abi.encode_quote(p.kind, base["address"], usdc["address"], x_base_raw, p.tier)))
            layout.append((r, "sell"))

    results = multicall(rpc, cfg.MULTICALL3, calls)
    (ok_b, ret_b), (ok_f, ret_f), (ok_l1, ret_l1) = results[:3]
    if not (ok_b and ok_f):
        raise RuntimeError("multicall could not read block number / base fee")
    rd = RoundData(
        block=abi.decode_uint(ret_b),
        basefee_wei=abi.decode_uint(ret_f),
        l1_fee_wei=abi.decode_uint(ret_l1) if ok_l1 and len(ret_l1) >= 32 else None,
        quotes=raws,
    )
    for (r, side), (ok, ret) in zip(layout, results[3:]):
        if not ok or len(ret) < 128:
            continue
        out, gas = abi.decode_quote(ret)
        if out == 0:
            continue
        if side == "buy":
            r.buy_out_raw, r.buy_gas = out, gas
        else:
            r.sell_out_raw, r.sell_gas = out, gas
    return rd


def leg_gas_usd(gas_units, basefee_wei, priority_wei, overhead, l1_fee_wei, fallback_l1_usd, eth_usd):
    """Cost of one swap transaction on Base, in USD: L2 execution + L1 data fee."""
    l2_wei = (gas_units + overhead) * (basefee_wei + priority_wei)
    l2_usd = l2_wei / 1e18 * eth_usd
    l1_usd = l1_fee_wei / 1e18 * eth_usd if l1_fee_wei is not None else fallback_l1_usd
    return l2_usd + l1_usd


def to_dex_quotes(cfg, rd: RoundData, pair, eth_usd):
    """Convert one pair's raw quotes to DexQuote (human units, USD gas)."""
    base_dec, usdc_dec = pair["base"]["decimals"], cfg.USDC["decimals"]

    def gas(units):
        if units is None:
            return 0.0
        return leg_gas_usd(units, rd.basefee_wei, cfg.GAS_PRIORITY_WEI, cfg.SWAP_GAS_OVERHEAD,
                           rd.l1_fee_wei, cfg.FALLBACK_L1_FEE_USD, eth_usd)

    out = []
    for r in rd.quotes.get(pair["name"], []):
        out.append(DexQuote(
            label=r.pool.label,
            x_base=r.x_base_raw / 10 ** base_dec,
            buy_base_out=r.buy_out_raw / 10 ** base_dec if r.buy_out_raw else None,
            sell_usd_out=r.sell_out_raw / 10 ** usdc_dec if r.sell_out_raw else None,
            buy_gas_usd=gas(r.buy_gas),
            sell_gas_usd=gas(r.sell_gas),
        ))
    return out
