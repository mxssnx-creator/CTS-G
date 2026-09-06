"""Prev-position window/count coverage checks (5..55, step 5).

The previous segment is deliberately distinct from the current segment.  The
test catches the old implementation that used the latest N rows for both
Last and Prev and the old 24-position clipping.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server" / "pulse"))
from coord_engine import Coordinator


def rows(n=110):
    # Every row is a completed, cost-nettable round trip.  Timestamps are
    # intentionally chronological so the window ordering is unambiguous.
    return [dict(t=i, pnl_pct=.01, pnl=.01, qty=1, entry=100,
                 position_cost_pct=.10, symbol="X-USDT", side="LONG")
            for i in range(n)]


class PrevCoordinationTests(unittest.TestCase):
    def test_every_window_and_min_count_is_represented(self):
        tape = rows()
        for window in range(5, 56, 5):
            for minimum in range(5, 56, 5):
                c = Coordinator()
                c.load({}, {"additionalCoordination": True,
                        "coordOptimizationN": 50,
                        "prevPosWindow": window,
                        "prevPosMinCount": minimum,
                        "positionCostPct": .10,
                        "minPf": 1.05})
                allowed, reasons, metrics = c.gate(tape, 0, {"pf": 1.5, "n": 20})
                self.assertEqual(c.prev_window, window)
                self.assertEqual(c.prev_min_count, minimum)
                self.assertEqual(metrics["prevCount"], window)
                self.assertEqual(metrics["prevWindow"], window)
                self.assertGreaterEqual(metrics["prevPf"], 1.05)

    def test_prev_uses_the_segment_before_current(self):
        tape = rows()
        # Make the current segment negative while the immediately preceding
        # segment is positive.  Prev must retain the positive PF; Last must
        # observe the negative tail.
        for row in tape[-55:]:
            row["pnl_pct"] = -.003
            row["pnl"] = -.003
        c = Coordinator()
        c.load({}, {"additionalCoordination": True, "coordOptimizationN": 50,
                "prevPosWindow": 55, "prevPosMinCount": 5,
                "positionCostPct": .10, "minPf": 1.05})
        _, _, metrics = c.gate(tape, 0, {"pf": 1.5, "n": 20})
        self.assertEqual(metrics["prevCount"], 55)
        self.assertGreater(metrics["prevPf"], 1.05)
        self.assertLess(metrics["lastPf"], 1.05)


if __name__ == "__main__":
    unittest.main()
