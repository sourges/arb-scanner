"""SQLite storage. One file (arb_scanner.sqlite), six tables. Open it with
report.py, DB Browser for SQLite, or pandas.read_sql.

connect() also upgrades an older database in place: missing tables and any
episode columns added since it was created are added, so existing data is kept.

WAL mode: the live database runs in SQLite's write-ahead-log mode, so a reader
(report.py, export_db.py, a notebook) never blocks the scanner's writes. WAL
keeps two side files next to the database, arb_scanner.sqlite-wal and -shm.
They are part of the database: never delete them while the scanner runs, and
never copy the .sqlite file alone. Use export_db.py for a copy.
"""
import sqlite3

BUSY_TIMEOUT_S = 10   # if the file is busy anyway, wait this long before giving up

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id            INTEGER PRIMARY KEY,
    pair          TEXT,
    route         TEXT,
    start_block   INTEGER,
    end_block     INTEGER,
    blocks        INTEGER,
    samples       INTEGER,
    start_ts      REAL,
    seconds       REAL,
    max_net_usd   REAL,
    max_net_pct   REAL,
    best_buy      TEXT,
    best_sell     TEXT
);
CREATE TABLE IF NOT EXISTS minute_stats (
    minute        INTEGER,
    pair          TEXT,
    route         TEXT,
    samples       INTEGER,
    positives     INTEGER,
    best_net_pct  REAL,
    avg_net_pct   REAL,
    PRIMARY KEY (minute, pair, route)
);
CREATE TABLE IF NOT EXISTS pegged_quotes (
    ts            REAL,
    block         INTEGER,
    pair          TEXT,
    pool          TEXT,
    bid           REAL,
    ask           REAL,
    depth_usd     REAL,
    depth_bid     REAL,
    depth_ask     REAL
);
CREATE INDEX IF NOT EXISTS idx_pegged_pair_ts ON pegged_quotes (pair, ts);
-- Shared-format columns first (ts, strategy, venue, path, size_usd, raw_edge, net_edge),
-- the same names every new collector uses so data can be combined later.
-- Edges are % of size_usd.
CREATE TABLE IF NOT EXISTS tri_quotes (
    ts            REAL,
    strategy      TEXT,
    venue         TEXT,
    path          TEXT,
    size_usd      REAL,
    raw_edge      REAL,
    net_edge      REAL,
    block         INTEGER,
    usd_back      REAL,
    gas_usd       REAL,
    leg1          TEXT,
    leg2          TEXT,
    leg3          TEXT
);
CREATE INDEX IF NOT EXISTS idx_tri_path_ts ON tri_quotes (path, ts);
-- Coinbase (or other CEX) best bid/ask for a recorder pair, once a minute (recorder.py).
-- raw_edge = CEX mid / the same minute's best Base mid - 1, in %. size_usd and net_edge
-- are NULL (top of book only, no costs). block = that minute's Base block, for joins.
CREATE TABLE IF NOT EXISTS cex_quotes (
    ts            REAL,
    strategy      TEXT,
    venue         TEXT,
    path          TEXT,
    size_usd      REAL,
    raw_edge      REAL,
    net_edge      REAL,
    block         INTEGER,
    product       TEXT,
    bid           REAL,
    ask           REAL,
    bid_size      REAL,
    ask_size      REAL,
    fetch_s       REAL
);
CREATE INDEX IF NOT EXISTS idx_cex_path_ts ON cex_quotes (path, ts);
-- Probes the hourly cap refused (Nov 27, 2026): one row per episode/loop the first time
-- it is refused, so the report can say how much the cap hid. Shared columns first.
-- net_edge = the $100 net % that triggered it; raw_edge and size_usd are $100-scan values.
CREATE TABLE IF NOT EXISTS probe_skips (
    ts            REAL,
    strategy      TEXT,
    venue         TEXT,
    path          TEXT,
    size_usd      REAL,
    raw_edge      REAL,
    net_edge      REAL,
    block         INTEGER,
    reason        TEXT
);
CREATE INDEX IF NOT EXISTS idx_probe_skips_ts ON probe_skips (ts);
CREATE TABLE IF NOT EXISTS episode_probes (
    ts            REAL,
    strategy      TEXT,
    venue         TEXT,
    path          TEXT,
    size_usd      REAL,
    raw_edge      REAL,
    net_edge      REAL,
    block         INTEGER,
    route         TEXT,
    pair          TEXT,
    buy_pool      TEXT,
    sell_pool     TEXT,
    usd_back      REAL,
    gas_usd       REAL,
    buy_px        REAL,
    sell_px       REAL
);
CREATE INDEX IF NOT EXISTS idx_probe_ts ON episode_probes (ts);
-- Triangle size probes (see tri_probe.py): one row per size when a loop opens.
-- path = loop + its three pools. NULL edges = a leg couldn't fill that size.
CREATE TABLE IF NOT EXISTS tri_probes (
    ts            REAL,
    strategy      TEXT,
    venue         TEXT,
    path          TEXT,
    size_usd      REAL,
    raw_edge      REAL,
    net_edge      REAL,
    block         INTEGER,
    loop          TEXT,
    leg1          TEXT,
    leg2          TEXT,
    leg3          TEXT,
    usd_back      REAL,
    gas_usd       REAL,
    trigger_net   REAL
);
CREATE INDEX IF NOT EXISTS idx_tri_probe_ts ON tri_probes (ts);
-- Triangles on one exchange (see cex_triangle.py): one row per loop per round.
-- raw_edge = before taker fees, net_edge = after. book_age = seconds between the first and
-- last of the three book fetches (rows before Nov 18 14:40 hold a wrong, negative number).
CREATE TABLE IF NOT EXISTS cex_tri_quotes (
    ts            REAL,
    strategy      TEXT,
    venue         TEXT,
    path          TEXT,
    size_usd      REAL,
    raw_edge      REAL,
    net_edge      REAL,
    back_raw      REAL,
    back_net      REAL,
    fee_usd       REAL,
    leg1          TEXT,
    leg2          TEXT,
    leg3          TEXT,
    book_age      REAL
);
CREATE INDEX IF NOT EXISTS idx_cex_tri_path_ts ON cex_tri_quotes (path, ts);
-- Prices at every scan, one row per pair (see prices.py). path = pair name (ETH, BTC).
-- raw_edge = DEX mid vs CEX mid in %; net_edge is always NULL (prices only, no costs).
CREATE TABLE IF NOT EXISTS scan_prices (
    ts            REAL,
    strategy      TEXT,
    venue         TEXT,
    path          TEXT,
    size_usd      REAL,
    raw_edge      REAL,
    net_edge      REAL,
    block         INTEGER,
    kraken_bid    REAL,
    kraken_ask    REAL,
    kraken_age    REAL,
    coinbase_bid  REAL,
    coinbase_ask  REAL,
    coinbase_age  REAL,
    dex_buy_px    REAL,
    dex_buy_pool  TEXT,
    dex_sell_px   REAL,
    dex_sell_pool TEXT
);
CREATE INDEX IF NOT EXISTS idx_prices_path_ts ON scan_prices (path, ts);
"""

# Columns added after v1. Older databases get them via ALTER TABLE (old rows read NULL).
EPISODE_PRICE_COLUMNS = ["start_mid", "best_mid", "best_buy_px", "best_sell_px"]
# Pool depth (Nov 14, 2026): the same pool quoted at a bigger size. Older rows read NULL.
PEGGED_DEPTH_COLUMNS = ["depth_usd", "depth_bid", "depth_ask"]
# Probe's own block (Nov 27, 2026). `block` stays the triggering scan's block. Older rows NULL.
PROBE_BLOCK_TABLES = ["episode_probes", "tri_probes"]


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S)
    conn.execute("PRAGMA journal_mode=WAL")    # saved in the file; ":memory:" ignores it
    conn.executescript(SCHEMA)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(episodes)")}
    for col in EPISODE_PRICE_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE episodes ADD COLUMN {col} REAL")
    existing = {row[1] for row in conn.execute("PRAGMA table_info(pegged_quotes)")}
    for col in PEGGED_DEPTH_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE pegged_quotes ADD COLUMN {col} REAL")
    for table in PROBE_BLOCK_TABLES:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "probe_block" not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN probe_block INTEGER")
    conn.commit()
    return conn


def insert_episodes(conn, episodes):
    conn.executemany(
        "INSERT INTO episodes (pair, route, start_block, end_block, blocks, samples, start_ts,"
        " seconds, max_net_usd, max_net_pct, best_buy, best_sell,"
        " start_mid, best_mid, best_buy_px, best_sell_px)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(e.pair, e.key, e.start_block, e.end_block, e.blocks, e.samples, e.start_ts,
          e.seconds, e.max_net_usd, e.max_net_pct, e.best_buy, e.best_sell,
          e.start_mid, e.best_mid, e.best_buy_px, e.best_sell_px) for e in episodes],
    )
    conn.commit()


def insert_minutes(conn, rows):
    conn.executemany(
        "INSERT OR REPLACE INTO minute_stats VALUES (?,?,?,?,?,?,?)",
        [(r.minute, r.pair, r.key, r.samples, r.positives, r.best_net_pct, r.avg_net_pct) for r in rows],
    )
    conn.commit()


def insert_pegged(conn, ts, block, quotes, depth_usd=None):
    """Recorder rows: one per pool per minute (price = B per 1 A, see recorder.py).
    depth_usd: the bigger size each pool was also quoted at (None = depth quotes off)."""
    conn.executemany(
        "INSERT INTO pegged_quotes (ts, block, pair, pool, bid, ask, depth_usd, depth_bid, depth_ask)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [(ts, block, q.pair, q.pool, q.bid, q.ask, depth_usd if depth_usd else None,
          q.depth_bid, q.depth_ask) for q in quotes],
    )
    conn.commit()


def insert_cex_quotes(conn, rows):
    """rows: [recorder.CexQuote]. One per product per minute."""
    conn.executemany(
        "INSERT INTO cex_quotes (ts, strategy, venue, path, size_usd, raw_edge, net_edge, block,"
        " product, bid, ask, bid_size, ask_size, fetch_s) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r.ts, "peg_cex", r.venue, r.pair, None, r.raw_edge, None, r.block, r.product,
          r.bid, r.ask, r.bid_size, r.ask_size, r.fetch_s) for r in rows],
    )
    conn.commit()


def insert_tri(conn, ts, block, results, strategy="tri_arb", venue="base"):
    """Triangle rows: one per loop per scan (see triangle.py)."""
    conn.executemany(
        "INSERT INTO tri_quotes (ts, strategy, venue, path, size_usd, raw_edge, net_edge,"
        " block, usd_back, gas_usd, leg1, leg2, leg3) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(ts, strategy, venue, r.path, r.size_usd, r.raw_edge, r.net_edge, block,
          r.usd_back, r.gas_usd, r.legs[0], r.legs[1], r.legs[2]) for r in results],
    )
    conn.commit()


def insert_probes(conn, ts, block, route, results, strategy="dex_arb_probe", venue="base"):
    """Size-probe rows: one per size (see probe.py). ts = the round the episode opened,
    so a probe joins its episode on (pair, route, start_ts = ts). NULL edges = can't fill."""
    conn.executemany(
        "INSERT INTO episode_probes (ts, strategy, venue, path, size_usd, raw_edge, net_edge,"
        " block, route, pair, buy_pool, sell_pool, usd_back, gas_usd, buy_px, sell_px, probe_block)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(ts, strategy, venue, r.path, r.size_usd, r.raw_edge, r.net_edge, block, route,
          r.pair, r.buy_pool, r.sell_pool, r.usd_back, r.gas_usd, r.buy_px, r.sell_px,
          getattr(r, "probe_block", None))
         for r in results],
    )
    conn.commit()


