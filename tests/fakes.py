"""A fake Base chain and fake CEX books for offline tests.

FakeChain answers Multicall3.aggregate3 by dispatching each inner call to
simple Python handlers, so dex.py and scanner.py run their real encode/decode
paths without a network.
"""
from types import SimpleNamespace

from eth_abi import decode, encode

import abi
import config
from cex import Book

MULTICALL = config.MULTICALL3.lower()
ORACLE = config.GAS_PRICE_ORACLE.lower()
USDC = config.USDC["address"].lower()
WETH = config.PAIRS[0]["base"]["address"].lower()
CBBTC = config.PAIRS[1]["base"]["address"].lower()
WBTC = config.WBTC["address"].lower()
USDT = config.USDT["address"].lower()

UNI_FACTORY = "0x" + "a1" * 20
UNI_QUOTER = "0x" + "a2" * 20
SLIP_FACTORY = "0x" + "b1" * 20
SLIP_QUOTER = "0x" + "b2" * 20

SEL = {name: abi.selector(sig) for name, sig in {
    "agg": abi.SIG_AGGREGATE3, "pool_uni": abi.SIG_GET_POOL_UNI, "pool_slip": abi.SIG_GET_POOL_SLIP,
    "q_uni": abi.SIG_QUOTE_UNI, "q_slip": abi.SIG_QUOTE_SLIP, "symbol": abi.SIG_SYMBOL,
    "decimals": abi.SIG_DECIMALS, "block": abi.SIG_BLOCK_NUMBER, "basefee": abi.SIG_BASEFEE,
    "l1": abi.SIG_L1_FEE_UPPER}.items()}

DECIMALS = {USDC: 6, WETH: 18, CBBTC: 8, WBTC: 8, USDT: 6}


class Revert(Exception):
    pass


