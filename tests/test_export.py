"""export_db.py: snapshots of a live WAL database.

The live writer stays open for the whole test, like the running scanner, so recent
rows are still in the -wal side file. The snapshot must contain them anyway.
"""
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import db
import export_db
from tracker import Episode, MinuteRow


def episode(n):
    return Episode("dex>dex", "ETH", n, float(n), n, float(n), 1, 0.1, 0.1, "A", "B")


class TestExport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.live = os.path.join(self.tmp.name, "live.sqlite")
        self.out = os.path.join(self.tmp.name, "snap.sqlite")
        self.writer = db.connect(self.live)                    # the "scanner"
        db.insert_episodes(self.writer, [episode(i) for i in range(5)])
        db.insert_minutes(self.writer, [MinuteRow(60, "ETH", "dex>dex", 3, 0, -0.1, -0.3)])

    def tearDown(self):
        self.writer.close()
        self.tmp.cleanup()

    def test_snapshot_includes_rows_still_in_the_wal(self):
        self.assertGreater(os.path.getsize(self.live + "-wal"), 0)   # rows not folded in yet
        counts = export_db.export(self.live, self.out)
        self.assertEqual(counts["episodes"], 5)
        self.assertEqual(counts["minute_stats"], 1)
        self.assertEqual(counts["pegged_quotes"], 0)

    def test_snapshot_is_a_plain_single_file(self):
        export_db.export(self.live, self.out)
        snap = sqlite3.connect(self.out)
        try:
            self.assertEqual(snap.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(snap.execute("SELECT COUNT(*) FROM episodes").fetchone(), (5,))
        finally:
            snap.close()
        self.assertFalse(os.path.exists(self.out + "-wal"))
        self.assertFalse(os.path.exists(self.out + ".tmp"))

    def test_scanner_keeps_writing_during_and_after_export(self):
        reader = sqlite3.connect(self.live)                    # e.g. report.py mid-query
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM episodes").fetchall()
        try:
            export_db.export(self.live, self.out)
            db.insert_episodes(self.writer, [episode(99)])     # must not raise "locked"
        finally:
            reader.close()
        self.assertEqual(self.writer.execute("SELECT COUNT(*) FROM episodes").fetchone(), (6,))

    def test_second_export_replaces_the_first(self):
        export_db.export(self.live, self.out)
        db.insert_episodes(self.writer, [episode(7)])
        self.assertEqual(export_db.export(self.live, self.out)["episodes"], 6)

    def test_missing_database_is_a_clear_error(self):
        with self.assertRaises(FileNotFoundError):
            export_db.export(os.path.join(self.tmp.name, "nope.sqlite"), self.out)

    def test_main_prints_counts(self):
        buf = io.StringIO()
        with mock.patch.object(export_db.config, "DB_PATH", self.live), redirect_stdout(buf):
            export_db.main(["--out", self.out])
        self.assertIn("snapshot written", buf.getvalue())
        self.assertIn("episodes", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