def insert_probe_skip(conn, ts, block, strategy, path, size_usd, net_edge, reason="hourly_cap",
                      raw_edge=None, venue="base"):
    """One probe the hourly cap refused (see probe.py / tri_probe.py)."""
    conn.execute(
        "INSERT INTO probe_skips (ts, strategy, venue, path, size_usd, raw_edge, net_edge, block, reason)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (ts, strategy, venue, path, size_usd, raw_edge, net_edge, block, reason))
    conn.commit()


def insert_prices(conn, ts, block, rows, strategy="px", venue="base"):
    """Price rows: one per pair per scan (see prices.py). net_edge is NULL on purpose."""
    conn.executemany(
        "INSERT INTO scan_prices (ts, strategy, venue, path, size_usd, raw_edge, net_edge, block,"
        " kraken_bid, kraken_ask, kraken_age, coinbase_bid, coinbase_ask, coinbase_age,"
        " dex_buy_px, dex_buy_pool, dex_sell_px, dex_sell_pool)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(ts, strategy, venue, r.pair, r.size_usd, r.raw_edge, None, block,
          r.kraken_bid, r.kraken_ask, r.kraken_age, r.coinbase_bid, r.coinbase_ask, r.coinbase_age,
          r.dex_buy_px, r.dex_buy_pool, r.dex_sell_px, r.dex_sell_pool) for r in rows],
    )
    conn.commit()


