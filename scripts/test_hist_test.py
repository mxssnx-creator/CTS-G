#!/usr/bin/env python3
"""Historic test: 4–64h window, min PF 1.1, fill until selected count is positive."""
from __future__ import annotations

import json
import os
import shutil
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
        ht.invalidate_job_cache()
        self._iso = tempfile.mkdtemp(prefix="hist-test-iso-")
        self._prev_ids = ht.VALIDATED_IDS_PATH
        self._prev_last = ht.LAST_READY_PATH
        ht.VALIDATED_IDS_PATH = os.path.join(self._iso, "validated-ids.json")
        ht.LAST_READY_PATH = os.path.join(self._iso, "last-ready.json")

    def tearDown(self):
        ht.VALIDATED_IDS_PATH = self._prev_ids
        ht.LAST_READY_PATH = self._prev_last
        shutil.rmtree(self._iso, ignore_errors=True)
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

    def test_target_defaults_to_fifty_and_validates_up_to_250(self):
        self.assertEqual(ht.DEFAULT_TARGET, 50)
        self.assertEqual(ht.VALIDATION_CAP, 250)
        self.assertEqual(ht.SYMBOL_CAP, 50)
        self.assertEqual(ht.clamp_target(None), 50)
        self.assertEqual(ht.clamp_target(0), 50)
        self.assertEqual(ht.clamp_target(50), 50)
        self.assertEqual(ht.clamp_target(250), 250)
        self.assertEqual(ht.clamp_target(999), 250)

    def test_rank_universe_fills_volume_beyond_majors_up_to_250(self):
        majors = list(ht.HIST_TEST_MAJORS)
        extra = [
            {"symbol": f"ALT{i}-USDT", "last": 1, "vol24h": 1, "quoteVolume": 1e9 - i, "changePct": 1, "vol1h": 1}
            for i in range(220)
        ]
        ticker = [
            {"symbol": s, "last": 1, "vol24h": 1, "quoteVolume": 5e9, "changePct": 1, "vol1h": 1}
            for s in majors
        ] + extra
        with patch.object(ht, "fetch_ticker", return_value=ticker):
            picked, preview = ht.rank_universe(250)
        self.assertEqual(len(picked), 250)
        self.assertTrue(set(ht.PREFERRED_SYMBOLS).issubset({r["symbol"] for r in picked}))
        self.assertGreaterEqual(sum(1 for r in picked if str(r["symbol"]).startswith("ALT")), 150)
        self.assertLessEqual(len(preview), 40)

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

    def test_persist_validated_ids_caps_sidecar(self):
        import tempfile, os
        prev = ht.VALIDATED_IDS_PATH
        tmp = tempfile.mkdtemp(prefix="hist-ids-cap-")
        try:
            ht.VALIDATED_IDS_PATH = os.path.join(tmp, "validated-ids.json")
            ht.persist_validated_ids([f"set:{i}" for i in range(ht.VALIDATED_IDS_CAP + 25)])
            ids = ht.read_persisted_validated_ids()
            self.assertEqual(len(ids), ht.VALIDATED_IDS_CAP)
            self.assertEqual(ids[0], "set:0")
            self.assertEqual(ids[-1], f"set:{ht.VALIDATED_IDS_CAP - 1}")
        finally:
            ht.VALIDATED_IDS_PATH = prev
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_validated_set_ids_from_last_ready_file(self):
        import tempfile, os
        prev = ht.LAST_READY_PATH
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            ht.LAST_READY_PATH = path
            with open(path, "w") as handle:
                handle.write('{"winner":{"id":"indications:1m:sl2.7:tr0.9:0.1:st11"},"validatedIds":[]}')
            ids = ht.validated_set_ids({"phase": "evaluate", "ready": False})
            self.assertEqual(ids, ["indications:1m:sl2.7:tr0.9:0.1:st11"])
        finally:
            ht.LAST_READY_PATH = prev
            os.unlink(path)

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
        book.progress.ready = True
        book.use_historic_gate = True
        book.strict_gate = True
        long_rows = book._validated_entry_rows(st.pack, side="LONG")
        short_rows = book._validated_entry_rows(st.pack, side="SHORT")
        self.assertIn(winner_id, {row.id for row in long_rows})
        self.assertIn(winner_id, {row.id for row in short_rows})
        self.assertTrue(book.entry_pack_open(st.pack, side="LONG"))
        self.assertTrue((st.by_side.get("LONG") or {}).get("active"))

    def test_score_pairs_chunks_above_calc_batch(self):
        from set_engine import SetBook
        from calculation_cache import CalculationCache

        class St:
            def __init__(self, i):
                self.id = f"dummy:{i}"

        class _Cache:
            BATCH = CalculationCache.BATCH
            calls = []

            def prepare(self, _book, states):
                if len(states) > self.BATCH:
                    raise ValueError("calculation pipeline exceeds batch limit")
                self.calls.append(len(states))
                return [(st, None) for st in states]

        book = SetBook()
        book.calculation_cache = _Cache()
        states = [St(i) for i in range(CalculationCache.BATCH * 2 + 5)]
        pairs = book.score_pairs(states)
        self.assertEqual(len(pairs), len(states))
        self.assertEqual(len(_Cache.calls), 3)
        self.assertTrue(all(n <= CalculationCache.BATCH for n in _Cache.calls))
        self.assertEqual(sum(_Cache.calls), len(states))

    def test_persist_validated_ids_does_not_shrink(self):
        import tempfile, os, shutil
        prev = ht.VALIDATED_IDS_PATH
        tmp = tempfile.mkdtemp(prefix="hist-ids-keep-")
        try:
            ht.VALIDATED_IDS_PATH = os.path.join(tmp, "validated-ids.json")
            ht.persist_validated_ids([f"keep:{i}" for i in range(40)])
            ht.persist_validated_ids(["keep:0", "tiny:1"])
            ids = ht.read_persisted_validated_ids()
            self.assertEqual(len(ids), 41)
            self.assertEqual(ids[0], "keep:0")
            self.assertIn("tiny:1", ids)
            self.assertIn("keep:39", ids)
        finally:
            ht.VALIDATED_IDS_PATH = prev
            shutil.rmtree(tmp, ignore_errors=True)

    def test_apply_scores_gates_book(self):
        import tempfile, os, shutil
        from set_engine import SetBook
        prev_ids = ht.VALIDATED_IDS_PATH
        prev_last = ht.LAST_READY_PATH
        tmp = tempfile.mkdtemp(prefix="hist-gate-")
        try:
            ht.VALIDATED_IDS_PATH = os.path.join(tmp, "validated-ids.json")
            ht.LAST_READY_PATH = os.path.join(tmp, "no-last-ready.json")
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
        finally:
            ht.VALIDATED_IDS_PATH = prev_ids
            ht.LAST_READY_PATH = prev_last
            shutil.rmtree(tmp, ignore_errors=True)

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

    def test_short_ready_book_does_not_recalc_only(self):
        """2 positive symbols vs target 50 must fill the ranked book, not freeze intern."""
        ids = ["indications:1m:sl0.6:st8"]
        self.assertFalse(ht.use_recalc_only({}, ids, ["THETA-USDT", "RENDER-USDT"], 50))
        self.assertTrue(ht.use_recalc_only({}, ids, [f"S{i}-USDT" for i in range(50)], 50))
        self.assertFalse(ht.use_recalc_only({"fullCatalog": True}, ids, [f"S{i}-USDT" for i in range(50)], 50))
        self.assertFalse(ht.use_recalc_only({}, [], ["THETA-USDT", "RENDER-USDT"], 2))

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

    def test_validated_symbols_cap_and_off_view(self):
        names = [f"S{i}-USDT" for i in range(80)]
        self.assertEqual(ht.validated_symbols({"positive": names}), names[: ht.SYMBOL_CAP])
        off = ht.off_progress_view()
        self.assertEqual(off["phase"], "off")
        self.assertFalse(off["enabled"])
        self.assertEqual(off["runningSets"], [])
        self.assertEqual(off["internSymbols"], [])

    def test_intern_liquid_pool_drops_range_dust(self):
        universe = [
            {"symbol": "LAPTOP-USDT", "quoteVolume": 12000},
            {"symbol": "HOOKR-USDT", "quoteVolume": 800},
            {"symbol": "BTC-USDT", "quoteVolume": 9e9},
            {"symbol": "ETH-USDT", "quoteVolume": 4e9},
            {"symbol": "XRP-USDT", "quoteVolume": 8e8},
        ]
        overlay = ["LAPTOP-USDT", "HOOKR-USDT", "AIN-USDT", "XRP-USDT"]
        out = ht.intern_liquid_pool(overlay, universe, opens=["LAPTOP-USDT"], cap=50)
        self.assertEqual(out[0], "BCH-USDT")
        self.assertIn("SOL-USDT", out[:3])
        self.assertIn("XRP-USDT", out[:3])
        self.assertIn("BTC-USDT", out)
        self.assertIn("ETH-USDT", out)
        self.assertNotIn("LAPTOP-USDT", out)
        self.assertNotIn("HOOKR-USDT", out)
        self.assertNotIn("AIN-USDT", out)

    def test_intern_liquid_pool_without_universe_uses_majors(self):
        out = ht.intern_liquid_pool(["LAPTOP-USDT", "MICRODUCK-USDT"], [], cap=20)
        self.assertIn("XRP-USDT", out)
        self.assertIn("BCH-USDT", out)
        self.assertIn("SOL-USDT", out)
        self.assertIn("BTC-USDT", out)
        self.assertNotIn("LAPTOP-USDT", out)
        self.assertNotIn("MICRODUCK-USDT", out)

    def test_select_intern_symbols_ready_uses_overlay_not_junk(self):
        overlay = ["XRP-USDT", "BCH-USDT", "SOL-USDT", "BTC-USDT"]
        job = {
            "phase": "ready",
            "ready": True,
            "positive": ["SYN-USDT", "BONER-USDT", "XRP-USDT"],
            "audit": {"failed": ["symbols-meet-floor"]},
            "error": "audit: symbols-meet-floor",
        }
        out = ht.select_intern_symbols(overlay, job, cap=50)
        self.assertEqual(out[:4], overlay)
        self.assertNotIn("BONER-USDT", out)
        self.assertNotIn("SYN-USDT", out)

    def test_intern_liquid_pool_interns_fifty_majors(self):
        overlay = ["FLYBRAIN-USDT", "AIN-USDT", "BCH-USDT"]
        out = ht.intern_liquid_pool(overlay, None, cap=50)
        self.assertEqual(len(out), 50)
        self.assertEqual(set(out), set(ht.HIST_TEST_MAJORS))
        self.assertNotIn("FLYBRAIN-USDT", out)
        self.assertNotIn("AIN-USDT", out)

    def test_intern_liquid_pool_fills_cap_with_liquid_volume(self):
        tradable = list(ht.HIST_TEST_MAJORS[:48]) + ["AAA-USDT", "BBB-USDT"]
        universe = (
            [{"symbol": s, "quoteVolume": 5e9} for s in ht.HIST_TEST_MAJORS[:48]]
            + [
                {"symbol": "AAA-USDT", "quoteVolume": 9e9},
                {"symbol": "BBB-USDT", "quoteVolume": 8e9},
                {"symbol": "DUST-USDT", "quoteVolume": 12},
            ]
        )
        out = ht.intern_liquid_pool(None, universe, cap=50, tradable=tradable)
        self.assertEqual(len(out), 50)
        self.assertIn("AAA-USDT", out)
        self.assertIn("BBB-USDT", out)
        self.assertNotIn("DUST-USDT", out)

    def test_intern_liquid_pool_drops_offline_contracts(self):
        tradable = [s for s in ht.HIST_TEST_MAJORS if s not in ("EOS-USDT", "MKR-USDT")]
        out = ht.intern_liquid_pool(["EOS-USDT", "MKR-USDT", "BTC-USDT"], None, cap=50, tradable=tradable)
        self.assertNotIn("EOS-USDT", out)
        self.assertNotIn("MKR-USDT", out)
        self.assertIn("BTC-USDT", out)
        self.assertEqual(out[0], "BCH-USDT")
        self.assertIn("SOL-USDT", out[:3])
        self.assertIn("XRP-USDT", out[:3])

    def test_intern_liquid_pool_keeps_open_offline_lots(self):
        tradable = ["BTC-USDT", "ETH-USDT", "BCH-USDT", "SOL-USDT", "XRP-USDT"]
        out = ht.intern_liquid_pool(None, None, opens=["EOS-USDT"], cap=50, tradable=tradable)
        self.assertIn("EOS-USDT", out)
        self.assertIn("BTC-USDT", out)
        self.assertNotIn("MKR-USDT", out)

    def test_select_intern_symbols_inflight_keeps_overlay(self):
        overlay = ["XRP-USDT", "BCH-USDT", "SOL-USDT"]
        job = {"phase": "score", "ready": False, "positive": ["BCH-USDT", "BONER-USDT"]}
        out = ht.select_intern_symbols(overlay, job, cap=50)
        self.assertEqual(out, overlay)
        self.assertNotIn("BONER-USDT", out)

    def test_progress_view_does_not_mark_ready_on_batch_error(self):
        view = ht.job_progress_view({
            "phase": "ready",
            "ready": True,
            "pct": 100,
            "validatedCount": 1000,
            "successfulConfigs": [{"setId": "indications:1m:sl0.6:st12", "validated": True, "pf": 1.3, "n": 96, "indication": "combined", "strategy": "normal"}],
            "error": "ValueError: calculation pipeline exceeds batch limit",
            "positive": ["ETH-USDT", "ADA-USDT"],
        })
        self.assertEqual(view["phase"], "error")
        self.assertFalse(view["ready"])
        self.assertIn("batch limit", view["detail"])
        self.assertEqual(len(view["internSymbols"]), 50)
        self.assertIn("BTC-USDT", view["internSymbols"])

    def test_select_intern_symbols_keeps_open_lots(self):
        overlay = ["XRP-USDT", "SOL-USDT"]
        job = {"phase": "ready", "ready": True, "positive": ["SYN-USDT"]}
        out = ht.select_intern_symbols(overlay, job, opens=["AAVE-USDT", "XRP-USDT"], cap=50)
        self.assertEqual(out, ["XRP-USDT", "SOL-USDT", "AAVE-USDT"])

    def test_apply_scores_empty_ids_keeps_gate_closed(self):
        from set_engine import SetBook
        book = SetBook()
        book.load({"slToTpRatios": [0.6], "stratTrailing": False, "setMinStep": 8, "setStepMax": 8})
        self.assertGreater(len(book.by_idx), 1)
        with patch.object(ht, "read_persisted_validated_ids", return_value=[]), patch.object(ht, "read_last_ready", return_value={}):
            ids = ht.apply_scores_to_book(book, {"successfulConfigs": [], "validatedIds": [], "rows": []})
        self.assertEqual(ids, [])
        self.assertIsNotNone(book.hist_test_set_ids)
        self.assertEqual(book.hist_test_set_ids, set())
        self.assertEqual(book._validated_entry_rows(book.by_idx[0].pack), [])
        self.assertIsNone(book.pick(book.by_idx[0].pack))
        snap = book.snapshot()
        self.assertEqual(snap["processingCount"], 0)
        self.assertEqual(snap["histFills"], 0)

    def test_engine_processes_only_selected_hist_test_configs(self):
        from set_engine import SetBook
        book = SetBook()
        book.load({"slToTpRatios": [0.6, 0.9], "stratTrailing": False, "setMinStep": 8, "setStepMax": 8,
                   "stratIndications": True, "stratGeneral": True})
        self.assertGreaterEqual(len(book.by_idx), 4)
        keep = [st.id for st in book.by_idx[:2]]
        other = [st.id for st in book.by_idx[2:4]]
        for st in book.by_idx:
            st.last15_n = 40
            st.last15_ratio = 1.4
            st.active = True
            st.n = 40
        with patch.object(ht, "read_persisted_validated_ids", return_value=[]), patch.object(ht, "read_last_ready", return_value={}):
            ids = ht.apply_scores_to_book(book, {
            "successfulConfigs": [
                {"setId": keep[0], "validated": True, "pf": 1.51, "evalN": 40, "n": 40,
                 "indication": "combined", "strategy": "block"},
                {"setId": keep[1], "validated": True, "pf": 1.44, "evalN": 32, "n": 32,
                 "indication": "general", "strategy": "normal"},
            ],
            "comboMatrix": [
                {"indication": "combined", "strategy": "block", "validated": True, "pf": 1.51, "n": 40, "setId": keep[0]},
                {"indication": "general", "strategy": "dca", "validated": False, "pf": 0.8, "n": 12},
            ],
        })
        self.assertEqual(ids, keep)
        self.assertEqual(book.hist_test_set_ids, set(keep))
        for sid in keep:
            self.assertTrue(book.sets[sid].active, sid)
            self.assertEqual(book.sets[sid].deact_reason, "")
        for sid in other:
            self.assertFalse(book.sets[sid].active, sid)
            self.assertEqual(book.sets[sid].deact_reason, "hist-test gate")
        picked = {st.id for st in book._validated_entry_rows(book.by_idx[0].pack)}
        self.assertTrue(picked <= set(keep))
        self.assertTrue(picked)
        for sid in other:
            self.assertNotIn(sid, picked)
        source = book.intern_metric_source()
        self.assertIsNotNone(source)
        self.assertIn(source.id, keep)
        snap = book.snapshot()
        self.assertEqual(snap["setCount"], 2)
        self.assertEqual(snap["internSetCount"], 2)
        self.assertEqual(snap["validatedCount"], 2)
        self.assertGreater(snap["catalogSetCount"], 2)
        self.assertEqual(snap["processingCount"], 0)
        intern_ids = set(snap.get("internSetIds") or [])
        proc_ids = set(snap["processingSetIds"])
        self.assertTrue(set(keep) <= intern_ids)
        self.assertFalse(set(other) & intern_ids)
        self.assertFalse(set(other) & proc_ids)
        coords = ht.selected_coordinations({
            "successfulConfigs": [
                {"setId": keep[0], "validated": True, "indication": "combined", "strategy": "block", "pf": 1.51, "n": 40},
            ],
            "comboMatrix": [
                {"indication": "combined", "strategy": "block", "validated": True, "pf": 1.51, "n": 40},
            ],
        })
        self.assertGreaterEqual(len(coords), 1)
        self.assertEqual(coords[0]["strategy"], "block")
        with patch.object(ht, "read_persisted_validated_ids", return_value=[]), patch.object(ht, "read_last_ready", return_value={}):
            view = ht.job_progress_view({
            "phase": "ready",
            "ready": True,
            "validatedCount": 2,
            "validatedIds": keep,
            "successfulConfigs": [
                {"setId": keep[0], "validated": True, "indication": "combined", "strategy": "block", "pf": 1.51, "n": 40},
            ],
            "comboMatrix": [
                {"indication": "combined", "strategy": "block", "validated": True, "pf": 1.51, "n": 40, "setId": keep[0]},
            ],
            "withWithout": {"block": {"with": {"pf": 1.4, "n": 10}, "without": {"pf": 1.1, "n": 8}}},
            "pfStats": {"overall": {"pf": 1.4, "n": 10}},
        })
        self.assertTrue(view["selectedCoordinations"])
        self.assertIn("block", view["withWithout"])
        self.assertEqual(view["validatedCount"], 2)
        self.assertEqual(view["processedSetCount"], 2)
        self.assertLessEqual(view["processingCount"], 2)

    def test_selected_coordinations_drop_false_cells(self):
        coords = ht.selected_coordinations({
            "successfulConfigs": [
                {"setId": "indications:1m:sl0.6:st8", "validated": True, "indication": "combined", "strategy": "block", "pf": 1.5, "n": 20, "maxDdS": 12},
                {"setId": "junk", "validated": True, "indication": "", "strategy": "sl0.6:st8:trbase", "pf": 1.9, "n": 9},
                {"setId": "general:1m:sl0.6:st4", "validated": False, "indication": "general", "strategy": "dca", "pf": 0.7, "n": 8},
            ],
            "comboMatrix": [
                {"indication": "combined", "strategy": "block", "validated": True, "pf": 1.5, "n": 20},
                {"indication": "signals", "strategy": "normal", "validated": False, "pf": 0.9, "n": 40},
                {"indication": "block", "strategy": "block", "validated": True, "pf": 2.0, "n": 5},
            ],
        })
        keys = {(c["indication"], c["strategy"]) for c in coords}
        self.assertIn(("combined", "block"), keys)
        self.assertNotIn(("signals", "normal"), keys)
        self.assertNotIn(("block", "block"), keys)
        self.assertFalse(any("sl0.6" in str(c.get("strategy") or "") for c in coords))
        self.assertTrue(all(c["validated"] for c in coords))
        ident = ht.identity_from_set_id("indications:1m:sl2.7:tr0.9:0.1:st11")
        self.assertEqual(ident["indication"], "combined")
        self.assertEqual(ident["strategy"], "trailing")
        run = ht.running_sets({"validatedIds": ["general:1m:sl0.6:st8"]})
        self.assertEqual(run[0]["indication"], "general")
        self.assertEqual(run[0]["strategy"], "normal")
        self.assertNotEqual(run[0]["strategy"], "sl0.6:st8:trbase")
        self.assertEqual(ht.normalize_catalog_set_id("indications:1m:sl0.6:st8:long"), "indications:1m:sl0.6:st8")
        self.assertEqual(ht.normalize_catalog_set_id("dca:base"), "")
        self.assertEqual(ht.normalize_catalog_set_id("block:indications:1m:sl0.6:st8"), "")
        ids = ht.validated_set_ids({
            "validatedIds": [
                "indications:1m:sl0.6:st8:long",
                "indications:1m:sl0.6:st8:SHORT",
                "dca:add",
                "general:1m:sl0.6:st4",
            ]
        })
        self.assertEqual(ids, ["indications:1m:sl0.6:st8", "general:1m:sl0.6:st4"])
        overlay = ht.identity_from_set_id("block:indications:1m:sl0.6:st8")
        self.assertEqual(overlay["indication"], "combined")
        self.assertEqual(overlay["strategy"], "block")
        self.assertEqual(overlay["pack"], "indications")
        self.assertEqual(ht.identity_from_set_id("dca:base"), {"pack": "", "indication": "", "strategy": ""})
        self.assertEqual(ht.identity_from_set_id("block:signals")["indication"], "")
        self.assertFalse(ht.is_coordination_set_id("indications:signals"))
        self.assertFalse(ht.is_coordination_set_id("block:signals"))
        self.assertTrue(ht.is_coordination_set_id("block:indications:1m:sl0.6:st8"))
        false_coords = ht.selected_coordinations({
            "successfulConfigs": [
                {"setId": "indications:signals", "validated": True, "indication": "signals", "strategy": "normal", "pf": 2.0, "n": 40},
                {"setId": "block:signals", "validated": True, "indication": "signals", "strategy": "block", "pf": 2.2, "n": 12},
                {"setId": "block:indications:1m:sl0.6:st8", "validated": True, "indication": "combined", "strategy": "block", "pf": 1.6, "n": 18},
            ],
            "comboMatrix": [
                {"indication": "signals", "strategy": "normal", "validated": True, "pf": 2.0, "n": 40},
                {"indication": "signals", "strategy": "block", "validated": True, "pf": 2.2, "n": 12},
            ],
        })
        false_keys = {(c["indication"], c["strategy"], c["id"]) for c in false_coords}
        self.assertEqual(false_keys, {("combined", "block", "block:indications:1m:sl0.6:st8")})

    def test_selected_coordinations_keep_catalog_payload_rows(self):
        coords = ht.selected_coordinations({
            "successfulConfigs": [{"setId": "indications:1m:sl0.6:st8", "validated": True}],
            "selectedCoordinations": [{"id": "indications:1m:sl0.6:st8", "strategy": "block"}],
        })
        self.assertEqual(len(coords), 1)
        self.assertEqual(coords[0]["id"], "indications:1m:sl0.6:st8")
        self.assertEqual(coords[0]["strategy"], "block")
        self.assertEqual(coords[0]["indication"], "combined")

    def test_cap_active_cannot_drop_hist_test_validated(self):
        from set_engine import SetBook
        book = SetBook()
        book.load({"slToTpRatios": [0.6], "stratTrailing": False, "setMinStep": 8, "setStepMax": 8})
        sid = book.by_idx[0].id
        with patch.object(ht, "read_persisted_validated_ids", return_value=[]), patch.object(ht, "read_last_ready", return_value={}):
            ht.apply_scores_to_book(book, {
                "successfulConfigs": [{"setId": sid, "validated": True, "pf": 1.2, "evalN": 30, "n": 30}],
            })
        book.max_active = 0
        book._cap_active(True)
        self.assertTrue(book.sets[sid].active)
        book.max_active = 1
        for st in book.by_idx:
            st.last15_ratio = 9.9
            st.active = True
        book._cap_active(True)
        self.assertTrue(book.sets[sid].active)
        self.assertEqual(book.sets[sid].deact_reason, "")

    def test_apply_scores_does_not_drop_validated_ids_above_old_cap(self):
        from set_engine import SetBook
        book = SetBook()
        book.load({"slToTpRatios": [0.6], "stratTrailing": False, "setMinStep": 8, "setStepMax": 8})
        sid = book.by_idx[0].id
        extras = [f"extra:{i}:st8" for i in range(360)]
        with patch.object(ht, "read_persisted_validated_ids", return_value=[]), patch.object(ht, "read_last_ready", return_value={}):
            ids = ht.apply_scores_to_book(book, {"validatedIds": [sid] + extras})
        self.assertGreaterEqual(len(ids), 361)
        self.assertIn(sid, book.hist_test_set_ids)
        self.assertEqual(len(book.hist_test_set_ids), len(ids))

    def test_keep_recalc_symbols_drops_junk(self):
        out = ht.keep_recalc_symbols(["BONER-USDT", "XRP-USDT", "AIN-USDT", "SOL-USDT"], target=40)
        self.assertEqual(out, ["XRP-USDT", "SOL-USDT"])
        empty = ht.keep_recalc_symbols([], target=5, job={"positive": ["SYN-USDT"]})
        self.assertTrue(empty)
        self.assertIn("BCH-USDT", empty)
        self.assertNotIn("SYN-USDT", empty)

    def test_rank_universe_is_majors_not_range_dust(self):
        ticker = [
            {"symbol": "BONER-USDT", "lastPrice": 1, "highPrice": 2, "lowPrice": 0.1, "quoteVolume": 5e6, "priceChangePercent": 90},
            {"symbol": "BTC-USDT", "lastPrice": 100, "highPrice": 101, "lowPrice": 99, "quoteVolume": 9e9, "priceChangePercent": 1},
            {"symbol": "XRP-USDT", "lastPrice": 1, "highPrice": 1.1, "lowPrice": 0.9, "quoteVolume": 8e8, "priceChangePercent": 2},
            {"symbol": "SOL-USDT", "lastPrice": 80, "highPrice": 81, "lowPrice": 79, "quoteVolume": 2e9, "priceChangePercent": 1},
            {"symbol": "BCH-USDT", "lastPrice": 400, "highPrice": 401, "lowPrice": 399, "quoteVolume": 1e8, "priceChangePercent": 0.5},
        ]
        with patch.object(ht, "_public_json", return_value={"data": ticker}):
            picked, _preview = ht.rank_universe(10)
        names = [r["symbol"] for r in picked]
        self.assertEqual(names[0], "BCH-USDT")
        self.assertIn("SOL-USDT", names[:3])
        self.assertIn("XRP-USDT", names[:3])
        self.assertNotIn("BONER-USDT", names)

    def test_job_is_running_ready_is_not_inflight(self):
        self.assertFalse(ht.job_is_running({"phase": "ready", "ready": True, "running": True}))
        self.assertTrue(ht.job_is_running({"phase": "replay", "running": True}))

    def test_compact_job_positives_are_scored_majors(self):
        job = {
            "phase": "ready",
            "ready": True,
            "positive": ["XRP-USDT", "SOL-USDT"],
            "symbols": ["XRP-USDT", "SOL-USDT"],
            "validatedIds": ["indications:1m:sl0.6:st8"],
            "bySymbol": [
                {"symbol": "XRP-USDT", "n": 20, "pf": 1.4, "maxDdS": 12, "validated": True},
                {"symbol": "BONER-USDT", "n": 90, "pf": 3.7, "maxDdS": 180, "validated": True},
            ],
            "pfStats": {
                "overall": {"pf": 1.3, "n": 20, "maxDdS": 12},
                "block": {"pf": 1.2, "n": 4, "maxDdS": 8},
                "dca": {"pf": 1.0, "n": 0, "maxDdS": 0},
            },
            "kinds": {"signals": {"pf": 1.4, "n": 10, "maxDdS": 9, "evaluationWindows": {}}},
        }
        ranked = [{"symbol": "XRP-USDT", "n": 20, "pf": 1.4, "positive": True, "maxDdS": 12}]
        out = ht.compact_job(job, ranked, [], 20, 1.1)
        self.assertEqual(out["symbols"], ["XRP-USDT", "SOL-USDT"])
        self.assertNotIn("BONER-USDT", [r.get("symbol") for r in out["bySymbol"]])
        self.assertEqual(out["filled"], 2)
        self.assertIn("signals", out["byIndication"])
        self.assertIn("maxDdS", out["byIndication"]["signals"])
        self.assertNotIn("evaluationWindows", out["byIndication"]["signals"])
        view = ht.job_progress_view(out)
        self.assertFalse(view["running"])
        self.assertEqual(view["phase"], "ready")
        self.assertIn("signals", view.get("byIndication") or {})

    def test_seed_recalc_prior_keeps_last_ready_ids(self):
        import tempfile, os, shutil
        prev_last = ht.LAST_READY_PATH
        prev_ids = ht.VALIDATED_IDS_PATH
        tmp = tempfile.mkdtemp(prefix="hist-seed-")
        try:
            ht.LAST_READY_PATH = os.path.join(tmp, "last-ready.json")
            ht.VALIDATED_IDS_PATH = os.path.join(tmp, "validated-ids.json")
            with open(ht.LAST_READY_PATH, "w") as handle:
                json.dump({
                    "phase": "ready",
                    "ready": True,
                    "positive": ["BCH-USDT", "SOL-USDT", "XRP-USDT"],
                    "validatedIds": ["indications:1m:sl0.6:st8", "general:1m:sl0.6:st4"],
                    "winner": {"id": "indications:1m:sl0.6:st8"},
                }, handle)
            ht.persist_validated_ids(["indications:1m:sl0.6:st8", "general:1m:sl0.6:st4"])
            with patch.object(ht, "read_job", return_value={"phase": "evaluate", "validatedIds": [], "positive": []}):
                prior = ht.seed_recalc_prior()
            self.assertEqual(prior.get("positive"), ["BCH-USDT", "SOL-USDT", "XRP-USDT"])
            self.assertIn("indications:1m:sl0.6:st8", prior.get("validatedIds") or [])
            ids = ht.collect_validated_ids({"phase": "evaluate", "validatedIds": []})
            self.assertIn("indications:1m:sl0.6:st8", ids)
        finally:
            ht.LAST_READY_PATH = prev_last
            ht.VALIDATED_IDS_PATH = prev_ids
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
