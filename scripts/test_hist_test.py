#!/usr/bin/env python3
"""Historic test: 4–64h window, min PF 1.1, fill until selected count is positive."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server", "pulse"))

import hist_test as ht  # noqa: E402
from position_cost import PF_MAX, PF_MIN, POSITIVE_PF  # noqa: E402


class HistTestContract(unittest.TestCase):
    def test_hours_clamp_4_to_64_default_20(self):
        self.assertEqual(ht.clamp_hours(None), 20)
        self.assertEqual(ht.clamp_hours(20), 20)
        self.assertEqual(ht.clamp_hours(4), 4)
        self.assertEqual(ht.clamp_hours(64), 64)
        self.assertEqual(ht.clamp_hours(1), 4)
        self.assertEqual(ht.clamp_hours(99), 64)
        self.assertEqual(ht.lookback_bars(20), 1200)
        self.assertEqual(ht.lookback_bars(4), 240)
        self.assertEqual(ht.lookback_bars(64), 3840)

    def test_min_pf_defaults_to_positive_floor(self):
        self.assertEqual(ht.clamp_min_pf(None), POSITIVE_PF)
        self.assertEqual(ht.clamp_min_pf(1.1), 1.1)
        self.assertEqual(ht.clamp_min_pf(0.8), PF_MIN)
        self.assertEqual(ht.clamp_min_pf(2.5), PF_MAX)

    def test_symbol_positive_requires_fills_and_floor(self):
        self.assertTrue(ht.symbol_clears_floor({"n": 30, "pf": 1.21}, 1.1))
        self.assertFalse(ht.symbol_clears_floor({"n": 30, "pf": 1.04}, 1.1))
        self.assertFalse(ht.symbol_clears_floor({"n": 0, "pf": 3.0}, 1.1))
        self.assertFalse(ht.symbol_clears_floor({}, 1.1))

    def test_fill_keeps_evaluating_until_selected_count(self):
        result = ht.self_test()
        failed = result.get("failed") or []
        self.assertTrue(result["ok"], failed)
        self.assertGreaterEqual(result["pass"], 18)

    def test_overlay_carries_hours_and_min_pf(self):
        ov = ht.test_overlay(20, 1.1)
        self.assertEqual(ov["histTestHours"], 20)
        self.assertEqual(ov["histLookbackBars"], 1200)
        self.assertEqual(ov["histTestMinPf"], 1.1)
        self.assertEqual(ov["setMinPf"], 1.1)
        self.assertFalse(ov["dcaEnabled"])
        self.assertTrue(ov["stratBlock"])

    def test_fill_walks_ranked_queue_until_target(self):
        queue = [{"symbol": "LOSER"}, {"symbol": "WIN1"}, {"symbol": "WIN2"}, {"symbol": "WIN3"}]

        def fetch(symbol, limit):
            return [[0, 1, 1, 1, 1, 1]] * 80

        def score(symbol, bars):
            return {"n": 12, "pf": 1.3 if symbol.startswith("WIN") else 0.7}

        fill = ht.fill_positive(queue, 2, 1.1, {"histTestHours": 20, "histLookbackBars": 80, "histWarmup": 0}, fetch, score_fn=score)
        self.assertEqual([r["symbol"] for r in fill["selected"]], ["WIN1", "WIN2"])
        self.assertEqual(fill["filled"], 2)
        self.assertEqual(fill["evaluated"], 3)
        self.assertEqual(fill["short"], 0)
        self.assertIn("LOSER", [r["symbol"] for r in fill["rejected"]])
        self.assertNotIn("WIN3", [r["symbol"] for r in fill["selected"] + fill["rejected"]])

    def test_start_stop_does_not_touch_hist_calc_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            public = os.path.join(tmp, "hist-test.json")
            summary = os.path.join(tmp, "summary.json")
            sweep = os.path.join(tmp, "step-sweep.json")
            with patch.object(ht, "PUBLIC_JSON", public), patch.object(ht, "SUMMARY_PATH", summary), patch.object(ht, "PUBLIC_SWEEP", sweep), patch.object(ht, "OUT_DIR", tmp):
                idle = ht.read_job()
                self.assertEqual(idle["phase"], "idle")
                self.assertEqual(idle["hours"], 20)
                stopped = ht.stop_test()
                self.assertFalse(stopped.get("running"))
                self.assertTrue(os.path.exists(public))
                with open(public, encoding="utf-8") as handle:
                    blob = json.load(handle)
                self.assertIn(blob.get("phase"), ("idle", "stopped"))
                self.assertNotIn("hist-calc", json.dumps(blob))


if __name__ == "__main__":
    unittest.main()
