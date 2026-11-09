import unittest

from routes import RouteResult
from tracker import EpisodeTracker, MinuteStats

K = ("ETH", "dex>dex")


def rr(net_usd, key="dex>dex", buy="A", sell="B"):
    # usd_back chosen so net_usd comes out exactly (no fees/gas)
    return RouteResult(key, "ETH", buy, sell, 100.0, 100.0 + net_usd, 0.0, 0.0, 0.0)


class TestEpisodeTracker(unittest.TestCase):
    def test_opens_extends_and_closes(self):
        t = EpisodeTracker()
        self.assertEqual(t.update(1, 10.0, {K: rr(-0.1)}), [])
        self.assertEqual(t.update(2, 12.0, {K: rr(0.2)}), [])
        self.assertEqual(t.update(3, 14.0, {K: rr(0.5, buy="C")}), [])
        self.assertEqual(t.update(4, 16.0, {K: rr(0.1)}), [])
        (ep,) = t.update(5, 18.0, {K: rr(-0.3)})
        self.assertEqual((ep.start_block, ep.end_block, ep.blocks, ep.samples), (2, 4, 3, 3))
        self.assertAlmostEqual(ep.seconds, 4.0)
        self.assertAlmostEqual(ep.max_net_usd, 0.5)
        self.assertEqual(ep.best_buy, "C")
        self.assertEqual(t.open, {})

    def test_missing_route_closes_episode(self):
        t = EpisodeTracker()
        t.update(1, 0.0, {K: rr(0.2)})
        (ep,) = t.update(2, 2.0, {})
        self.assertEqual(ep.blocks, 1)

    def test_zero_is_not_profitable(self):
        t = EpisodeTracker()
        t.update(1, 0.0, {K: rr(0.0)})
        self.assertEqual(t.open, {})

    def test_flush_returns_open_episodes(self):
        t = EpisodeTracker()
        t.update(1, 0.0, {K: rr(0.2)})
        self.assertEqual(len(t.flush()), 1)
        self.assertEqual(t.open, {})


class TestMinuteStats(unittest.TestCase):
    def test_roll_over(self):
        m = MinuteStats()
        self.assertEqual(m.add(60.0, {K: rr(-0.2)}), [])
        self.assertEqual(m.add(90.0, {K: rr(0.1)}), [])
        (row,) = m.add(125.0, {K: rr(-0.5)})          # new minute -> previous one done
        self.assertEqual((row.minute, row.samples, row.positives), (60, 2, 1))
        self.assertAlmostEqual(row.best_net_pct, 0.1)
        self.assertAlmostEqual(row.avg_net_pct, -0.05)
        (row2,) = m.flush()
        self.assertEqual((row2.minute, row2.samples), (120, 1))


if __name__ == "__main__":
    unittest.main()
