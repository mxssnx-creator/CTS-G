"""No-network regressions for snapshot consistency and bounded cycle diagnostics."""
import copy
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
import pulse_http as http
import pulse_trader as trader


class OverallPerformanceTests(unittest.TestCase):
    def test_merge_reads_each_lane_once_preserves_failures_and_per_lane_progress(self):
        states = {}
        for i, lane in enumerate(http.LANES):
            states[lane["id"]] = {"running": True, "open": [{"symbol": "X-USDT", "side": "LONG", "qty": i + 1}],
                "openCount": 1, "closed": [], "sets": {"progress": {"phase": "replay", "pct": 10 + i * 70}},
                "tests": [{"name": f"failure-{i}", "pass": False, "t": i}] +
                         [{"name": f"ok-{i}-{j}", "pass": True, "t": 10 + j} for j in range(23)]}
        before = copy.deepcopy(states)
        with patch.object(http, "load_stats", side_effect=lambda cid: states[cid]) as reads, \
             patch.object(http, "unit_state", return_value="active"), \
             patch.object(http, "stats_age", return_value=1), \
             patch.object(http.os.path, "exists", return_value=False):
            result = http.merge_overall()
        self.assertEqual(reads.call_count, len(http.LANES))
        self.assertTrue(result["running"])
        self.assertEqual(result["openCount"], len(http.LANES))
        self.assertEqual(len(result["tests"]), 24)
        self.assertEqual([t["name"] for t in result["tests"] if not t["pass"]], ["failure-1", "failure-0"])
        self.assertEqual({t["connection"] for t in result["tests"] if not t["pass"]}, set(states))
        self.assertEqual(result["sets"]["progress"]["phase"], "lanes")
        self.assertIsNone(result["sets"]["progress"]["pct"])
        self.assertEqual([l["pct"] for l in result["progress"]["lanes"]], [10, 80])
        self.assertEqual(states, before, "cached input snapshots must remain immutable")

    def test_old_running_snapshot_cannot_mark_inactive_services_as_running(self):
        with patch.object(http, "load_stats", return_value={"running": True}), \
             patch.object(http, "unit_state", return_value="inactive"), \
             patch.object(http, "stats_age", return_value=100), \
             patch.object(http.os.path, "exists", return_value=False):
            result = http.merge_overall()
        self.assertFalse(result["running"])
        self.assertTrue(result["halted"])

    def test_slow_computation_is_not_misclassified_as_network_io(self):
        p = trader.Pulse.__new__(trader.Pulse)
        p.did_io = False; p.hist_busy = False; p._cycle_stage_ms = {"entries": 1000.0}
        with patch.object(trader.time, "perf_counter", return_value=11), \
             patch.object(trader.time, "thread_time", return_value=3):
            p._finish_cycle_timing(10, 2)
        self.assertEqual(p.last_scan_ms, 1000)
        self.assertEqual(p.last_scan_cpu_ms, 1000)
        self.assertTrue(p.cycle_overrun)
        self.assertFalse(p.last_scan_io)
        self.assertEqual(p.last_cycle_stages, {"entries": 1000.0})
        p._cycle_stage_ms.clear()
        self.assertEqual(p.last_cycle_stages, {"entries": 1000.0})

    def test_io_latency_remains_visible_with_separate_cpu_time(self):
        p = trader.Pulse.__new__(trader.Pulse)
        p.did_io = True; p.hist_busy = False; p._cycle_stage_ms = {"controls": 5000.0}
        with patch.object(trader.time, "perf_counter", return_value=15), \
             patch.object(trader.time, "thread_time", return_value=2.01):
            p._finish_cycle_timing(10, 2)
        self.assertEqual(p.last_scan_ms, 5000)
        self.assertAlmostEqual(p.last_scan_cpu_ms, 10)
        self.assertTrue(p.last_scan_io)
        self.assertFalse(p.cycle_overrun)  # Existing compute-load governor is preserved.

    def test_stage_accounting_is_bounded_and_exceptions_clear_active_stage(self):
        p = trader.Pulse.__new__(trader.Pulse); p._cycle_stage_ms = {}
        def failed():
            self.assertEqual(p._active_cycle_stage, "controls")
            raise RuntimeError("temporary")
        with patch.object(trader.time, "perf_counter", side_effect=[1, 1.125, 2, 2.125]):
            for _ in range(2):
                with self.assertRaises(RuntimeError): p._cycle_step("controls", failed)
        self.assertEqual(p._active_cycle_stage, "")
        self.assertEqual(p._cycle_stage_ms, {"controls": 250.0})


if __name__ == "__main__":
    unittest.main()
