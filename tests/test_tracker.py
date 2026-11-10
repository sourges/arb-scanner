import unittest

from routes import RouteResult
from tracker import EpisodeTracker, MinuteStats

K = ("ETH", "dex>dex")


def rr(net_usd, key="dex>dex", buy="A", sell="B", base=0.05):
    # usd_back chosen so net_usd comes out exactly (no fees/gas); 0.05 base -> buy_px 2000
    return RouteResult(key, "ETH", buy, sell, 100.0, 100.0 + net_usd, 0.0, 0.0, 0.0, base)


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

    def test_prices_recorded_at_start_and_best_moment(self):
        t = EpisodeTracker()
        t.update(1, 0.0, {K: rr(0.2)}, {"ETH": 2000.0})
        t.update(2, 2.0, {K: rr(0.5)}, {"ETH": 1990.0})     # best moment
        t.update(3, 4.0, {K: rr(0.1)}, {"ETH": 1985.0})     # positive but not best
        (ep,) = t.update(4, 6.0, {K: rr(-0.1)}, {"ETH": 1984.0})
        self.assertEqual((ep.start_mid, ep.best_mid), (2000.0, 1990.0))
        self.assertAlmostEqual(ep.best_buy_px, 2000.0)           # 100 / 0.05
        self.assertAlmostEqual(ep.best_sell_px, 100.5 / 0.05)    # 2010.0 at the best moment

    def test_missing_mids_leave_none(self):
        t = EpisodeTracker()
        t.update(1, 0.0, {K: rr(0.2)})
        (ep,) = t.flush()
        self.assertIsNone(ep.start_mid)
        self.assertIsNone(ep.best_mid)


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
