"""On-chain side: find pools, check tokens, and quote every pool in ONE eth_call per block.

How the batching works: every quoter call, plus the block number, base fee and
L1 fee, is packed into a single Multicall3.aggregate3 eth_call. Calls are
allowed to fail individually (a quoter reverts if a pool can't fill the size),
so one bad pool never kills the round.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import abi
from rpc import RpcError


@dataclass
class Pool:
    dex: str
    kind: str        # "uni" or "slip" (scanner DEXES); "classic" is recorder-only, see recorder.py
    tier: int        # fee tier (uni), tick spacing (slip), or 1 = stable / 0 = volatile (classic)
    address: str
    quoter: str

    @property
    def label(self) -> str:
        if self.kind == "classic":
            return f"{self.dex}-{'stable' if self.tier else 'vol'}"
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
class CrossQuoteRaw:
    """One pool between two non-USDC tokens (e.g. WETH/cbBTC), quoted both ways.
    a = first token, b = second token (see config.TRIANGLE_PAIRS)."""
    pool: Pool
    a_in_raw: int                   # a amount used for the a -> b quote
    b_in_raw: int                   # b amount used for the b -> a quote
    a_to_b_out_raw: int | None = None
    a_to_b_gas: int | None = None
    b_to_a_out_raw: int | None = None
    b_to_a_gas: int | None = None


@dataclass
class RoundData:
    block: int
    basefee_wei: int
    l1_fee_wei: int | None
    quotes: dict = field(default_factory=dict)   # pair name -> [PoolQuoteRaw]
    cross: list = field(default_factory=list)    # [CrossQuoteRaw] for the triangle's middle leg


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
    """calls: list of (target, calldata). Returns list of (success, return_bytes).

    A reply that is empty, cut off, or has the wrong number of answers raises RpcError:
    the provider sent something bad, so the round is skipped and retried (see scanner.RETRYABLE)."""
    if not calls:
        return []
    data = rpc.eth_call(multicall_addr, abi.encode_aggregate3(calls))
    try:
        results = abi.decode_aggregate3(data)
    except abi.DecodingError as e:
        raise RpcError(f"eth_call: multicall reply can't be decoded "
                       f"({len(data)} bytes, {type(e).__name__})") from e
    if len(results) != len(calls):
        raise RpcError(f"eth_call: multicall answered {len(results)} of {len(calls)} calls")
    return results


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


def discovery_dexes(dexes, extra_tiers):
    """Copies of DEXES with extra_tiers[kind] added to each DEX's own tiers (no duplicates,
    original order first). Used for the extra pairs' pool discovery only."""
    out = []
    for d in dexes:
        tiers = list(d["tiers"])
        for t in (extra_tiers or {}).get(d["kind"], []):
            if t not in tiers:
                tiers.append(t)
        out.append(dict(d, tiers=tiers))
    return out


def pair_quote(cfg, pair):
    """The token a pair's pools price against: pair["quote"], default USDC."""
    return pair.get("quote", cfg.USDC)


def quote_round(rpc, cfg, pair_pools, ref_prices, size_usd, cross=None, quote_usd=None):
    """pair_pools: {pair name: (pair cfg, [Pool])}. ref_prices: {pair name: USD price of base}.
    quote_usd: {pair name: USD value of one quote token} for pairs not quoted in USDC
    (missing = 1.0, i.e. USDC).

    For each pool we quote both directions:
      buy  = TRADE_SIZE worth of the quote token -> base
      sell = x_base -> quote token, where x_base = TRADE_SIZE / reference price
    routes.py rescales the sell quote to the exact amount a buy produced.

    cross (optional): (pair_a cfg, pair_b cfg, [Pool]) for pools between the two base
    tokens (WETH/cbBTC). Each is quoted a -> b and b -> a at about TRADE_SIZE worth,
    inside the SAME eth_call, so the triangle costs no extra RPC call.
    """
    quote_usd = quote_usd or {}
    calls = [
        (cfg.MULTICALL3, abi.encode_no_args(abi.SIG_BLOCK_NUMBER)),
        (cfg.MULTICALL3, abi.encode_no_args(abi.SIG_BASEFEE)),
        (cfg.GAS_PRICE_ORACLE, abi.encode_l1_fee_upper(cfg.L1_TX_SIZE_BYTES)),
    ]
    layout = []   # parallel to calls[3:], two entries per pool
    raws = {}
    for name, (pair, pools) in pair_pools.items():
        base, quote = pair["base"], pair_quote(cfg, pair)
        if not ref_prices.get(name) or not quote_usd.get(name, 1.0):
            continue              # no reference price this round: skip the pair, not the round
        quote_in = int(size_usd / quote_usd.get(name, 1.0) * 10 ** quote["decimals"])
        x_base_raw = int(size_usd / ref_prices[name] * 10 ** base["decimals"])
        raws[name] = []
        for p in pools:
            r = PoolQuoteRaw(pool=p, x_base_raw=x_base_raw)
            raws[name].append(r)
            calls.append((p.quoter, abi.encode_quote(p.kind, quote["address"], base["address"], quote_in, p.tier)))
            layout.append((r, "buy"))
            calls.append((p.quoter, abi.encode_quote(p.kind, base["address"], quote["address"], x_base_raw, p.tier)))
            layout.append((r, "sell"))

    cross_raws = []
    if cross:
        pair_a, pair_b, cross_pools = cross
        ta, tb = pair_a["base"], pair_b["base"]
        a_in = int(size_usd / ref_prices[pair_a["name"]] * 10 ** ta["decimals"])
        b_in = int(size_usd / ref_prices[pair_b["name"]] * 10 ** tb["decimals"])
        for p in cross_pools:
            c = CrossQuoteRaw(pool=p, a_in_raw=a_in, b_in_raw=b_in)
            cross_raws.append(c)
            calls.append((p.quoter, abi.encode_quote(p.kind, ta["address"], tb["address"], a_in, p.tier)))
            layout.append((c, "a_to_b"))
            calls.append((p.quoter, abi.encode_quote(p.kind, tb["address"], ta["address"], b_in, p.tier)))
            layout.append((c, "b_to_a"))

    results = multicall(rpc, cfg.MULTICALL3, calls)
    (ok_b, ret_b), (ok_f, ret_f), (ok_l1, ret_l1) = results[:3]
    if not (ok_b and ok_f and len(ret_b) >= 32 and len(ret_f) >= 32):
        raise RuntimeError("multicall could not read block number / base fee")
    rd = RoundData(
        block=abi.decode_uint(ret_b),
        basefee_wei=abi.decode_uint(ret_f),
        l1_fee_wei=abi.decode_uint(ret_l1) if ok_l1 and len(ret_l1) >= 32 else None,
        quotes=raws,
        cross=cross_raws,
    )
    for (r, side), (ok, ret) in zip(layout, results[3:]):
        if not ok or len(ret) < 128:
            continue
        out, gas = abi.decode_quote(ret)
        if out == 0:
            continue
        if side == "buy":
            r.buy_out_raw, r.buy_gas = out, gas
        elif side == "sell":
            r.sell_out_raw, r.sell_gas = out, gas
        elif side == "a_to_b":
            r.a_to_b_out_raw, r.a_to_b_gas = out, gas
        else:
            r.b_to_a_out_raw, r.b_to_a_gas = out, gas
    return rd


