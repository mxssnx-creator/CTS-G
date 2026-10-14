"""Hourly bar store: completed bars only, paging, gaps and persistence."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server", "pulse"))

from hourly_bars import H_MS, PAGE_MAX, HourlyBars, parse_klines, window_arrays  # noqa: E402

NOW = 1_791_990_000_000  # 2026-10-14 ~17:40 UTC
CUR = NOW // H_MS * H_MS  # open time of the bar still forming


def kline(t, px=100.0):
    return {"open": str(px), "high": str(px + 1), "low": str(px - 1), "close": str(px + 0.5), "volume": "10", "time": t}


class ParseTests(unittest.TestCase):
    def test_drops_forming_bar_and_bad_rows(self):
        data = [kline(CUR), kline(CUR - H_MS), kline(CUR - 2 * H_MS), kline(CUR - H_MS + 1), {"open": "x"}, kline(CUR - 3 * H_MS, px=-1)]
        rows = parse_klines(data, NOW)
        self.assertEqual([r[0] for r in rows], [CUR - 2 * H_MS, CUR - H_MS])
        self.assertEqual(rows[-1][1:], (100.0, 101.0, 99.0, 100.5, 10.0))

    def test_list_rows_and_newest_first_input(self):
        rows = parse_klines([[CUR - H_MS, 1, 2, 0.5, 1.5, 3], [CUR - 2 * H_MS, 1, 2, 0.5, 1.5, 3]], NOW)
        self.assertEqual([r[0] for r in rows], [CUR - 2 * H_MS, CUR - H_MS])


class StoreTests(unittest.TestCase):
    def test_merge_dedupes_caps_and_counts_gaps(self):
        s = HourlyBars(keep=5, min_bars=3)
        rows = parse_klines([kline(CUR - k * H_MS) for k in range(1, 8)], NOW)
        self.assertEqual(s.merge("X", rows), 7)
        self.assertEqual(s.count("X"), 5)
        self.assertEqual(s.merge("X", rows[-2:]), 0)
        self.assertTrue(s.ready("X"))
        self.assertEqual(s.gaps("X"), 0)
        s2 = HourlyBars(keep=10, min_bars=3)
        s2.merge("Y", parse_klines([kline(CUR - k * H_MS) for k in (1, 2, 5)], NOW))
        self.assertEqual(s2.gaps("Y"), 2)

    def test_boot_requests_page_the_window(self):
        reqs = HourlyBars.boot_requests("X", 1200, NOW)
        self.assertEqual(len(reqs), 2)
        self.assertEqual(int(reqs[0]["startTime"]), CUR - 1200 * H_MS)
        self.assertEqual(int(reqs[-1]["endTime"]), CUR - 1)
        self.assertEqual(int(reqs[1]["startTime"]), CUR - 1200 * H_MS + PAGE_MAX * H_MS)

    def test_boot_uses_public_and_persistence_round_trips(self):
        def public(path, params):
            start, end = int(params["startTime"]), int(params["endTime"])
            return {"code": 0, "data": [kline(t) for t in range(start, end + 1, H_MS)][::-1]}

        s = HourlyBars(keep=1500, min_bars=1200)
        self.assertEqual(s.boot("X", public, NOW, bars=1200), 1200)
        self.assertTrue(s.ready("X"))
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "hourly-bars-test.json")
            s.save(path)
            again = HourlyBars(keep=1500, min_bars=1200)
            self.assertEqual(again.load(path, now_ms=NOW), 1200)
            self.assertEqual(again.arrays("X")["t"].tolist(), s.arrays("X")["t"].tolist())
            # A restart before the newest bar completed must not load it.
            self.assertEqual(HourlyBars().load(path, now_ms=CUR - H_MS + 1), 1199)
        win = window_arrays(s, ["X", "missing"], 1000)
        self.assertEqual(list(win), ["X"])
        self.assertEqual(len(win["X"]["c"]), 1000)


if __name__ == "__main__":
    unittest.main()
