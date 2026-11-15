# base-arb-scanner

Read-only arbitrage scanner for Base. Every block (~2s) it prices a $100 round
trip across Base DEX pools, Kraken and Coinbase, subtracts every cost, and logs
how often a route nets above zero and for how long. **It never trades and needs
no wallet or exchange API keys.**

## What it scans

- **Pairs:** WETH/USDC and cbBTC/USDC on Base, ETH-USD and BTC-USD on the CEXs (USD treated as equal to USDC)
- **DEXs:** Uniswap v3, PancakeSwap v3, Aerodrome Slipstream (all three factories). Pools are found from each factory at startup.
- **CEX fee schedules:** `kraken`, `kraken_reduced`, `coinbase`, each as maker and taker

## Routes (13 per pair)

| key | meaning |
|---|---|
| `dex>dex` | buy on the best pool, sell on another |
| `kraken_reduced_taker>dex` | buy on Kraken (taker), sell on the best pool |
| `dex>kraken_reduced_maker` | buy on the best pool, sell with a resting Kraken ask |
| ...same for each schedule and side | |

net = USD back - $100 - CEX fee - Base gas - rebalance cost.
DEX swap fees and slippage are inside the on-chain quotes.
Maker routes assume the resting order fills at the best bid/ask (optimistic on purpose: that is the question being tested).

## Triangle (three-way) arb

Every scan the scanner also prices two loops that start and end in USDC:
**USDC → WETH → cbBTC → USDC** and the reverse **USDC → cbBTC → WETH → USDC**.

- Each leg uses the best pool for that leg, on any DEX (e.g. buy WETH on Uniswap, swap to cbBTC on Aerodrome, sell on PancakeSwap).
- The WETH/cbBTC pools are found at startup and quoted inside the **same** per-block eth_call: no extra RPC calls.
- Legs 2 and 3 scale a ~$100 quote to the exact amount the previous leg produced (accurate in deep pools; a profitable loop should be re-checked with chained quotes).
- **raw_edge** = USDC back / $100 − 1, in %, with pool fees and slippage included but not gas. **net_edge** also subtracts gas for three separate swaps (cautious: one contract doing all three would pay less).
- Rows go in the **tri_quotes** table, one row per loop per scan (~2 rows every 2s, roughly 86K rows a day). Columns start with the shared format used by every new collector: `ts, strategy ('tri_arb'), venue ('base'), path, size_usd, raw_edge, net_edge`, then `block, usd_back, gas_usd, leg1, leg2, leg3`.
- The status line shows `TRI best <path> <net %>`; `python scanner.py --once` prints both loops with their pools.
- Settings: `TRIANGLE_PAIRS` in `config.py` (`None` turns it off). No WETH/cbBTC pools found = triangle off with a log line; the scanner keeps running.

## Pegged-pair recorder (data for the stat-arb project)

Once a minute (inside the normal scan loop) the scanner also quotes pairs of tokens
that are meant to be worth the same: **cbBTC/WBTC, USDT/USDC, USDbC/USDC, DAI/USDC**.
Each pair is quoted both ways on every pool, $100 a side, in ONE extra eth_call.

- Price is "B per 1 A", so it sits near 1.0. **bid** = what selling A gets, **ask** = what buying A costs (pool fee included).
- **mid** = (best bid + best ask) / 2 across pools: the series stat arb studies. Shown in bp away from 1.0.
- **gap** = best bid / best ask - 1: above 0 means buying on one pool and selling on another nets something before gas.
- Rows go in the **pegged_quotes** table (ts, block, pair, pool, bid, ask, depth_usd, depth_bid, depth_ask); the log prints one `PEG ...` line a minute.
- A pair whose tokens fail the on-chain check is skipped with a log line; a failed minute is skipped. Neither stops the scanner.
- Pairs live in `RECORD_PAIRS` in `config.py`.
- **Pools:** every v3-style pool from `DEXES` (shared with the scan), plus **Aerodrome classic** stable and volatile pools from `RECORD_CLASSIC_DEXES`. Classic pools are recorder-only: the 2-second scan, triangle and size probes never see them. They're quoted with the pool's own `getAmountOut` (fee included); labels `aeroc-stable` / `aeroc-vol`.
- **Depth:** each pool is also quoted at `RECORD_DEPTH_USD` ($1,000) in the same call, stored as depth_bid / depth_ask. A deep pool's $1,000 round trip (ask / bid - 1) is about its $100 one; a thin pool's blows up or can't fill. Best bid/ask, mid and gap still use $100.
- `report.py` **PEGGED POOLS** lists every pool: % of minutes it set the best bid / ask, round trip at $100 and $1,000, and how often $1,000 filled. "dead" = a round trip over 100,000 bp.