def leg_gas_usd(gas_units, basefee_wei, priority_wei, overhead, l1_fee_wei, fallback_l1_usd, eth_usd):
    """Cost of one swap transaction on Base, in USD: L2 execution + L1 data fee."""
    l2_wei = (gas_units + overhead) * (basefee_wei + priority_wei)
    l2_usd = l2_wei / 1e18 * eth_usd
    l1_usd = l1_fee_wei / 1e18 * eth_usd if l1_fee_wei is not None else fallback_l1_usd
    return l2_usd + l1_usd


def to_dex_quotes(cfg, rd: RoundData, pair, eth_usd, quote_usd=1.0):
    """Convert one pair's raw quotes to DexQuote (human units, USD gas).
    quote_usd: USD value of one quote token (1.0 for USDC); sell amounts come back in USD."""
    base_dec, usdc_dec = pair["base"]["decimals"], pair_quote(cfg, pair)["decimals"]

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
            sell_usd_out=r.sell_out_raw / 10 ** usdc_dec * quote_usd if r.sell_out_raw else None,
            buy_gas_usd=gas(r.buy_gas),
            sell_gas_usd=gas(r.sell_gas),
        ))
    return out


def read_tokens(rpc, multicall_addr: str, tokens):
    """[(symbol, decimals) or None] per token, read from the chain (None = no reply)."""
    calls = []
    for t in tokens:
        calls.append((t["address"], abi.encode_no_args(abi.SIG_SYMBOL)))
        calls.append((t["address"], abi.encode_no_args(abi.SIG_DECIMALS)))
    results = multicall(rpc, multicall_addr, calls)
    out = []
    for i in range(len(tokens)):
        (ok_s, ret_s), (ok_d, ret_d) = results[2 * i], results[2 * i + 1]
        if not (ok_s and ok_d and ret_s and ret_d):
            out.append(None)
            continue
        try:
            out.append((abi.decode_string(ret_s), abi.decode_uint(ret_d)))
        except Exception:
            out.append(None)
    return out


def median_price(prices):
    """Median of the non-None values, or None."""
    vals = sorted(p for p in prices if p)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def pool_buy_prices(rpc, cfg, pair, pools, quote_usd, size_usd):
    """{pool label: USD paid per base token} for a size_usd buy on each pool (None = no fill).
    One eth_call. Used at startup to get a first reference price and spot off-market pools."""
    base, quote = pair["base"], pair_quote(cfg, pair)
    quote_in = int(size_usd / quote_usd * 10 ** quote["decimals"])
    calls = [(p.quoter, abi.encode_quote(p.kind, quote["address"], base["address"], quote_in, p.tier))
             for p in pools]
    out = {}
    for p, (ok, ret) in zip(pools, multicall(rpc, cfg.MULTICALL3, calls)):
        got = None
        if ok and len(ret) >= 128:
            amount, _gas = abi.decode_quote(ret)
            if amount:
                got = size_usd / (amount / 10 ** base["decimals"])
        out[p.label] = got
    return out


def dex_mid(quotes, size_usd):
    """Median mid price (USD per base) across pools from one round's DexQuotes:
    mid of the buy price (size / base out) and sell price (USD out / x_base) per pool."""
    mids = []
    for q in quotes:
        if q.buy_base_out and q.sell_usd_out and q.x_base:
            mids.append((size_usd / q.buy_base_out + q.sell_usd_out / q.x_base) / 2)
    return median_price(mids)
