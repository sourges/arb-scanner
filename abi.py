"""Hand-rolled ABI encoding/decoding for the few contract calls the scanner makes.

Using eth_abi directly (instead of web3.py contract objects) keeps the
dependency list small and avoids web3 version differences.
"""
from eth_abi import decode, encode
from eth_hash.auto import keccak


def selector(signature: str) -> bytes:
    """First 4 bytes of keccak256(signature), e.g. 'getPool(address,address,uint24)'."""
    return keccak(signature.encode())[:4]


def _addr(a: str) -> str:
    # eth_abi validates checksums on mixed-case input; lowercase sidesteps that.
    return a.lower()


# ---- signatures -------------------------------------------------------------

SIG_AGGREGATE3 = "aggregate3((address,bool,bytes)[])"
SIG_GET_POOL_UNI = "getPool(address,address,uint24)"        # Uniswap v3 / PancakeSwap v3
SIG_GET_POOL_SLIP = "getPool(address,address,int24)"        # Aerodrome Slipstream
SIG_QUOTE_UNI = "quoteExactInputSingle((address,address,uint256,uint24,uint160))"
SIG_QUOTE_SLIP = "quoteExactInputSingle((address,address,uint256,int24,uint160))"
SIG_SYMBOL = "symbol()"
SIG_DECIMALS = "decimals()"
SIG_BLOCK_NUMBER = "getBlockNumber()"                       # Multicall3 helper
SIG_BASEFEE = "getBasefee()"                                # Multicall3 helper
SIG_L1_FEE_UPPER = "getL1FeeUpperBound(uint256)"            # Base GasPriceOracle


# ---- encoders -----------------------------------------------------------------

def encode_get_pool(kind: str, token_a: str, token_b: str, tier: int) -> bytes:
    if kind == "uni":
        sig, t = SIG_GET_POOL_UNI, "uint24"
    elif kind == "slip":
        sig, t = SIG_GET_POOL_SLIP, "int24"
    else:
        raise ValueError(f"unknown dex kind {kind!r}")
    return selector(sig) + encode(["address", "address", t], [_addr(token_a), _addr(token_b), tier])


def encode_quote(kind: str, token_in: str, token_out: str, amount_in: int, tier: int) -> bytes:
    """QuoterV2.quoteExactInputSingle. Note the struct field ORDER: tokenIn, tokenOut,
    amountIn, fee/tickSpacing, sqrtPriceLimitX96 (0 = no limit)."""
    if kind == "uni":
        sig, tup = SIG_QUOTE_UNI, "(address,address,uint256,uint24,uint160)"
    elif kind == "slip":
        sig, tup = SIG_QUOTE_SLIP, "(address,address,uint256,int24,uint160)"
    else:
        raise ValueError(f"unknown dex kind {kind!r}")
    args = (_addr(token_in), _addr(token_out), int(amount_in), tier, 0)
    return selector(sig) + encode([tup], [args])


def encode_no_args(signature: str) -> bytes:
    return selector(signature)


def encode_l1_fee_upper(tx_size_bytes: int) -> bytes:
    return selector(SIG_L1_FEE_UPPER) + encode(["uint256"], [tx_size_bytes])


def encode_aggregate3(calls) -> bytes:
    """calls: list of (target, calldata_bytes). Every call allows failure."""
    payload = [(_addr(t), True, data) for t, data in calls]
    return selector(SIG_AGGREGATE3) + encode(["(address,bool,bytes)[]"], [payload])


# ---- decoders -----------------------------------------------------------------

def decode_aggregate3(data: bytes):
    """Returns list of (success, return_bytes)."""
    (results,) = decode(["(bool,bytes)[]"], data)
    return [(bool(ok), bytes(ret)) for ok, ret in results]


def decode_quote(data: bytes):
    """QuoterV2 returns (amountOut, sqrtPriceX96After, initializedTicksCrossed, gasEstimate)."""
    amount_out, _sqrt, _ticks, gas_est = decode(["uint256", "uint160", "uint32", "uint256"], data)
    return int(amount_out), int(gas_est)


def decode_address(data: bytes) -> str:
    (a,) = decode(["address"], data)
    return a.lower()


def decode_uint(data: bytes) -> int:
    (v,) = decode(["uint256"], data)
    return int(v)


def decode_string(data: bytes) -> str:
    (s,) = decode(["string"], data)
    return s


ZERO_ADDRESS = "0x" + "0" * 40
