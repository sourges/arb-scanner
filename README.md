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

## Pegged-pair recorder (data for the stat-arb project)

Once a minute (inside the normal scan loop) the scanner also quotes pairs of tokens
that are meant to be worth the same: **cbBTC/WBTC, USDT/USDC, USDbC/USDC, DAI/USDC**.
Each pair is quoted both ways on every pool, $100 a side, in ONE extra eth_call.

- Price is "B per 1 A", so it sits near 1.0. **bid** = what selling A gets, **ask** = what buying A costs (pool fee included).
- **mid** = (best bid + best ask) / 2 across pools: the series stat arb studies. Shown in bp away from 1.0.
- **gap** = best bid / best ask - 1: above 0 means buying on one pool and selling on another nets something before gas.
- Rows go in the **pegged_quotes** table (ts, block, pair, pool, bid, ask); the log prints one `PEG ...` line a minute.
- A pair whose tokens fail the on-chain check is skipped with a log line; a failed minute is skipped. Neither stops the scanner.
- Pairs live in `RECORD_PAIRS` in `config.py`. Only v3-style pools are seen (same as the scanner), so Aerodrome's classic stable pools are not included yet.

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
python -m unittest            (expect: Ran 76 tests ... OK)
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
- **PEGGED PAIRS** section of `report.py`: per pair, minutes recorded, mid vs 1.0 (avg/min/max/std in bp), how many minutes had a gap > 0, and the best gap.

## Settings

All in `config.py` (trade size, fee tiers, rebalance cost, pairs, DEXs, gas model). Edit on the main PC, push, pull on the 24/7 PC.

## Not included yet

Uniswap v4 and other singleton-style DEXs, Aerodrome classic (v2-style) pools, extra pairs, execution of any kind.
