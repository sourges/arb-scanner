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

POLL_SECONDS = 2.0          # Base makes a block about every 2s
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

PAIRS = [
    {
        "name": "ETH",
        "base": {"address": "0x4200000000000000000000000000000000000006", "symbol": "WETH", "decimals": 18},
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

# ---- pegged-pair recorder (data for the stat-arb project) -----------------------
# Once a minute, each pair below is quoted both ways on every pool and one row per
# pool goes in the pegged_quotes table. Price is "B per 1 A", so it sits near 1.0.
# usd_ref: which PAIRS price gives the USD value of one token (None = $1, a stablecoin).
# A token whose on-chain symbol/decimals don't match is skipped with a log line;
# it does not stop the scanner.
RECORD_SECONDS = 60
RECORD_SIZE_USD = 100.0

WBTC = {"address": "0x0555E30da8f98308EdB960aa94C0Db47230d2B9c", "symbol": "WBTC", "decimals": 8}
USDT = {"address": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2", "symbol": "USDT", "decimals": 6}
USDBC = {"address": "0xd9aAEc86B65D86f6a7B5B1b0c42FFA531710b6CA", "symbol": "USDbC", "decimals": 6}
DAI = {"address": "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb", "symbol": "DAI", "decimals": 18}

RECORD_PAIRS = [
    {"name": "cbBTC/WBTC", "a": CBBTC, "b": WBTC, "usd_ref": "BTC"},
    {"name": "USDT/USDC",  "a": USDT,  "b": USDC, "usd_ref": None},
    {"name": "USDbC/USDC", "a": USDBC, "b": USDC, "usd_ref": None},
    {"name": "DAI/USDC",   "a": DAI,   "b": USDC, "usd_ref": None},
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
