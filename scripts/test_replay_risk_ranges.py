"""Boundary tests for TP/SL percentages, SL:TP ratios and trailing exits."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server" / "pulse"))
from replay_five_days import replay
from position_cost import SL_TP_MIN, SL_TP_MAX, SL_TP_STEP, sl_tp_grid
from risk_variants import trail_grid, give_from_arm, TRAIL_ARM_MIN, TRAIL_ARM_MAX, TRAIL_GIVE_MIN, TRAIL_GIVE_MAX


def bars(prices):
    return [[p, p + .001, p - .001, p] for p in prices]


def one_entry(n):
    return [(1, 1.0) if i == 2 else (0, 0.0) for i in range(n)]


class ReplayRiskRangeTests(unittest.TestCase):
    def test_sl_tp_grid_is_inclusive_and_exact_step(self):
        g = sl_tp_grid()
        self.assertEqual(g[0], SL_TP_MIN)
        self.assertEqual(g[-1], SL_TP_MAX)
        self.assertEqual(len(g), 30)
        self.assertTrue(all(abs((b - a) - SL_TP_STEP) < 1e-9 for a, b in zip(g, g[1:])))
        # Reversed bounds are normalized to the same ascending grid.
        self.assertEqual(sl_tp_grid(3.0, .1, .1), g)

    def test_tp_and_sl_are_percent_values_and_same_bar_stops_win(self):
        # tpPct/slPct are expressed in percent in the research config.  A
        # 0.5% target from 100 is 100.5; same-candle SL+TP resolves to SL.
        cfg = [dict(strategy="base", levels=0, incrementPct=0, volumeRatio=0,
                    tpPct=.5, slPct=.3)]
        close = []
        result = replay([[100, 100.51, 99.5, 100], [100, 100.01, 99.99, 100],
                         [100, 100.01, 99.99, 100], [100, 100.51, 99.5, 100.0],
                         [100, 100.01, 99.99, 100.0], [100, 100.01, 99.99, 100.0],
                         [100, 100.01, 99.99, 100.0], [100, 100.01, 99.99, 100.0]],
                        one_entry(8), 1, cfg, warmup=2, cost_pct=.10,
                        on_close=close.append)[0]
        self.assertEqual(close[0]["reason"], "sl")
        self.assertAlmostEqual(close[0]["exit"], 99.7, places=6)
        self.assertEqual(result["n"], 1)

        close = []
        result = replay(bars([100, 100, 100, 100.51, 100.51, 100.51, 100.51, 100.51]),
                        one_entry(8), 1, cfg, warmup=2, cost_pct=.10,
                        on_close=close.append)[0]
        self.assertEqual(close[0]["reason"], "tp")
        self.assertAlmostEqual(close[0]["exit"], 100.5, places=6)
        # 0.05% entry + 0.05% exit is subtracted once from the 0.5% move.
        self.assertAlmostEqual(result["costPct"], .10025, places=5)
        self.assertAlmostEqual(result["netPct"], .39975, places=5)

    def test_trailing_arm_and_give_are_independent_and_causal(self):
        cfg = [dict(strategy="trail", levels=0, incrementPct=0, volumeRatio=0,
                    tpPct=5.0, slPct=5.0, trailArmPct=.3, trailGivePct=.1),
               dict(strategy="trail", levels=0, incrementPct=0, volumeRatio=0,
                    tpPct=5.0, slPct=5.0, trailArmPct=.3, trailGivePct=.5)]
        closes = [[] for _ in cfg]
        trail_bars = [[100, 100.01, 99.99, 100], [100, 100.01, 99.99, 100],
                      [100, 100.01, 99.99, 100], [100, 100.41, 100.39, 100.4],
                      [100.4, 100.41, 100.39, 100.4], [100.1, 100.41, 99.8, 99.8],
                      [100.1, 100.41, 99.8, 99.8], [100.1, 100.41, 99.8, 99.8],
                      [100.1, 100.41, 99.8, 99.8], [100.1, 100.41, 99.8, 99.8]]
        result = replay(trail_bars,
                        one_entry(10), 1, cfg, warmup=2, cost_pct=0.0,
                        on_close=lambda r: closes[r["config"]].append(r))[0:]
        self.assertEqual([x[0]["reason"] for x in closes], ["trail", "trail"])
        # The tighter give (0.1%) keeps 100.4*(1-.001)=100.2996, so the
        # adverse gap opens at 100.1; the wider give keeps 99.898 and fills
        # at that stop.
        self.assertAlmostEqual(closes[0][0]["exit"], 100.1, places=6)
        self.assertAlmostEqual(closes[1][0]["exit"], 99.898, places=6)
        self.assertNotEqual(result[0]["netPct"], result[1]["netPct"])

    def test_trail_grid_and_give_clamps_cover_full_ranges(self):
        g = trail_grid()
        self.assertEqual(len(g), 25)
        self.assertEqual((g[0][1], g[-1][1]), (TRAIL_ARM_MIN, TRAIL_ARM_MAX))
        self.assertEqual(min(x[2] for x in g), TRAIL_GIVE_MIN)
        self.assertEqual(max(x[2] for x in g), TRAIL_GIVE_MAX)
        self.assertEqual(give_from_arm(.3, 1/3, .1, .5), .1)
        self.assertEqual(give_from_arm(1.5, 1/3, .1, .5), .5)


if __name__ == "__main__":
    unittest.main()
