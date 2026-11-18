"""Look up any pool on Base: its tokens, fee, tick spacing and which DEX made it.
Read-only, one RPC call. Uses BASE_RPC_URL from .env like the scanner.

Run (in arb-scanner, venv active):
    python find_pool.py 0x868eed6bad4bd368cd053b766f84cc727697589a
    python find_pool.py 0xPOOL1 0xPOOL2 ...

It also says whether the scanner would find that pool:
    "scanned"            the tier is in that DEX's DEXES tiers (all pairs)
    "extra pairs only"   the tier is only in EXTRA_DISCOVERY_TIERS
    "NOT scanned"        neither: add the tier to EXTRA_DISCOVERY_TIERS (or DEXES) to cover it
"""
from __future__ import annotations

import sys

import abi
import config
import dex
from rpc import RpcClient

FIELDS = ("token0", "token1", "fee", "tick_spacing", "factory", "stable")
SIGS = {"token0": abi.SIG_TOKEN0, "token1": abi.SIG_TOKEN1, "fee": abi.SIG_FEE,
        "tick_spacing": abi.SIG_TICK_SPACING, "factory": abi.SIG_FACTORY, "stable": abi.SIG_STABLE}
DECODE = {"token0": abi.decode_address, "token1": abi.decode_address, "fee": abi.decode_uint,
          "tick_spacing": abi.decode_int, "factory": abi.decode_address, "stable": abi.decode_bool}


def read_pool(rpc, cfg, address):
    """{field: value or None} for one pool. A call the pool doesn't support comes back None
    (classic pools have no tickSpacing, v3 pools have no stable)."""
    calls = [(address, abi.encode_no_args(SIGS[f])) for f in FIELDS]
    out = {}
    for f, (ok, ret) in zip(FIELDS, dex.multicall(rpc, cfg.MULTICALL3, calls)):
        try:
            out[f] = DECODE[f](ret) if ok and len(ret) >= 32 else None
        except Exception:
            out[f] = None
    return out


def which_dex(cfg, factory):
    """(name, kind, own tiers) for a known factory, else None."""
    if not factory:
        return None
    for d in cfg.DEXES:
        if d["factory"].lower() == factory.lower():
            return d["name"], d["kind"], list(d["tiers"])
    for d in getattr(cfg, "RECORD_CLASSIC_DEXES", []) or []:
        if d["factory"].lower() == factory.lower():
            return d["name"], "classic", []
    return None


def coverage(cfg, kind, tier, own_tiers):
    """'scanned', 'extra pairs only' or 'NOT scanned' for a pool's tier on a known DEX."""
    if kind == "classic":
        return "recorder only (classic pools aren't in the 2-second scan)"
    if tier in own_tiers:
        return "scanned"
    if tier in (getattr(cfg, "EXTRA_DISCOVERY_TIERS", {}) or {}).get(kind, []):
        return "extra pairs only"
    return "NOT scanned"


def describe(rpc, cfg, address):
    """Lines describing one pool."""
    info = read_pool(rpc, cfg, address)
    if info["token0"] is None or info["token1"] is None:
        return [f"{address}: not a pool (no token0/token1). Wrong address or wrong chain?"]
    toks = dex.read_tokens(rpc, cfg.MULTICALL3, [{"address": info["token0"]}, {"address": info["token1"]}])
    names = [f"{t[0]} ({t[1]} decimals)" if t else "unknown token" for t in toks]
    lines = [f"pool    {address}",
             f"token0  {info['token0']}  {names[0]}",
             f"token1  {info['token1']}  {names[1]}"]
    known = which_dex(cfg, info["factory"])
    dex_name = known[0] if known else f"unknown factory {info['factory']}"
    if info["stable"] is not None and info["tick_spacing"] is None:
        lines.append(f"type    classic {'stable' if info['stable'] else 'volatile'} pool, {dex_name}")
        if known:
            lines.append(f"scanner {coverage(cfg, 'classic', None, [])}")
        return lines
    fee = info["fee"]
    lines.append(f"fee     {fee / 10000:.4g}% (fee() = {fee})" if fee is not None else "fee     (no fee())")
    ts = info["tick_spacing"]
    lines.append(f"spacing {ts}" if ts is not None else "spacing (no tickSpacing())")
    lines.append(f"dex     {dex_name}")
    if known:
        name, kind, own = known
        tier = fee if kind == "uni" else ts
        label = f"{name}-{'ts' if kind == 'slip' else ''}{tier}"
        lines.append(f"scanner {label}: {coverage(cfg, kind, tier, own)}")
    return lines


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(__doc__)
        return 1
    rpc = RpcClient(config.RPC_URL, config.RPC_TIMEOUT_S)
    for i, address in enumerate(args):
        if i:
            print()
        for line in describe(rpc, config, address):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
