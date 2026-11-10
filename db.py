"""SQLite storage. One file (arb_scanner.sqlite), three tables. Open it with
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
    ask           REAL
);
CREATE INDEX IF NOT EXISTS idx_pegged_pair_ts ON pegged_quotes (pair, ts);
"""

# Columns added after v1. Older databases get them via ALTER TABLE (old rows read NULL).
EPISODE_PRICE_COLUMNS = ["start_mid", "best_mid", "best_buy_px", "best_sell_px"]


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S)
    conn.execute("PRAGMA journal_mode=WAL")    # saved in the file; ":memory:" ignores it
    conn.executescript(SCHEMA)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(episodes)")}
    for col in EPISODE_PRICE_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE episodes ADD COLUMN {col} REAL")
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


def insert_pegged(conn, ts, block, quotes):
    """Recorder rows: one per pool per minute (price = B per 1 A, see recorder.py)."""
    conn.executemany(
        "INSERT INTO pegged_quotes (ts, block, pair, pool, bid, ask) VALUES (?,?,?,?,?,?)",
        [(ts, block, q.pair, q.pool, q.bid, q.ask) for q in quotes],
    )
    conn.commit()
