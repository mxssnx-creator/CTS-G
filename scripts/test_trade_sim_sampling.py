#!/usr/bin/env python3
"""Report-only gate sampling must not change the hourly execution timeline."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trade_sim import report_sample_times


class ReportSampling(unittest.TestCase):
    def test_default_hourly_sampling_keeps_every_report_hour(self):
        hours = list(range(1000, 1000 + 168 * 3600, 3600))
        self.assertEqual(report_sample_times(hours, 3600), hours)

    def test_long_replay_can_sample_stage_grid_every_six_hours_and_keep_final(self):
        hours = list(range(1000, 1000 + 168 * 3600, 3600))
        samples = report_sample_times(hours, 6 * 3600)
        self.assertEqual(samples[0], hours[0])
        self.assertEqual(samples[-1], hours[-1])
        self.assertEqual(len(samples), 29)
        self.assertTrue(all(b - a == 6 * 3600 for a, b in zip(samples, samples[1:-1])))

    def test_subhour_sampling_is_global_and_empty_input_is_safe(self):
        hours = [3600, 7200, 10800]
        self.assertEqual(report_sample_times(hours, 600), list(range(3600, 10801, 600)))
        self.assertEqual(report_sample_times([], 3600), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
