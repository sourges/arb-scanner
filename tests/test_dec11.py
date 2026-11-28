"""dec11.py: the Dec 11 dex>dex rule applied mechanically. Hand-worked.

Window: 14 days from T0, so 10 positive days are needed.
A $1,000 probe at +0.5% net = $5.00 -> expected 0.425 * (5.00 - 0.15) = $2.06125.
    22 of them, 2 a day on 11 days: total $45.3475 -> $3.239/day, top day 2/22 = 9%  -> PASS
The same with an Aero leg: $5.00 * 0.5 = $2.50 -> 0.425 * 2.35 = $0.99875 each,
    total $21.9725 -> $1.569/day -> IN BETWEEN
A $1,000 probe at +0.01% = $0.10: under the $0.15 cost -> not attempted, $0.
"""
import io
import time
import unittest
from contextlib import redirect_stdout

import db
import dec11

T0 = time.mktime((2026, 11, 28, 0, 0, 0, 0, 0, -1))
DAY = 86400.0
END = T0 + 14 * DAY


def add_probe(conn, ts, net, size=1000.0, buy="uni-500", sell="pcsv3-100", samples=2, pair="ETH"):
    conn.execute("INSERT INTO episode_probes (ts, strategy, venue, path, size_usd, net_edge, route, pair,"
                 " buy_pool, sell_pool) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (ts, "dex_arb_probe", "base", f"{pair} {buy}>{sell}", size, net, "dex>dex", pair, buy, sell))
    if samples is not None:
        conn.execute("INSERT INTO episodes (pair, route, start_ts, samples, max_net_usd) VALUES (?,?,?,?,?)",
                     (pair, "dex>dex", ts, samples, 0.5))


def spread(conn, days, per_day, **kw):
    """per_day probes a day on the first `days` days, at noon + k minutes."""
    for d in range(days):
        for k in range(per_day):
            add_probe(conn, T0 + d * DAY + 12 * 3600 + 60 * k, **kw)


class TestRule(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")

    def size_row(self, size=1000.0):
        rows, verdict = dec11.evaluate(self.conn, T0, END)
        return {r["size"]: r for r in rows}[size], verdict

    def test_pass(self):
        spread(self.conn, 11, 2, net=0.5)
        r, verdict = self.size_row()
        self.assertEqual((r["n"], r["days"], r["need"], r["days_pos"]), (22, 14, 10, 11))
        self.assertAlmostEqual(r["per_day"], 45.3475 / 14, places=6)
        self.assertAlmostEqual(r["max_share"], 2 / 22)
        self.assertTrue(verdict.startswith("PASS at $1,000"))

    def test_aero_haircut(self):
        spread(self.conn, 11, 2, net=0.5, sell="aerov3-ts50")
        r, verdict = self.size_row()
        self.assertAlmostEqual(r["per_day"], 22 * 0.425 * 2.35 / 14, places=6)
        self.assertTrue(verdict.startswith("IN BETWEEN"))

    def test_one_scan_flashes_are_not_counted(self):
        spread(self.conn, 11, 2, net=0.5, samples=1)
        r, verdict = self.size_row()
        self.assertEqual((r["n"], r["per_day"]), (0, 0.0))
        self.assertAlmostEqual(r["flash_share"], 1.0)
        self.assertTrue(verdict.startswith("SHELVE (< $1/day"))

    def test_episode_not_closed_yet_counts_as_not_proven(self):
        spread(self.conn, 11, 2, net=0.5, samples=None)          # no episodes row to join
        r, _ = self.size_row()
        self.assertEqual(r["n"], 0)

    def test_under_cost_is_not_attempted(self):
        spread(self.conn, 14, 10, net=0.01)                       # $0.10 each
        r, verdict = self.size_row()
        self.assertEqual(r["per_day"], 0.0)
        self.assertTrue(verdict.startswith("SHELVE"))

    def test_too_few_positive_days(self):
        spread(self.conn, 5, 5, net=0.5)                          # $3.68/day but 5 of 14 days
        r, verdict = self.size_row()
        self.assertGreater(r["per_day"], 3.0)
        self.assertEqual(r["days_pos"], 5)
        self.assertTrue(verdict.startswith("IN BETWEEN"))

    def test_one_day_is_most_of_it(self):
        spread(self.conn, 1, 30, net=0.5)                         # all on day 1
        r, verdict = self.size_row()
        self.assertAlmostEqual(r["max_share"], 1.0)
        self.assertTrue(verdict.startswith("SHELVE (the value that clears $1/day is mostly one day)"))

    def test_enough_days_but_one_day_is_most_of_it(self):
        """10 days with one $1,000 +0.5% probe each, then 40 on day 11:
        total 50 * 2.06125 = $103.06, $7.36/day, 11 days > 0, top day 40/50 = 80% > 75%."""
        spread(self.conn, 10, 1, net=0.5)
        for k in range(40):
            add_probe(self.conn, T0 + 10 * DAY + 12 * 3600 + 60 * k, net=0.5)
        r, verdict = self.size_row()
        self.assertEqual(r["days_pos"], 11)
        self.assertAlmostEqual(r["max_share"], 0.8)
        self.assertFalse(verdict.startswith("PASS"))
        self.assertTrue(verdict.startswith("SHELVE (the value that clears $1/day is mostly one day)"))

    def test_expected_value_per_attempt(self):
        self.assertAlmostEqual(dec11.expected(5.0), 0.425 * 4.85)
        self.assertEqual(dec11.expected(0.10), 0.0)          # under the $0.15 cost: not attempted
        self.assertEqual(dec11.expected(0.15), 0.0)

    def test_flashes_most_of_the_value(self):
        spread(self.conn, 11, 1, net=0.5)                         # counted: $1.62/day
        spread(self.conn, 11, 3, net=0.5, samples=1, pair="BTC")  # 3x as much in flashes
        r, verdict = self.size_row()
        self.assertAlmostEqual(r["flash_share"], 0.75)
        self.assertTrue(verdict.startswith("SHELVE (most of the value is one-scan flashes)"))

    def test_pass_needs_500_or_1000(self):
        spread(self.conn, 11, 2, net=0.5, size=5000.0)            # big, but at $5,000
        _, verdict = dec11.evaluate(self.conn, T0, END)
        self.assertTrue(verdict.startswith("IN BETWEEN"))

    def test_outside_the_window_and_cant_fill_ignored(self):
        add_probe(self.conn, T0 - DAY, 9.0)                       # before --since
        add_probe(self.conn, END + DAY, 9.0)                      # after --until
        add_probe(self.conn, T0 + DAY, None)                      # can't fill
        rows, verdict = dec11.evaluate(self.conn, T0, END)
        self.assertEqual(rows, [])
        self.assertTrue(verdict.startswith("NO DATA"))

    def test_whole_history_counts_days_from_the_first_probe(self):
        spread(self.conn, 11, 2, net=0.5)
        rows, _ = dec11.evaluate(self.conn, 0, END)
        self.assertEqual(rows[0]["days"], 14)

    def test_print(self):
        spread(self.conn, 11, 2, net=0.5)
        buf = io.StringIO()
        with redirect_stdout(buf):
            dec11.print_section(self.conn, T0, END)
        out = buf.getvalue()
        self.assertIn("DEC 11 RULE", out)
        self.assertIn("11/14", out)
        self.assertIn("VERDICT (mechanical, check by hand): PASS at $1,000", out)


if __name__ == "__main__":
    unittest.main()
