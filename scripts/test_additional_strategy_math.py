"""Deterministic checks for Block/DCA volume, weighted averages and outcomes.

These tests keep the additional strategies independent from the exchange.  A
small candle tape forces one addition so every ratio can be compared against a
manually calculated weighted entry and a different net result.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from replay_five_days import replay


def tape_for_block():
    # Long entry at 100, then a favourable close at 100.30 (Block add),
    # followed by a protected adverse close.  The short tape is deliberately
    # longer than the warmup so the split and terminal close are causal.
    prices = [100.0, 100.0, 100.0, 100.30, 99.70, 99.70, 99.70, 99.70, 99.70, 99.70]
    return [[p, p + 0.01, p - 0.01, p] for p in prices]


def tape_for_dca():
    # Long entry at 100, then a 0.30% adverse close (DCA add), followed by a
    # recovery that exits every lane at the same terminal boundary.
    prices = [100.0, 100.0, 100.0, 99.70, 100.20, 100.20, 100.20, 100.20, 100.20, 100.20]
    return [[p, p + 0.01, p - 0.01, p] for p in prices]


def signals(n):
    # One causal entry; no later signal may reopen the boundary-closed lane.
    return [(1, 1.0) if i == 2 else (0, 0.0) for i in range(n)]


class AdditionalStrategyMathTests(unittest.TestCase):
    def run_lanes(self, strategy, tape):
        ratios = (.25, .5, 1.0)
        cfg = [dict(strategy=strategy, levels=1, incrementPct=.2,
                    volumeRatio=ratio, tpPct=5.0, slPct=5.0)
               for ratio in ratios]
        return ratios, replay(tape, signals(len(tape)), 1, cfg,
                              warmup=2, cost_pct=0.0,
                              min_pf=1.05, max_dd_s=57600)

    def test_block_ratio_changes_quantity_average_and_result(self):
        ratios, rows = self.run_lanes("block", tape_for_block())
        self.assertEqual([round(r["maxVolume"], 6) for r in rows], [1.25, 1.5, 2.0])
        # 100 and 100.30 weighted by the added quantity.
        expected = [100.06, 100.10, 100.15]
        for row, value in zip(rows, expected):
            self.assertAlmostEqual(row["avgEntryPrice"], value, places=6)
            self.assertEqual(row["entryExecutions"], 2)
        self.assertEqual(len({r["netPct"] for r in rows}), 3)
        self.assertEqual(len({r["avgEntryPrice"] for r in rows}), 3)

    def test_dca_ratio_changes_quantity_average_and_result(self):
        ratios, rows = self.run_lanes("dca", tape_for_dca())
        self.assertEqual([round(r["maxVolume"], 6) for r in rows], [1.25, 1.5, 2.0])
        expected = [99.94, 99.90, 99.85]
        for row, value in zip(rows, expected):
            self.assertAlmostEqual(row["avgEntryPrice"], value, places=6)
            self.assertEqual(row["entryExecutions"], 2)
        self.assertEqual(len({r["netPct"] for r in rows}), 3)
        self.assertEqual(len({r["avgEntryPrice"] for r in rows}), 3)

    def test_block_cap_is_explicit_and_not_compounded(self):
        tape = tape_for_block()
        cfg = [dict(strategy="block", levels=6, incrementPct=.2,
                    volumeRatio=1.5, maxVolumeMultiplier=2.0,
                    tpPct=5.0, slPct=5.0)]
        row = replay(tape, signals(len(tape)), 1, cfg, warmup=2,
                     cost_pct=0.0)[0]
        # Six 1.5-ratio targets are capped at 2x the parent, rather than
        # compounded into 10x; the cap is part of the reported policy.
        self.assertAlmostEqual(row["maxVolume"], 2.0)
        self.assertLessEqual(row["entryQtyTotal"], 3.0)
        self.assertGreater(row["avgEntryPrice"], 100.0)


if __name__ == "__main__":
    unittest.main()
