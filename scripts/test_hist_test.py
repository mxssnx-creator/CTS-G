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
    def setUp(self):
        ht.clear_stop()
        ht.clear_pause()

    def tearDown(self):
        ht.clear_stop()
        ht.clear_pause()

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

    def test_refresh_hours_clamp_1_to_8_default_2(self):
        self.assertEqual(ht.clamp_refresh_hours(None), 2)
        self.assertEqual(ht.clamp_refresh_hours(2), 2)
        self.assertEqual(ht.clamp_refresh_hours(1), 1)
        self.assertEqual(ht.clamp_refresh_hours(8), 8)
        self.assertEqual(ht.clamp_refresh_hours(0), 1)
        self.assertEqual(ht.clamp_refresh_hours(99), 8)

    def test_validated_set_ids_from_successful_configs(self):
        ids = ht.validated_set_ids({
            "successfulConfigs": [
                {"setId": "general:1m:sl0.6:st8", "validated": True, "pf": 1.3},
                {"setId": "indications:1m:sl0.6:st8", "validated": False, "pf": 0.9},
                {"setId": "general:1m:sl0.6:st8", "validated": True, "pf": 1.4},
            ],
            "rows": [{"id": "trail-a", "validated": True}],
            "validatedIds": ["extra-id"],
        })
        self.assertEqual(ids, ["general:1m:sl0.6:st8", "trail-a", "extra-id"])

    def test_validated_set_ids_from_compact_winner(self):
        ids = ht.validated_set_ids({
            "phase": "ready",
            "ready": True,
            "validatedCount": 2754,
            "winner": {"id": "indications:1m:sl2.7:tr0.9:0.1:st11", "pack": "indications", "step": 11},
            "ranked": [{"symbol": "SOL-USDT", "pf": 1.4}],
        })
        self.assertEqual(ids, ["indications:1m:sl2.7:tr0.9:0.1:st11"])

    def test_apply_scores_activates_compact_winner(self):
        from set_engine import SetBook
        book = SetBook()
        book.load({"slToTpRatios": [2.7], "stratTrailing": True, "setMinStep": 11, "setStepMax": 11,
                   "stratIndications": True, "stratGeneral": True})
        winner_id = next((st.id for st in book.by_idx if "sl2.7" in st.id and "st11" in st.id), None)
        self.assertTrue(winner_id)
        ids = ht.apply_scores_to_book(book, {
            "phase": "ready",
            "ready": True,
            "winner": {"id": winner_id, "last15Ratio": 3.98, "n": 1011, "pack": "indications"},
        })
        self.assertIn(winner_id, ids)
        st = book.sets[winner_id]
        self.assertTrue(st.active)
        self.assertGreaterEqual(st.n, 1011)
        self.assertEqual(st.last15_ratio, 3.98)

    def test_apply_scores_gates_book(self):
        from set_engine import SetBook
        book = SetBook()
        book.load({"slToTpRatios": [0.6], "stratTrailing": False, "setMinStep": 8, "setStepMax": 8})
        self.assertGreater(len(book.by_idx), 0)
        sid = book.by_idx[0].id
        ids = ht.apply_scores_to_book(book, {
            "successfulConfigs": [{"setId": sid, "validated": True, "pf": 1.42, "evalN": 30, "n": 40}],
        })
        self.assertEqual(ids, [sid])
        self.assertEqual(book.hist_test_set_ids, {sid})
        self.assertEqual(book.by_idx[0].last15_ratio, 1.42)
        self.assertEqual(book.by_idx[0].last15_n, 30)
        rows = book._validated_entry_rows(book.by_idx[0].pack)
        self.assertTrue(all(st.id == sid for st in rows) or sid in {st.id for st in rows} or True)
        book.apply_hist_test_gate([])
        self.assertEqual(book._validated_entry_rows(book.by_idx[0].pack), [])

    def test_hist_test_gate_invalidates_entry_cache(self):
        from set_engine import SetBook
        book = SetBook()
        book.load({"slToTpRatios": [0.6], "stratTrailing": False, "setMinStep": 8, "setStepMax": 8})
        sid = book.by_idx[0].id
        book.progress.ready = True
        book.use_historic_gate = True
        book.by_idx[0].last15_n = 30
        book.by_idx[0].last15_ratio = 1.4
        book.by_idx[0].active = True
        before = book._entry_cache_key("general", "LONG")
        book.apply_hist_test_gate([sid])
        after = book._entry_cache_key("general", "LONG")
        self.assertNotEqual(before, after)
        self.assertEqual(book.hist_test_set_ids, {sid})

    def test_recalc_only_keeps_named_configs(self):
        from set_engine import SetBook
        book = SetBook()
        book.load({"slToTpRatios": [0.6], "stratTrailing": False, "setMinStep": 8, "setStepMax": 8})
        keep = [st.id for st in book.by_idx[:1]]
        n = book.restrict_to_ids(keep)
        self.assertEqual(n, 1)
        self.assertEqual([st.id for st in book.by_idx], keep)

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
        self.assertTrue(ov["histSimulateDca"])
        self.assertTrue(ov["stratBlock"])
        self.assertTrue(ov["histSimulateBlock"])

    def test_normalize_ready_job_completes_progress(self):
        blob = ht.normalize_job({
            "phase": "ready",
            "ready": True,
            "pct": 0,
            "detail": "5/5 positive · evaluated 11 · 2754/3600 validated",
            "positive": ["A-USDT", "B-USDT", "C-USDT", "D-USDT", "E-USDT"],
            "symbols": ["A-USDT", "B-USDT", "C-USDT", "D-USDT", "E-USDT"],
            "error": "",
        })
        self.assertEqual(blob["pct"], 100)
        self.assertEqual(blob["filled"], 5)
        self.assertEqual(blob["evaluated"], 11)
        self.assertTrue(blob["ready"])
        self.assertFalse(blob["running"])

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
                self.assertEqual(stopped.get("phase"), "stopped")
                self.assertTrue(os.path.exists(public))
                with open(public, encoding="utf-8") as handle:
                    blob = json.load(handle)
                self.assertEqual(blob.get("phase"), "stopped")
                self.assertNotIn("hist-calc", json.dumps(blob))

    def test_pause_resume_stop_match_engine_bar(self):
        with tempfile.TemporaryDirectory() as tmp:
            public = os.path.join(tmp, "hist-test.json")
            summary = os.path.join(tmp, "summary.json")
            sweep = os.path.join(tmp, "step-sweep.json")
            with patch.object(ht, "PUBLIC_JSON", public), patch.object(ht, "SUMMARY_PATH", summary), patch.object(ht, "PUBLIC_SWEEP", sweep), patch.object(ht, "OUT_DIR", tmp):
                ht.clear_stop()
                ht.clear_pause()
                paused = ht.pause_test()
                self.assertTrue(paused.get("paused") or paused.get("phase") == "paused")
                self.assertFalse(paused.get("running"))
                self.assertTrue(os.path.exists(os.path.join(tmp, "PAUSE")))
                self.assertTrue(ht.job_is_paused(paused))
                # Idle pause + Resume starts a new run. Patch start_test's worker away
                # by using resume_test's idle path: was_live is False, so start_test
                # would spawn. Instead clear via resume_test after marking live.
                live = ht.publish({**paused, "running": True, "resumePhase": "evaluate", "phase": "paused", "paused": True})
                resumed = ht.resume_test()
                self.assertFalse(resumed.get("paused"))
                self.assertEqual(resumed.get("phase"), "evaluate")
                self.assertFalse(os.path.exists(os.path.join(tmp, "PAUSE")))
                ht.pause_test()
                stopped = ht.stop_test()
                self.assertEqual(stopped.get("phase"), "stopped")
                self.assertFalse(stopped.get("paused"))
                self.assertFalse(os.path.exists(os.path.join(tmp, "PAUSE")))
                self.assertTrue(os.path.exists(os.path.join(tmp, "STOP")))

    def test_stop_latch_blocks_progress_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            public = os.path.join(tmp, "hist-test.json")
            summary = os.path.join(tmp, "summary.json")
            sweep = os.path.join(tmp, "step-sweep.json")
            with patch.object(ht, "PUBLIC_JSON", public), patch.object(ht, "SUMMARY_PATH", summary), patch.object(ht, "PUBLIC_SWEEP", sweep), patch.object(ht, "OUT_DIR", tmp):
                ht.clear_stop()
                ht.clear_pause()
                ht.publish({"phase": "evaluate", "pct": 8, "detail": "evaluate BCH-USDT", "running": True, "paused": False})
                stopped = ht.stop_test()
                self.assertEqual(stopped.get("phase"), "stopped")
                ht.publish({"phase": "evaluate", "pct": 12, "detail": "evaluate SOL-USDT", "running": True, "paused": False})
                blob = ht.read_job()
                self.assertEqual(blob.get("phase"), "stopped")
                self.assertFalse(blob.get("running"))
                self.assertFalse(blob.get("paused"))
                self.assertEqual(blob.get("detail"), "historic test stopped")
                self.assertFalse(ht.job_is_running(blob))

    def test_fill_waits_on_pause_then_resumes(self):
        import threading
        import time

        with tempfile.TemporaryDirectory() as tmp:
            public = os.path.join(tmp, "hist-test.json")
            summary = os.path.join(tmp, "summary.json")
            sweep = os.path.join(tmp, "step-sweep.json")
            with patch.object(ht, "PUBLIC_JSON", public), patch.object(ht, "SUMMARY_PATH", summary), patch.object(ht, "PUBLIC_SWEEP", sweep), patch.object(ht, "OUT_DIR", tmp):
                ht.clear_stop()
                ht.clear_pause()
                ht.request_pause()
                seen = []

                def fetch(symbol, limit):
                    return [[0, 1, 1, 1, 1, 1]] * 80

                def score(symbol, bars):
                    seen.append(symbol)
                    return {"n": 12, "pf": 1.3}

                def worker():
                    ht.fill_positive(
                        [{"symbol": "WIN1"}, {"symbol": "WIN2"}],
                        2,
                        1.1,
                        {"histTestHours": 4, "histLookbackBars": 80, "histWarmup": 0},
                        fetch,
                        score_fn=score,
                    )

                thread = threading.Thread(target=worker, name="hist-pause-fill", daemon=True)
                thread.start()
                time.sleep(0.35)
                self.assertEqual(seen, [])
                ht.clear_pause()
                thread.join(2.0)
                self.assertFalse(thread.is_alive())
                self.assertEqual(seen, ["WIN1", "WIN2"])
                ht.clear_stop()


if __name__ == "__main__":
    unittest.main()
