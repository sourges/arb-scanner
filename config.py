"""Scanner settings. Edit here on the main PC, push, then pull on the 24/7 PC.

The RPC URL (it contains your API key) lives in .env, which is NOT committed.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# ---- connection ---------------------------------------------------------------
# Free Alchemy/QuickNode Base URL goes in .env as BASE_RPC_URL.
# Fallback is Base's public endpoint: fine for a quick test, heavily rate-limited.
RPC_URL = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
RPC_TIMEOUT_S = 10

POLL_SECONDS = 2.0          # one scan every 2s. Base makes a block about every 0.2s, so this
                            # sees roughly every 10th block; faster polling uses more RPC credits
BOOK_MAX_AGE_S = 6.0        # ignore a CEX order book older than this
DB_PATH = "arb_scanner.sqlite"

# ---- trade size ---------------------------------------------------------------
TRADE_SIZE_USD = 100.0

# ---- CEX fees (fractions, 0.0022 = 0.22%) ---------------------------------------
# Each schedule is scanned separately so you can compare tiers side by side.
CEX_FEES = {
    "kraken":         {"book": "kraken",   "maker": 0.0030, "taker": 0.0060},
    "kraken_reduced": {"book": "kraken",   "maker": 0.0022, "taker": 0.0038},
    "coinbase":       {"book": "coinbase", "maker": 0.0035, "taker": 0.0075},
}

# Amortized cost per CEX<->DEX trade of moving funds back between venues
# (withdrawal fees, bridge costs). 0 until we measure it.
REBALANCE_COST_USD = 0.0

# ---- Base gas model -------------------------------------------------------------
GAS_PRIORITY_WEI = 1_000_000      # 0.001 gwei tip on top of the base fee
SWAP_GAS_OVERHEAD = 50_000        # router/tx overhead beyond the quoter's pool-only estimate
L1_TX_SIZE_BYTES = 300            # size used to ask Base's oracle for the L1 data fee bound
FALLBACK_L1_FEE_USD = 0.002       # used only if the oracle call fails

# ---- contracts (Base mainnet, chain id 8453) ---------------------------------------
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
GAS_PRICE_ORACLE = "0x420000000000000000000000000000000000000F"

USDC = {"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "symbol": "USDC", "decimals": 6}
CBBTC = {"address": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf", "symbol": "cbBTC", "decimals": 8}
WETH = {"address": "0x4200000000000000000000000000000000000006", "symbol": "WETH", "decimals": 18}

PAIRS = [
    {
        "name": "ETH",
        "base": WETH,
        "kraken": "ETHUSD",
        "coinbase": "ETH-USD",
    },
    {
        "name": "BTC",
        "base": CBBTC,
        "kraken": "XBTUSD",
        "coinbase": "BTC-USD",
    },
]

# ---- extra pairs (added Nov 18, 2026) ------------------------------------------------
# Less-watched pairs, scanned every 2 seconds for dex>dex like ETH and BTC, in the SAME
# eth_call (no extra RPC calls). Unlike PAIRS above they are optional: a token whose
# on-chain symbol doesn't match, or a pair with no pools, is skipped with a log line and
# the scanner runs on. decimals None = read from the chain at startup.
#   quote      the token the pools price against (default USDC). Sizes stay in USD:
#              "$100" of WETH is 100 / ETH price. Profit is converted back to USD.
#   quote_ref  which reference price gives the quote token's USD value ("ETH", "BTC").
#   base_ref   take the base token's USD price from that reference instead of a CEX book.
#   kraken / coinbase  USD markets for the base token (optional). They give the reference
#              price and the scan_prices columns. CEX routes only run for USDC-quoted
#              pairs; the others are dex>dex only.
# With no CEX book and no base_ref, the reference is the median pool price (DEX-derived).
# AERO: the token migrated to a new contract Nov 2-6, 2026 (Aerodrome + Velodrome -> Aero).
# This is the NEW contract; the legacy 0x9401...8631 is deliberately not used.
SOL = {"address": "0x311935Cd80B76769bF2ecC9D8Ab7635b2139cf82", "symbol": "SOL", "decimals": None}
AERO = {"address": "0x3f5bF3EdAc59d650D8fDf9657a71F2f904A3a4Ee", "symbol": "AERO", "decimals": None}
VIRTUAL = {"address": "0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b", "symbol": "VIRTUAL", "decimals": None}

EXTRA_PAIRS = [
    {"name": "ETH/BTC", "base": WETH, "quote": CBBTC, "base_ref": "ETH", "quote_ref": "BTC"},
    {"name": "SOL", "base": SOL, "kraken": "SOLUSD", "coinbase": "SOL-USD"},
    {"name": "AERO/ETH", "base": AERO, "quote": WETH, "quote_ref": "ETH",
     "kraken": "AEROUSD", "coinbase": "AERO-USD"},
    {"name": "VIRTUAL/ETH", "base": VIRTUAL, "quote": WETH, "quote_ref": "ETH",
     "coinbase": "VIRTUAL-USD"},
]

# Pool discovery for the extra pairs asks each factory about these tiers IN ADDITION to
# the DEX's own "tiers" in DEXES (added Nov 18, 2026). Smaller tokens often sit in 1% pools
# (10000 on Uniswap/PancakeSwap, tick spacing 2000 on Aerodrome), and Aerodrome can enable
# more tick spacings over time. ETH and BTC keep using DEXES' tiers only (unchanged).
# Costs a few lookups in one call at startup; nothing per scan. Every pool found still goes
# through the OFF_MARKET_PCT screen below. find_pool.py shows any pool's real fee/spacing.
EXTRA_DISCOVERY_TIERS = {
    "uni":  [10000],                    # 1% fee tier (Uniswap v3 and PancakeSwap v3)
    "slip": [10, 500, 1000, 2000],      # more Aerodrome Slipstream tick spacings
}

# At startup, an extra pair's pool whose $100 price is more than this % away from the
# pair's reference price is dropped as off-market (dead, abandoned or legacy pool), so it
# can't show up as a fake gap. Logged. Not applied to ETH and BTC (unchanged).
OFF_MARKET_PCT = 10.0

# ---- price rows (prices.py) -----------------------------------------------------------
# Every scan, one row per pair goes in scan_prices: Kraken and Coinbase best bid/ask, how
# old each book was, and the best pool buy and sell price. No extra RPC calls. It adds
# about 86,000 small rows a day (the same as tri_quotes). False turns it off.
PRICE_LOG = True

# ---- size probes (probe.py) ---------------------------------------------------------
# When a dex>dex episode OPENS at or above PROBE_MIN_NET_PCT (net, after gas, at $100),
# the same buy pool -> sell pool trade is quoted at each size below in ONE extra eth_call,
# to see how much money the gap could absorb. Rows go in episode_probes.
# PROBE_MAX_PER_HOUR caps the extra calls if the market goes wild. [] turns probing off.
PROBE_SIZES_USD = [100, 500, 1000, 2500, 5000]
PROBE_MIN_NET_PCT = 0.05
PROBE_ROUTES = ("dex>dex",)
PROBE_MAX_PER_HOUR = 60

# Triangle size probes (tri_probe.py): when a triangle loop's net edge at $100 reaches
# this % (after being below it the scan before), its three pools are re-quoted at
# PROBE_SIZES_USD in one extra eth_call. Rows go in tri_probes. None turns triangle
# probes off. Triangle probes have their OWN hourly cap (since Nov 18, 2026), so a busy
# triangle hour can't use up the dex>dex probes' budget, and vice versa.
TRI_PROBE_MIN_NET_PCT = 0.02
TRI_PROBE_MAX_PER_HOUR = 60

# ---- triangular arb (triangle.py) ------------------------------------------------
# The two PAIRS names that form the triangle with USDC: USDC -> WETH -> cbBTC -> USDC
# and the reverse. Their cross pools (WETH/cbBTC) are found at startup and quoted in
# the same per-block eth_call. Every scan writes one row per loop to tri_quotes.
# Set to None to turn the triangle off.
TRIANGLE_PAIRS = ("ETH", "BTC")

# ---- Binance.US triangle (cex_triangle.py) ------------------------------------------
# Public order books, no API key. For each quote currency: Q -> BTC -> ETH -> Q and the
# reverse, sharing the ETHBTC book. Rows go in cex_tri_quotes. BUS_SECONDS = None turns it off.
# Rate limit: 1200 weight/min per IP, 5 per book: 7 books every 4s = ~525/min. Your own
# Binance.US trading from this PC uses the same limit, so don't go below 3s.
BUS_SECONDS = 4
BUS_SIZE_USD = 100.0
BUS_BOOK_LEVELS = 20
BUS_CROSS = "ETHBTC"
BUS_TRIANGLES = [
    {"quote": "USD",  "btc": "BTCUSD",  "eth": "ETHUSD"},
    {"quote": "USDT", "btc": "BTCUSDT", "eth": "ETHUSDT"},
    {"quote": "USDC", "btc": "BTCUSDC", "eth": "ETHUSDC"},
]
# Taker fee per symbol (fractions: 0.0002 = 0.02%). "default" covers any symbol not listed.
# Check your account's fee page; some pairs may be lower.
BUS_TAKER_FEES = {"default": 0.0002}
BUS_MAX_WEIGHT = 800        # skip a round when Binance says we've used this much of 1200
BUS_BACKOFF_S = 60          # pause at least this long after a 429/418

# ---- pegged-pair recorder (data for the stat-arb project) -----------------------
# Once a minute, each pair below is quoted both ways on every pool and one row per
# pool goes in the pegged_quotes table. Price is "B per 1 A", so it sits near 1.0.
# usd_ref: which PAIRS price gives the USD value of one token (None = $1, a stablecoin).
# A token whose on-chain symbol/decimals don't match is skipped with a log line;
# it does not stop the scanner.
RECORD_SECONDS = 60
RECORD_SIZE_USD = 100.0
# Each pool is also quoted at this size in the same call (depth_bid/depth_ask columns):
# a deep pool's $1,000 price is close to its $100 price; a thin pool's is much worse.
# None turns the depth quotes off.
RECORD_DEPTH_USD = 1000.0

WBTC = {"address": "0x0555E30da8f98308EdB960aa94C0Db47230d2B9c", "symbol": "WBTC", "decimals": 8}
USDT = {"address": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2", "symbol": "USDT", "decimals": 6}
USDBC = {"address": "0xd9aAEc86B65D86f6a7B5B1b0c42FFA531710b6CA", "symbol": "USDbC", "decimals": 6}
DAI = {"address": "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb", "symbol": "DAI", "decimals": 18}
# Staked ETH (added Nov 18, 2026). Both earn staking rewards, so their price in WETH sits
# ABOVE 1.0 and creeps up over time (mid shows as roughly +1,000 bp or more). stat-arb
# measures the spread against that rising line. Sizes use the ETH price, so "$100" of
# cbETH/wstETH is really a little more (one is worth more than 1 ETH); price ratios are unaffected.
# Addresses: cbETH and Lido's wstETH on Base (Coinbase / Lido bridged tokens).
CBETH = {"address": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22", "symbol": "cbETH", "decimals": 18}
WSTETH = {"address": "0xc1CBa3fCea344f92D9239c08C0568f6F2F0ee452", "symbol": "wstETH", "decimals": 18}

RECORD_PAIRS = [
    {"name": "cbBTC/WBTC", "a": CBBTC, "b": WBTC, "usd_ref": "BTC"},
    {"name": "USDT/USDC",  "a": USDT,  "b": USDC, "usd_ref": None},
    {"name": "USDbC/USDC", "a": USDBC, "b": USDC, "usd_ref": None},
    {"name": "DAI/USDC",   "a": DAI,   "b": USDC, "usd_ref": None},
    {"name": "cbETH/WETH",  "a": CBETH,  "b": WETH, "usd_ref": "ETH"},
    {"name": "wstETH/WETH", "a": WSTETH, "b": WETH, "usd_ref": "ETH"},
]

# Aerodrome classic (v2-style) pools, for the RECORDER ONLY: the 2-second scan, triangle
# and size probes use DEXES below and never see these. One stable pool and one volatile
# pool can exist per token pair; each is looked up with getPool(a, b, stable) and quoted
# with the pool's own getAmountOut (fee already taken out). Added Nov 14, 2026.
# Address from github.com/aerodrome-finance/contracts (deployment table).
RECORD_CLASSIC_DEXES = [
    {"name": "aeroc", "factory": "0x420DD381b31aEf6683db6B902084cB0FFECe40Da",
     "stable": [True, False]},
]

# kind "uni"  = Uniswap-v3-style pools keyed by fee tier (in hundredths of a bip: 500 = 0.05%)
# kind "slip" = Aerodrome Slipstream pools keyed by tick spacing
# Pools are looked up from each factory at startup; tiers with no pool are skipped.
DEXES = [
    {"name": "univ3", "kind": "uni",
     "factory": "0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
     "quoter":  "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
     "tiers": [100, 500, 3000]},
    {"name": "pcsv3", "kind": "uni",
     "factory": "0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865",
     "quoter":  "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997",
     "tiers": [100, 500, 2500]},
    # Aerodrome has three Slipstream factories live; each has its own quoter.
    {"name": "aero1", "kind": "slip",
     "factory": "0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A",
     "quoter":  "0x254cF9E1E6e233aa1AC962CB9B05b2cfeAaE15b0",
     "tiers": [1, 50, 100, 200]},
    {"name": "aero2", "kind": "slip",
     "factory": "0xaDe65c38CD4849aDBA595a4323a8C7DdfE89716a",
     "quoter":  "0x3d4C22254F86f64B7eC90ab8F7aeC1FBFD271c6C",
     "tiers": [1, 50, 100, 200]},
    {"name": "aero3", "kind": "slip",
     "factory": "0xf8f2eB4940CFE7d13603DDDD87f123820Fc061Ef",
     "quoter":  "0x514c8B5f54112481E28028F1166Bd78501089259",
     "tiers": [1, 50, 100, 200]},
]