def insert_tri_probes(conn, ts, block, results, strategy="tri_arb_probe", venue="base"):
    """Triangle size-probe rows: one per size (see tri_probe.py). NULL edges = can't fill."""
    conn.executemany(
        "INSERT INTO tri_probes (ts, strategy, venue, path, size_usd, raw_edge, net_edge, block,"
        " loop, leg1, leg2, leg3, usd_back, gas_usd, trigger_net, probe_block)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(ts, strategy, venue, r.path, r.size_usd, r.raw_edge, r.net_edge, block, r.loop,
          r.legs[0], r.legs[1], r.legs[2], r.usd_back, r.gas_usd, r.trigger_net,
          getattr(r, "probe_block", None)) for r in results],
    )
    conn.commit()


def insert_cex_tri(conn, ts, results, strategy="tri_arb", venue="binanceus"):
    """One-exchange triangle rows: one per loop per round (see cex_triangle.py)."""
    conn.executemany(
        "INSERT INTO cex_tri_quotes (ts, strategy, venue, path, size_usd, raw_edge, net_edge,"
        " back_raw, back_net, fee_usd, leg1, leg2, leg3, book_age)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(ts, strategy, venue, r.path, r.size_usd, r.raw_edge, r.net_edge, r.back_raw, r.back_net,
          r.fee_usd, r.legs[0], r.legs[1], r.legs[2], r.book_age) for r in results],
    )
    conn.commit()