class FakeChain:
    def __init__(self):
        self.block = 100
        self.basefee = 10_000_000          # 0.01 gwei
        self.l1_fee = 1_000_000_000_000    # 1e12 wei
        self.l1_fails = False
        # USDbC and DAI are deliberately unknown here: the recorder must skip those pairs.
        self.tokens = {USDC: ("USDC", 6), WETH: ("WETH", 18), CBBTC: ("cbBTC", 8),
                       WBTC: ("WBTC", 8), USDT: ("USDT", 6)}
        # (factory, tier) -> pool address
        self.pools = {
            (UNI_FACTORY, 500): "0x" + "c1" * 20,
            (SLIP_FACTORY, 100): "0x" + "c2" * 20,
        }
        # (quoter, tier, base token) -> (usd price, fee fraction); missing = revert
        self.prices = {
            (UNI_QUOTER, 500, WETH): (2000.0, 0.0005),
            (SLIP_QUOTER, 100, WETH): (2000.0, 0.0005),
            (UNI_QUOTER, 500, CBBTC): (60000.0, 0.0005),
            (SLIP_QUOTER, 100, CBBTC): (60000.0, 0.0005),
        }
        # pegged pools: (quoter, tier, token A, token B) -> (price of A in B, fee fraction)
        self.pegged = {
            (UNI_QUOTER, 500, CBBTC, WBTC): (0.9998, 0.0001),
            (SLIP_QUOTER, 100, CBBTC, WBTC): (1.0003, 0.0001),
            (UNI_QUOTER, 500, USDT, USDC): (1.0, 0.0001),      # no USDT pool on aero: reverts
        }
        self.quote_gas = 100_000
        self.calls = 0

    # -- RpcClient interface --
    def get_code(self, address):
        return b"\x60" if address.lower() == MULTICALL else b""

    def eth_call(self, to, data, block="latest"):
        assert to.lower() == MULTICALL and data[:4] == SEL["agg"]
        self.calls += 1
        (inner,) = decode(["(address,bool,bytes)[]"], data[4:])
        out = []
        for target, _allow, cd in inner:
            try:
                out.append((True, self.handle(target.lower(), bytes(cd))))
            except Revert:
                out.append((False, b""))
        return encode(["(bool,bytes)[]"], [out])

    # -- dispatch --
    def handle(self, target, cd):
        sel, args = cd[:4], cd[4:]
        if target == MULTICALL and sel == SEL["block"]:
            return encode(["uint256"], [self.block])
        if target == MULTICALL and sel == SEL["basefee"]:
            return encode(["uint256"], [self.basefee])
        if target == ORACLE and sel == SEL["l1"]:
            if self.l1_fails:
                raise Revert()
            return encode(["uint256"], [self.l1_fee])
        if target in self.tokens and sel == SEL["symbol"]:
            return encode(["string"], [self.tokens[target][0]])
        if target in self.tokens and sel == SEL["decimals"]:
            return encode(["uint8"], [self.tokens[target][1]])
        if sel in (SEL["pool_uni"], SEL["pool_slip"]):
            t = "uint24" if sel == SEL["pool_uni"] else "int24"
            _a, _b, tier = decode(["address", "address", t], args)
            return encode(["address"], [self.pools.get((target, tier), abi.ZERO_ADDRESS)])
        if sel in (SEL["q_uni"], SEL["q_slip"]):
            t = "uint24" if sel == SEL["q_uni"] else "int24"
            ((tin, tout, amount, tier, _limit),) = decode([f"(address,address,uint256,{t},uint160)"], args)
            tin, tout = tin.lower(), tout.lower()
            if (target, tier, tin, tout) in self.pegged:      # selling A for B
                price, fee = self.pegged[(target, tier, tin, tout)]
                out = amount / 10 ** DECIMALS[tin] * price * (1 - fee) * 10 ** DECIMALS[tout]
                return encode(["uint256", "uint160", "uint32", "uint256"], [int(out), 0, 0, self.quote_gas])
            if (target, tier, tout, tin) in self.pegged:      # buying A with B
                price, fee = self.pegged[(target, tier, tout, tin)]
                out = amount / 10 ** DECIMALS[tin] / price * (1 - fee) * 10 ** DECIMALS[tout]
                return encode(["uint256", "uint160", "uint32", "uint256"], [int(out), 0, 0, self.quote_gas])
            if USDC not in (tin, tout):
                raise Revert()
            base = tout if tin == USDC else tin
            key = (target, tier, base)
            if key not in self.prices:
                raise Revert()
            price, fee = self.prices[key]
            if tin == USDC:   # buy base
                out = amount / 10 ** 6 / price * (1 - fee) * 10 ** DECIMALS[base]
            else:             # sell base
                out = amount / 10 ** DECIMALS[base] * price * (1 - fee) * 10 ** 6
            return encode(["uint256", "uint160", "uint32", "uint256"], [int(out), 0, 0, self.quote_gas])
        raise Revert()


def make_config(**overrides):
    """config values with the fake chain's two DEXes."""
    cfg = SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})
    cfg.DEXES = [
        {"name": "uni", "kind": "uni", "factory": UNI_FACTORY, "quoter": UNI_QUOTER, "tiers": [100, 500]},
        {"name": "aero", "kind": "slip", "factory": SLIP_FACTORY, "quoter": SLIP_QUOTER, "tiers": [50, 100]},
    ]
    cfg.DB_PATH = ":memory:"
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def make_book(venue, mid, spread=1.0, size=10.0, now=None):
    b = Book(venue, [(mid - spread / 2, size)], [(mid + spread / 2, size)])
    if now is not None:
        b.fetched_at = now
    return b


class FakeFetchers:
    """Callable per venue returning books from a mutable dict {(venue, symbol): mid}."""

    def __init__(self, mids, clock):
        self.mids = mids
        self.clock = clock
        self.fail = set()

    def make(self, venue):
        def fetch(symbol):
            if venue in self.fail:
                raise ConnectionError(f"{venue} down")
            return make_book(venue, self.mids[(venue, symbol)], now=self.clock())
        return fetch

    def as_dict(self):
        return {"kraken": self.make("kraken"), "coinbase": self.make("coinbase")}
