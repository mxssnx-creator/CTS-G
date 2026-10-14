"""Walk-forward reporting normalizes complete paid trade/event evidence."""
import pathlib
import sys
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hourly_walkforward as wf


class HourlyReturnTests(unittest.TestCase):
    def test_event_grouping_keeps_every_members_cost_in_denominator(self):
        # Ten already-net +1-cost returns share one event. Grouping does not
        # turn that +1-cost edge into a ten-cost edge on the PF scale.
        result = wf.judge(np.arange(10)*1000, np.ones(10), np.full(10, .002), 1, cost_pct=.2)
        self.assertAlmostEqual(result["metrics"]["pf"], 1.1)
        self.assertAlmostEqual(result["metrics"]["eventPf"], 1.1)
        self.assertEqual(result["metrics"]["events"], 1)

    def test_invalid_returns_cannot_count_as_events_or_positive_samples(self):
        result = wf.judge([1, 2], [1, -1], [float("nan"), float("inf")], 1)
        self.assertEqual(result["metrics"]["n"], 0)
        self.assertEqual(result["metrics"]["events"], 0)
        self.assertFalse(result["pass"])

    def test_funding_is_applied_before_stage_decisions(self):
        closes = dict(pair=np.array([0]), cell=np.array([0]), sym=np.array([0]), side=np.array([1]),
                      entry_t=np.array([wf.H_MS]), exit_t=np.array([2*wf.H_MS]), r=np.array([.0001]),
                      cost_pct=np.array(.3))
        seen = []

        def walk(tr, *args, **kwargs):
            seen.append(float(tr["r"][0]))
            return dict(stage=np.array([4]), inside=np.array([True]))

        with patch.object(wf.RP, "period_range", return_value=(0, 3*wf.H_MS)), \
             patch.object(wf.RP, "rank_symbols", return_value=["A"]), \
             patch.object(wf, "shadow_trades", return_value=closes), \
             patch.object(wf, "load_funding", return_value={}), \
             patch.object(wf, "funding_for", return_value=np.array([-.0002])), \
             patch.object(wf, "walk", side_effect=walk):
            result = wf.run_period("A", {}, "/unused", wf.SL.StageConfig(), ["pair"],
                                   {"tp": [.03], "slOfTp": [1], "trailOfTp": [0], "holdH": [2]},
                                   None, 1, "normal", "/unused", 1, cost=.003)
        self.assertAlmostEqual(seen[0], -.0001)
        self.assertAlmostEqual(result["metrics"]["pf"], 1+.1*(-.01)/.3)
        self.assertAlmostEqual(result["forward"]["Live"]["pf"], result["metrics"]["pf"])


if __name__ == "__main__":
    unittest.main()
