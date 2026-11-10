"""SQLite storage. One file (arb_scanner.sqlite), two tables. Open it with
report.py, DB Browser for SQLite, or pandas.read_sql.

connect() also upgrades an older database in place: any episode columns added
since it was created are added, so existing data is kept.
"""
import sqlite3

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
"""

# Columns added after v1. Older databases get them via ALTER TABLE (old rows read NULL).
EPISODE_PRICE_COLUMNS = ["start_mid", "best_mid", "best_buy_px", "best_sell_px"]


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
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
