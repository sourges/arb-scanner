# base-arb-scanner

Read-only arbitrage scanner for Base. Every 2 seconds it prices a $100 round
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
that are meant to be worth the same: **cbBTC/WBTC, USDT/USDC, USDbC/USDC, DAI/USDC**, plus the
staked-ETH pairs **cbETH/WETH, wstETH/WETH** (added Nov 18, 2026).
Each pair is quoted both ways on every pool, $100 a side, in ONE extra eth_call.

- Price is "B per 1 A", so it sits near 1.0. Staked ETH is the exception: it earns staking rewards,
  so cbETH and wstETH are worth more than 1 WETH and their mid shows as roughly +1,000 bp or more,
  creeping up over time. Their "$100" size is priced with ETH, so it's really a bit more. **bid** = what selling A gets, **ask** = what buying A costs (pool fee included).
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

**Pairs added Nov 18, 2026:** cbETH/WETH and wstETH/WETH rows start then. The other pairs and
the 2-second scan are unchanged.

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
python -m unittest            (expect: Ran 292 tests ... OK)
python scanner.py --once      (one block, prints every route and every pegged-pool quote: sanity-check vs the exchanges)
python scanner.py             (or double-click run_scanner.bat)
```

Startup checks the token addresses (symbol + decimals) on-chain and lists the pools it found; it refuses to run if a token doesn't match.

### Two RPC providers (load split + failover, Nov 18 2026)

At about 1M Alchemy CU a day, one free Alchemy plan (30M/month) runs out in a full month.
Put a second Base URL in `.env` as `RPC_URL_2` (Ankr's free plan: 200M credits, about 1M
requests a month; we send about 1.4M a month in total) and scans alternate between them:

- **Split:** one provider per scan, in the rotation `RPC_WEIGHTS` (config.py). `[1, 1]` alternates;
  `[2, 1]` puts two scans on Alchemy for every one on Ankr. All calls in one scan (quotes, probes,
  recorder) go to the same provider.
- **Failover:** a failed call is tried once on the other provider, so the round isn't lost. HTTP 429,
  or `RPC_FAIL_LIMIT` (3) failures in a row, benches a provider for `RPC_BENCH_S` (60s); the log says
  `RPC ankr benched ...` and later `RPC ankr is answering again`.
- **Startup check:** each URL must answer `eth_chainId` with Base (8453); the time it took is logged.
  A bad `RPC_URL_2` is left out with a log line and everything runs on `BASE_RPC_URL`, as before.
- **Status line** ends with e.g. `rpc ankr (alchemy 50% / ankr 50%)`: who served this scan, and the
  share of calls since start. Only the domain is ever printed, never the URL (the key is in it).
- Providers can be a block apart. A scan that comes back with an older block than the last one is
  skipped like a repeated block.

## Reading the results

```
python report.py              all history
python report.py --hours 24   last day
python report.py --min-usd 0.10 --top 30
```

- **episodes** table: each run of consecutive profitable scans for a route: samples, seconds, best net, and the prices at the best moment (buy price, sell price, CEX mid) so you can see which side moved. Lasting episodes are what a slower bot could catch. Base makes a block about every 0.2s and the scanner samples about every 2s, so read `samples`/`seconds`, not the block columns.
- `report.py` skips episodes under `--min-usd` (default $0.05) as dust and lists the top episodes with their prices.
- **minute_stats** table: per route per minute: samples, how many were > 0, best and average net %.
- **tri_quotes** table: every scan's two triangle loops (see "Triangle" above). `report.py`'s **TRIANGLE** section shows, per loop, scans, % above zero, and median and best net.
- **cex_tri_quotes** table: the Binance.US triangle (see below), in `report.py`'s **CEX TRIANGLE** section.
- **tri_probes** table: triangle size probes (see "Triangle size probes" below), in `report.py`'s **TRIANGLE SIZE PROBES** section.
- **scan_prices** table: the prices behind every scan (see "Price rows" below). Not in `report.py` yet.
- **PEGGED PAIRS** section of `report.py`: per pair, minutes recorded, mid vs 1.0 (avg/min/max/std in bp), how many minutes had a gap > 0, and the best gap.

## Aero (added Nov 18, 2026)

Aero is the Aerodrome + Velodrome merger (launched Oct 22, 2026) with "Slipstream V3"
pools and **dynamic fees** that rise when markets move fast. It holds some of Base's
biggest pools (for example WETH/USDC `0x4a7a...366c`, about $10M liquidity). It is in
`DEXES` as `aerov3`, so **every** scan uses it: ETH, BTC, the extra pairs, the triangle,
both kinds of size probes and the recorder. Labels look like `aerov3-ts50`.

- Pools are found like Aerodrome's (factory `getPool` by tick spacing: 1, 10, 50, 100,
  150, 200, 250). Quotes go through Aero's Metaquoter, which takes the **pool address**.
- The Metaquoter gives no gas estimate, so `dex.AERO_QUOTE_GAS` (130,000) is used; at Base
  gas prices that is a fraction of a cent either way.
- **Caution:** Aero has an MEV tax module that charges swaps paying high priority fees,
  which competing bots do. Quotes are simulations without that tax, so a real trade on Aero
  can cost more than quoted. Treat Aero gaps as an upper bound.
- Pool set change: from Nov 18, 2026, data includes Aero pools. Episode counts, triangle
  loops and recorder rows before and after aren't directly comparable.
- Addresses: dromos-labs/metadex-slipstream-public, `deployment-addresses/cl-base.json`.
- `find_pool.py` names Aero pools `aerov3` and reports them as scanned.

## Extra pairs (added Nov 18, 2026)

Besides ETH/USDC and cbBTC/USDC, the 2-second scan also covers less-watched pairs from
`EXTRA_PAIRS` in `config.py`: **ETH/BTC** (WETH/cbBTC pools), **SOL** (SOL/USDC),
**AERO/ETH** (new AERO contract after the Nov 2026 Aero migration) and **VIRTUAL/ETH**.
They ride in the same eth_call, so no extra RPC calls.

- Pools priced in WETH or cbBTC are handled with a general quote token: the $100 trade is
  $100 worth of the quote token, and every result is converted back to USD.
- Reference price: the base token's own Kraken/Coinbase USD book, else `base_ref`
  (ETH/BTC uses the ETH price), else the median pool price (DEX-derived, updated each scan).
- CEX routes only run for USDC-quoted pairs (SOL). The WETH-priced pairs are `dex>dex` only;
  their CEX books still feed `scan_prices`.
- Optional: a token whose on-chain symbol doesn't match, a pair with no pools, or a CEX
  market that doesn't exist is skipped with a log line; the scanner runs on. A pool whose
  $100 price is more than `OFF_MARKET_PCT` (10%) from the reference is dropped at startup
  as dead/legacy, so it can't show fake gaps. ETH and BTC are not screened (unchanged).
- The status line shows ETH and BTC as before, plus one `EXTRA best <pair> <route> <net %>`.
- Size probes work for every pair. dex>dex probes and triangle probes now have separate
  hourly caps (`PROBE_MAX_PER_HOUR`, `TRI_PROBE_MAX_PER_HOUR`, 60 each).
- `--once` prints how long one scan took. It must stay well under `POLL_SECONDS`.
- Extra pairs search more pool tiers than ETH and BTC (`EXTRA_DISCOVERY_TIERS`): the 1% tier
  on Uniswap/PancakeSwap and Aerodrome tick spacings 10, 500, 1,000 and 2,000. Smaller tokens
  often sit in those pools.

### Checking a pool: find_pool.py

```
python find_pool.py 0x868eed6bad4bd368cd053b766f84cc727697589a
```

Reads a pool's tokens, fee, tick spacing and factory from the chain (one call) and says
whether the scanner covers it: `scanned`, `extra pairs only`, or `NOT scanned` (then add
its tier to `EXTRA_DISCOVERY_TIERS`). Works for classic pools too.
- Episode counts before and after Nov 18 aren't directly comparable: there are more pairs.

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

## Triangle size probes (Nov 18)

The same question for the triangle. When a loop's $100 net edge reaches
`TRI_PROBE_MIN_NET_PCT` (default +0.02%) after being below it on the scan before,
its three pools are re-quoted at `PROBE_SIZES_USD` in one extra eth_call. Each leg is
quoted at about that size (so slippage is in), then chained like the main scan.
One row per size goes in the **tri_probes** table: the shared columns
(`ts, strategy='tri_arb_probe', venue='base', path, size_usd, raw_edge, net_edge`), then
`block, loop, leg1, leg2, leg3, usd_back, gas_usd, trigger_net` (the $100 net % that
opened it). `path` is the loop plus its pools. NULL edges = a leg couldn't fill. Log line:

```
PROBE TRI USDC>WETH>cbBTC>USDC pcsv3-100/aero3-ts1/pcsv3-100: $100 +0.031% | $500 -0.012% | ...
```

It probes the pools the loop just used ("could this gap absorb more?"), not the best
loop at each size. It shares `PROBE_MAX_PER_HOUR` with the dex>dex probes.
`TRI_PROBE_MIN_NET_PCT = None` turns it off. Last week's data suggests roughly 100
openings a day. This is the data for the Dec 15 decision: keep the triangle only if
some loop clears costs at $500 or more.

## Binance.US triangle (Nov 18)

The same three-way loop, but all on one exchange: no gas, no pools, no MEV. For each
quote currency (USD, USDT, USDC): **Q → BTC → ETH → Q** and **Q → ETH → BTC → Q**, all
sharing the ETHBTC book. 6 loops, 7 public order books, no API key.

- Every `BUS_SECONDS` (default 4s) it fetches the 7 books (top 20 levels), walks them for a
  real $100 fill (buy at the asks, sell at the bids) and pays the taker fee on each leg.
- **raw_edge** = before fees (spreads and depth only), **net_edge** = after the three taker
  fees (`BUS_TAKER_FEES`, per symbol, default 0.02%: check your account's fee page).
- Rows go in **cex_tri_quotes**: `ts, strategy ('tri_arb'), venue ('binanceus'), path, size_usd,
  raw_edge, net_edge`, then `back_raw, back_net, fee_usd, leg1, leg2, leg3, book_age`
  (seconds between the first and last of the three book fetches; rows from before the
  Nov 18 fix hold a negative number and the report ignores them). 6 rows every 4s, about 130K a day.
- **Rate limits:** Binance.US allows 1200 request weight a minute per IP, 5 per book, so
  7 books every 4s is about 525. It skips a round when Binance reports `BUS_MAX_WEIGHT` (800)
  used, and pauses at least `BUS_BACKOFF_S` (60s) or Binance's Retry-After on a 429 (slow
  down) or 418 (IP ban). Your own Binance.US trading from this PC counts against the same
  limit, so don't set `BUS_SECONDS` below 3.
- Startup fetches every book once: a symbol Binance.US doesn't list drops that quote's loops
  with a log line; no ETHBTC book turns the whole thing off. A failed round is logged and
  skipped. Neither ever stops the Base scanner.
- The status line ends with `BUS best <loop> <net %>`; `python scanner.py --once` prints all 6.
- Not modeled: Binance's lot-size rounding (fractions of a cent at $100), and the few hundred
  ms between the three fetches (that's what `book_age` measures).
- `BUS_SECONDS = None` turns it off.

## Price rows (the prices behind every scan)

The other tables store results. `scan_prices` stores the prices themselves, one row per
pair per scan, from data the scan already has (no extra RPC calls):

- `kraken_bid`, `kraken_ask`, `coinbase_bid`, `coinbase_ask`: best bid and ask. Empty when there was no fresh book.
- `kraken_age`, `coinbase_age`: seconds between fetching that book and the scan. A gap seen against a book several seconds old may never have existed.
- `dex_buy_px` / `dex_buy_pool`: the cheapest pool to buy on (USDC paid per ETH/BTC at $100, fee and slippage inside, no gas). `dex_sell_px` / `dex_sell_pool`: the best pool to sell on.
- Shared-format columns first: `ts, strategy ('px'), venue ('base'), path (ETH or BTC), size_usd, raw_edge, net_edge`. **raw_edge** = DEX mid / CEX mid - 1 in % (Kraken's mid, or Coinbase's if Kraken has no fresh book); positive = pools priced above the exchange. **net_edge** is always empty: no costs are applied.
- Use it to measure how many seconds the pools take to follow Kraken, to drop stale-book gaps, or to re-price routes with other fees.
- About 86,000 small rows a day, the same as `tri_quotes`. `PRICE_LOG = False` in `config.py` turns it off. Rows start Nov 14, 2026.

## If the scanner stops

- A bad reply from the RPC provider (empty, cut off, not JSON) is treated like any RPC error: the round is skipped and retried after 2, 4, 8 ... 30 seconds.
- A real bug still stops the scanner with a traceback, on purpose.
- `run_scanner.bat` starts the scanner again 30 seconds after it stops for any reason. To stop it for good: Ctrl+C, then Y.

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
