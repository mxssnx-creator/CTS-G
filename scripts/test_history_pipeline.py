#!/usr/bin/env python3
"""Focused no-network tests for durable rolling 1m history."""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
PULSE = ROOT / "server" / "pulse"
sys.path.insert(0, str(PULSE))

import hist_calc  # noqa: E402
from history_store import HistoryStore, parse_exchange_rows  # noqa: E402


def bar(close: float) -> list[float]:
    return [close - 0.2, close + 0.3, close - 0.4, close, 100.0]


class HistoryPipelineTests(unittest.TestCase):
    def make_store(self, directory: str, lane: str = "bingx-x01") -> HistoryStore:
        return HistoryStore(
            lane,
            retention_bars=8,
            path=str(pathlib.Path(directory) / f"history-{lane}.json"),
            checkpoint_path=str(pathlib.Path(directory) / f"checkpoint-{lane}.json"),
        )

    def test_exchange_timestamp_parsing_keeps_ohlcv(self) -> None:
        rows = parse_exchange_rows([[1_700_000_000_000, "10", "11", "9", "10.5", "42"]])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["minute"], 28_333_333)
        self.assertEqual(rows[0]["bar"], [10.0, 11.0, 9.0, 10.5, 42.0])

    def test_overlap_dedupe_and_exchange_preference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.merge_bar("X-USDT", 100, bar(10), source="mark")
            first = store.merge("X-USDT", [{"minute": 100, "bar": bar(11), "source": "exchange"}], source="exchange")
            second = store.merge("X-USDT", [{"minute": 100, "bar": bar(12), "source": "mark"}], source="mark")
            self.assertEqual(first["replaced"], 1)
            self.assertEqual(second["ignored"], 1)
            self.assertEqual(store.window("X-USDT", bars=1, end=100), [bar(11)])

    def test_restart_load_and_retention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(pathlib.Path(directory) / "history.json")
            checkpoint = str(pathlib.Path(directory) / "checkpoint.json")
            store = HistoryStore("bingx-x01", retention_bars=120, path=path, checkpoint_path=checkpoint)
            store.merge("X-USDT", [{"minute": minute, "bar": bar(float(minute))} for minute in range(1, 126)], source="exchange")
            restarted = HistoryStore("bingx-x01", retention_bars=120, path=path, checkpoint_path=checkpoint)
            self.assertEqual([row["minute"] for row in restarted.records("X-USDT")], list(range(6, 126)))
            self.assertEqual(restarted.watermark("X-USDT", source="exchange"), 125)

    def test_bulk_merge_defers_persist_until_flush(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(pathlib.Path(directory) / "history.json")
            checkpoint = str(pathlib.Path(directory) / "checkpoint.json")
            store = HistoryStore("bingx-x01", retention_bars=120, path=path, checkpoint_path=checkpoint)
            store.merge("X-USDT", [{"minute": 1, "bar": bar(10.0)}], source="exchange", persist=False)
            self.assertFalse(os.path.exists(path))
            store.merge("Y-USDT", [{"minute": 1, "bar": bar(11.0)}], source="exchange", persist=False)
            self.assertFalse(os.path.exists(path))
            self.assertTrue(store.flush())
            self.assertTrue(os.path.exists(path))
            restarted = HistoryStore("bingx-x01", retention_bars=120, path=path, checkpoint_path=checkpoint)
            self.assertEqual(len(restarted.records("X-USDT")), 1)
            self.assertEqual(len(restarted.records("Y-USDT")), 1)

    def test_persist_debounce_skips_immediate_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(pathlib.Path(directory) / "history.json")
            checkpoint = str(pathlib.Path(directory) / "checkpoint.json")
            store = HistoryStore("bingx-x01", retention_bars=120, path=path, checkpoint_path=checkpoint)
            store._persist_interval_s = 60.0
            store.merge("X-USDT", [{"minute": 1, "bar": bar(10.0)}], source="exchange")
            first_mtime = os.path.getmtime(path)
            store.merge("X-USDT", [{"minute": 2, "bar": bar(11.0)}], source="exchange")
            self.assertEqual(os.path.getmtime(path), first_mtime)
            self.assertTrue(store.flush())
            self.assertGreaterEqual(os.path.getmtime(path), first_mtime)
            restarted = HistoryStore("bingx-x01", retention_bars=120, path=path, checkpoint_path=checkpoint)
            self.assertEqual([row["minute"] for row in restarted.records("X-USDT")], [1, 2])

    def test_missing_ranges_are_contiguous_and_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.merge("X-USDT", [{"minute": minute, "bar": bar(100 + minute)} for minute in (1, 2, 5, 8)], source="exchange")
            gaps = store.missing_ranges("X-USDT", 1, 8, source="exchange")
            self.assertEqual([(gap["start"], gap["end"], gap["minutes"]) for gap in gaps], [(3, 4, 2), (6, 7, 2)])
            self.assertFalse(store.coverage("X-USDT", 1, 8, source="exchange")["contiguous"])

    def test_lane_isolation_and_checkpoint_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            live = self.make_store(directory, "bingx-x01")
            vst = self.make_store(directory, "bingx-x02")
            live.merge_bar("X-USDT", 7, bar(7), source="exchange")
            self.assertEqual(vst.records("X-USDT"), [])
            saved = live.checkpoint({"runId": "run-1", "generation": 4, "watermark": {"X-USDT": 7}})
            self.assertEqual(live.checkpoint()["runId"], "run-1")
            self.assertEqual(saved["generation"], 4)

    def test_gap_backfill_request_can_be_derived_from_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.merge("X-USDT", [{"minute": minute, "bar": bar(100 + minute)} for minute in range(1, 4)], source="exchange")
            gaps = store.missing_ranges("X-USDT", 1, 6, source="exchange")
            self.assertEqual(gaps[0]["start"], 4)
            self.assertEqual(gaps[0]["end"], 6)
            self.assertEqual(gaps[0]["source"], "exchange")

    def test_explicit_one_hour_request_keeps_sixty_bar_window(self) -> None:
        pulse_source = (PULSE / "pulse_trader.py").read_text()
        self.assertIn("lookback = max(60, min(20160", pulse_source)
        self.assertNotIn("lookback = max(120, min(20160", pulse_source)
        self.assertEqual(hist_calc.hours_to_bars(1), 60)
        self.assertEqual(hist_calc.parse_options({"hours": 1})["hours"], 1)

    def test_one_hour_calc_reports_evaluation_and_stage_timing(self) -> None:
        body = {
            "synth": True,
            "hours": 1,
            "symbols": ["XRP-USDT"],
            "allConfigs": False,
            "minStep": 1,
            "stepMax": 1,
            "trailing": False,
            "stratIndications": False,
            "stratGeneral": True,
            "stratBlock": False,
            "stratDca": False,
            "workers": 1,
        }
        job = hist_calc.run_calc(body, persist=False)
        repeat = hist_calc.run_calc(body, persist=False)
        self.assertEqual(job.get("phase"), "ready")
        self.assertFalse(job.get("error"))
        self.assertEqual(job.get("hours"), 1)
        self.assertEqual(job.get("lookback"), 60)
        self.assertEqual(job.get("evaluationBars"), 60)
        self.assertEqual(job.get("warmupBars"), 30)
        self.assertEqual(job.get("requestedBars"), 90)
        timings = job.get("timings") or {}
        self.assertTrue({"fetchMs", "replayWallMs", "mergeMs", "scoreMs", "reportMs", "totalMs"} <= set(timings))
        coverage = job.get("coverage") or {}
        for key in ("symbols", "bars", "evaluationBars", "sets", "evaluations", "tasks"):
            self.assertEqual((coverage.get(key) or {}).get("coveragePct"), 100.0, key)
        compact = lambda value: sorted(
            (row.get("id"), row.get("direction"), row.get("n"), row.get("last15Ratio"), row.get("maxDdS"))
            for row in value.get("rows") or []
        )
        self.assertEqual(compact(job), compact(repeat))
        self.assertEqual(
            {key: (job.get("coverage") or {}).get(key) for key in ("product", "histFills", "validatedCount")},
            {key: (repeat.get("coverage") or {}).get(key) for key in ("product", "histFills", "validatedCount")},
        )

    def test_manual_requests_are_generation_safe_and_shared(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def scoped_path(name: str) -> str:
                return str(pathlib.Path(directory) / name)

            with patch.object(hist_calc, "path_for", side_effect=scoped_path):
                first = hist_calc.start_job({"symbols": ["*"], "hours": 7}, connection="bingx-x01")
                second = hist_calc.start_job({"symbols": ["SOL-USDT"], "hours": 4}, connection="bingx-x01")
                request = hist_calc.read_request("bingx-x01")

            self.assertEqual(first["generation"] + 1, second["generation"])
            self.assertEqual(request["runId"], second["runId"])
            self.assertEqual(request["generation"], second["generation"])
            self.assertTrue(second["shared"])
            self.assertFalse(second["independent"])
            self.assertEqual(second["phase"], "queued")
            self.assertEqual(request["options"]["hours"], 4)

    def test_queued_request_keeps_last_published_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def scoped_path(name: str) -> str:
                return str(pathlib.Path(directory) / name)

            with patch.object(hist_calc, "path_for", side_effect=scoped_path):
                published = hist_calc.idle_job("bingx-x01")
                published.update({
                    "phase": "ready",
                    "ready": True,
                    "runId": "published-run",
                    "generation": 8,
                    "rows": [{"id": "general:1m:sl0.6:st1"}],
                    "coverage": {"symbols": {"completed": 2}},
                    "lastPublishedWatermark": {"SOL-USDT": 10},
                })
                hist_calc.write_job(published, "bingx-x01")
                queued = hist_calc.start_job({"symbols": ["SOL-USDT"], "hours": 7}, connection="bingx-x01")

            self.assertEqual(queued["phase"], "queued")
            self.assertTrue(queued["stale"])
            self.assertEqual(queued["rows"], published["rows"])
            self.assertEqual(queued["lastPublishedWatermark"], {"SOL-USDT": 10})
            self.assertEqual(queued["coverage"], published["coverage"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