**Pool set changed Nov 14, 2026** (commit after 5e969cc): Aerodrome classic pools and the depth
columns start then. Earlier pegged rows have no classic pools and NULL depth, so best bid/ask
before and after aren't strictly comparable. For a like-for-like series, filter out pools
labelled `aeroc-*`. The 2-second scan's pool set did not change.

## How prices are measured

- **DEX:** each pool's QuoterV2 is asked "what comes out for $100 in?" and "what comes out for 0.05 ETH in?". All quotes, plus block number, base fee and L1 fee, go in ONE Multicall3 eth_call per block, which keeps a free RPC tier happy.
- **CEX:** public order books, walked for the real $100 fill.
- **Gas:** (quoter gas + overhead) x (base fee + tip) + Base's L1 data fee, converted at the ETH price.

## Setup (first time)

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env        (then paste your Alchemy/QuickNode Base URL into .env)
python -m unittest            (expect: Ran 136 tests ... OK)
python scanner.py --once      (one block, prints every route and every pegged-pool quote: sanity-check vs the exchanges)
python scanner.py             (or double-click run_scanner.bat)
```

Startup checks the token addresses (symbol + decimals) on-chain and lists the pools it found; it refuses to run if a token doesn't match.

## Reading the results

```
python report.py              all history
python report.py --hours 24   last day
python report.py --min-usd 0.10 --top 30
```

- **episodes** table: each run of consecutive profitable scans for a route: samples, seconds, best net, and the prices at the best moment (buy price, sell price, CEX mid) so you can see which side moved. Lasting episodes are what a slower bot could catch. Base makes a block about every 0.2s and the scanner samples about every 2s, so read `samples`/`seconds`, not the block columns.
- `report.py` skips episodes under `--min-usd` (default $0.05) as dust and lists the top episodes with their prices.
- **minute_stats** table: per route per minute: samples, how many were > 0, best and average net %.
- **tri_quotes** table: every scan's two triangle loops (see "Triangle" above). Not in `report.py` yet; open it with DB Browser for SQLite or pandas.
- **PEGGED PAIRS** section of `report.py`: per pair, minutes recorded, mid vs 1.0 (avg/min/max/std in bp), how many minutes had a gap > 0, and the best gap.

## Size probes (how much could a gap absorb?)

Every route is scored at $100, and the gaps that show up are mostly on thin pools.
So when a `dex>dex` episode opens at or above `PROBE_MIN_NET_PCT` (default +0.05% net),
the same buy pool -> sell pool trade is re-quoted at `PROBE_SIZES_USD`
($100, $500, $1,000, $2,500, $5,000) in one extra eth_call. One row per size goes in
the `episode_probes` table (joins its episode on pair, route and `ts = start_ts`).
"can't fill" means the pool couldn't take that size. The log prints one line:

```
PROBE ETH aero1-ts1>pcsv3-100: $100 +0.616% | $500 +0.40% | $1,000 +0.11% | $2,500 can't fill | ...
```

`report.py` shows a SIZE PROBES section: per size, how many probes filled and netted
above zero, the median and best net %, and the top probed gaps side by side. The probe
runs one RPC round trip after the scan, so its $100 number can differ slightly from the
episode's; all sizes in one probe come from the same block. `PROBE_MAX_PER_HOUR` caps
the extra calls; `PROBE_SIZES_USD = []` turns probing off.

## Sharing the data (stat-arb and other projects)

The live database (`arb_scanner.sqlite`) belongs to the scanner. It runs in SQLite's
WAL mode, so readers never block the scanner's writes, and it keeps two side files
next to it: `arb_scanner.sqlite-wal` and `arb_scanner.sqlite-shm`. **Never delete
them while the scanner runs, and never copy the .sqlite file by itself.**

Other projects read a snapshot instead:

```
python export_db.py                        (writes arb_scanner_snapshot.sqlite)
python export_db.py --out D:\data\snap.sqlite
```

The snapshot is one consistent moment of the database, including recent writes still
in the WAL file. It's safe to run while the scanner runs, and it's a plain single file
you can copy to another PC. A busy or locked database never stops the scanner: that
round is skipped and retried like an RPC error.

## Settings

All in `config.py` (trade size, fee tiers, rebalance cost, pairs, DEXs, gas model). Edit on the main PC, push, pull on the 24/7 PC.

## Not included yet

Uniswap v4 and other singleton-style DEXs; Aerodrome classic pools in the 2-second scan (recorder only);
Solidly V3 and BaseSwap V3 (no verified quoter address on Base yet); extra pairs; execution of any kind.
