"""Make a consistent copy of the scanner's database, safe to run while the scanner is running.

Run (in the arb-scanner folder, venv active):
    python export_db.py                          -> arb_scanner_snapshot.sqlite
    python export_db.py --out D:\\data\\snap.sqlite

Why not just copy the file? The live database is in WAL mode: recent writes sit in
arb_scanner.sqlite-wal until SQLite folds them in. Copying the .sqlite file alone can
miss them, or catch a write halfway. This uses SQLite's backup API, which reads one
consistent moment of the database including the WAL, without blocking the scanner.

The snapshot is a normal single-file database (not WAL), so it can be copied, emailed
or opened anywhere. Other projects (stat-arb) read snapshots, never the live file.
"""
import argparse
import os
import sqlite3

import config

DEFAULT_OUT = "arb_scanner_snapshot.sqlite"


def export(src_path: str, out_path: str) -> dict:
    """Copy src_path to out_path. Returns {table: row count} of the snapshot."""
    if not os.path.exists(src_path):
        raise FileNotFoundError(f"no database at {src_path}: run the scanner first")
    tmp_path = out_path + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    src = sqlite3.connect(f"file:{os.path.abspath(src_path)}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp_path)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")   # a plain single file, no -wal side files
        tables = [r[0] for r in dst.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        counts = {t: dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        dst.close()
        src.close()

    os.replace(tmp_path, out_path)   # appears all at once: a reader never sees half a snapshot
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser(description="Consistent copy of the scanner database.")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"output file (default {DEFAULT_OUT})")
    args = ap.parse_args(argv)
    counts = export(config.DB_PATH, args.out)
    print(f"snapshot written: {os.path.abspath(args.out)}")
    for table, n in counts.items():
        print(f"  {table:<16}{n:>10,} rows")


if __name__ == "__main__":
    main()
