#!/usr/bin/env python3
"""Progression audit regressions: Test Historic seeding, allow-list, replay input.

Each test pins one behaviour the audit found broken (see the commit message):
own Base/Main/Real evidence beats the seed, the allow-list shrinks with a
full-catalog run and keeps every listed Set's evidence, "validated" follows the
run's floor and sample size, replay bars never span missing minutes, the refresh
timer ignores wall-clock steps, and a stale pid file cannot kill another process.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server", "pulse"))

import hist_calc as hc  # noqa: E402
import hist_test as ht  # noqa: E402
from history_store import HistoryStore  # noqa: E402
from set_engine import SetBook, SetState  # noqa: E402

COST = 0.1
STRICT = {
    "setStrictGate": True, "setUseHistoricGate": False, "entryPolicy": "permissive-bounded",
    "entryPolicyMaxCandidates": 0, "entryPolicyMinLiveSamples": 0,
    "minPf": 1.02, "baseMinPf": 1.02, "mainMinPf": 1.02, "realMinPf": 1.02, "setMinPf": 1.02,
    "baseEvalPosCount": 30, "setPfWindow": 30, "setDeactN": 25, "setLiveNegativeDeact": True,
    "positionCostPct": COST,
}


def make_set(i=0, step=3):
    return SetState(
        id=f"general:1m:sl{0.1 + (i % 30) * 0.1:.1f}:st{step + i // 30}", pack="general", tf="1m",
        sl_ratio=0.1 + (i % 30) * 0.1, trail_key="", trail_arm=0, trail_give=0, step=step + i // 30,
        tp_pct=0.0045, idx=i,
    )


def make_book(sets=1, overlay=None):
    book = SetBook()
    book.load(dict(overlay or STRICT), rebuild=False)
    book.progress.ready = True
    book.sets, book.by_idx = {}, []
    for i in range(sets):
        st = make_set(i)
        book.sets[st.id] = st
        book.by_idx.append(st)
    book._reindex()
    return book


def gross_for_pf(pf):
    return (1.0 + (pf - 1.0) / 0.1) * COST / 100.0


def live_close(book, st, i, pf, t0):
    book.on_live_close({
        "t": t0 + i * 300, "symbol": "X-USDT", "side": "LONG", "pnl": 1.0, "pnl_pct": gross_for_pf(pf),
        "hold_s": 60, "reason": "tp", "set_id": st.id, "strategy": "core", "client_id": f"c{i}",
        "close_fill_id": f"f{i}",
    })


class Isolated(unittest.TestCase):
    """Sidecar paths point at a scratch directory, never the real data dir."""

    def setUp(self):
        ht.clear_stop()
        ht.clear_pause()
        ht.invalidate_job_cache()
        self.tmp = tempfile.mkdtemp(prefix="progression-fixes-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for name, leaf in (("VALIDATED_IDS_PATH", "validated-ids.json"), ("LAST_READY_PATH", "last-ready.json")):
            guard = patch.object(ht, name, os.path.join(self.tmp, leaf))
            guard.start()
            self.addCleanup(guard.stop)


class SeedVersusOwnEvidence(Isolated):
    def test_a_direction_with_its_own_complete_window_keeps_its_own_stages(self):
        # Main window pinned to last-5: this contract is about a losing tail
        # of the direction's own Main window, not about the window length.
        book = make_book(overlay={**STRICT, "mainEvalPosCount": 5})
        st = book.by_idx[0]
        t0 = time.time() - 200000
        for i in range(25):
            live_close(book, st, i, 1.25, t0)
        for i in range(25, 30):
            live_close(book, st, i, 0.80, t0)
        self.assertTrue(st.stage_ledger["base"])
        self.assertFalse(st.stage_ledger["main"], "last-5 is losing")
        book.apply_hist_test_gate([st.id])
        job = {"successfulConfigs": [{"setId": st.id, "validated": True, "pf": 1.4, "evalN": 40, "n": 40}]}
        ht.apply_scores_to_book(book, job)
        # LONG has its own complete window (losing last-5): the seed must not forge Main/Real for it.
        long_side = st.by_side["LONG"]
        self.assertLess(float(long_side.get("main_pf") or 0), 1.02, "seed must not forge Main")
        self.assertLess(float(long_side.get("real_pf") or 0), 1.02, "seed must not forge Real")
        self.assertFalse(book.entry_sets("general", "LONG"))
        self.assertFalse(book.execution_allowed(st, "general", "LONG"))
        # SHORT has no closes of its own, so it is the direction the seed fills.
        self.assertTrue(book.execution_allowed(st, "general", "SHORT"))

    def test_a_set_without_own_evidence_is_still_seeded(self):
        book = make_book()
        st = book.by_idx[0]
        book.apply_hist_test_gate([st.id])
        job = {"successfulConfigs": [{"setId": st.id, "validated": True, "pf": 1.4, "evalN": 40, "n": 40}]}
        ht.apply_scores_to_book(book, job)
        self.assertTrue(st.stage_ledger["base"])
        self.assertGreater(float((st.by_side.get("LONG") or {}).get("base_pf") or 0), 1.0)


class AllowList(Isolated):
    def test_every_validated_id_keeps_its_evidence_beyond_the_job_row_cap(self):
        book = make_book(sets=600)
        ids = [st.id for st in book.by_idx]
        rows = [{"setId": sid, "validated": True, "pf": 1.4, "evalN": 30, "n": 40} for sid in ids[:80]]
        ht.persist_validated_ids(ids, evidence={sid: {"n": 40, "evalN": 30, "pf": 1.4, "maxDdS": 0} for sid in ids})
        ht.apply_scores_to_book(book, {"successfulConfigs": rows})
        self.assertEqual(len(book.hist_test_set_ids), 600)
        entering = {s.id for d in ("LONG", "SHORT") for s in book.entry_sets("general", d)}
        self.assertEqual(len(entering), 600)

    def test_a_full_run_replaces_the_list_and_a_default_write_never_shrinks_it(self):
        ht.persist_validated_ids(["general:1m:sl0.6:st7", "general:1m:sl0.6:st8"])
        ht.persist_validated_ids(["general:1m:sl0.6:st8"])
        self.assertEqual(set(ht.read_persisted_validated_ids()), {"general:1m:sl0.6:st7", "general:1m:sl0.6:st8"})
        ht.persist_validated_ids(["general:1m:sl0.6:st8"], replace=True)
        self.assertEqual(ht.read_persisted_validated_ids(), ["general:1m:sl0.6:st8"])

    def test_strategy_rows_never_count_as_set_ids(self):
        ids = ht.validated_set_ids({"validatedIds": ["block", "dca", "general:1m:sl0.6:st7"]}, use_persisted=False)
        self.assertEqual(ids, ["general:1m:sl0.6:st7"])


class ValidatedFollowsTheRunsFloor(unittest.TestCase):
    def test_one_close_at_pf_116_is_not_validated_at_floor_130_need_30(self):
        book = make_book(overlay={"baseEvalPosCount": 30, "minPf": 1.30, "baseMinPf": 1.30, "mainMinPf": 1.30,
                                  "realMinPf": 1.30, "setMinPf": 1.30})
        st = book.by_idx[0]
        st.last15_n, st.last15_ratio, st.n = 1, 1.16, 1
        rows = hc._rank_set_rows(book)
        self.assertEqual([r[3] for r in rows if not r[2]], [False])

    def test_a_full_window_above_the_floor_is_validated(self):
        book = make_book(overlay={"baseEvalPosCount": 30, "minPf": 1.30, "baseMinPf": 1.30, "mainMinPf": 1.30,
                                  "realMinPf": 1.30, "setMinPf": 1.30})
        st = book.by_idx[0]
        st.last15_n, st.last15_ratio, st.n = 30, 1.31, 30
        rows = hc._rank_set_rows(book)
        self.assertEqual([r[3] for r in rows if not r[2]], [True])


class ReplayBarsNeverSpanMissingMinutes(unittest.TestCase):
    def store(self, minutes):
        tmp = tempfile.mkdtemp(prefix="history-gap-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = HistoryStore("g", retention_bars=5000, path=os.path.join(tmp, "h.json"),
                             checkpoint_path=os.path.join(tmp, "c.json"))
        rows = [[m * 60000, 100 + m, 101 + m, 99 + m, 100.5 + m, 10.0] for m in minutes]
        store.merge("SOL-USDT", rows, source="exchange")
        return store

    def test_a_short_hole_is_filled_with_flat_bars_so_time_stays_aligned(self):
        base = 30_000_000
        minutes = [m for m in range(base, base + 60) if not base + 20 <= m < base + 23]
        store = self.store(minutes)
        bars = store.window("SOL-USDT", bars=60, end=base + 59, source="exchange")
        self.assertEqual(len(bars), 60)
        filled = bars[20:23]
        for bar in filled:
            self.assertEqual(bar[4], 0.0)
            self.assertEqual(bar[0], bar[3])
        self.assertEqual(filled[0][3], bars[19][3], "flat at the previous close")

    def test_a_long_hole_ends_the_run_and_only_the_newest_stretch_is_returned(self):
        base = 30_000_000
        minutes = [m for m in range(base, base + 200) if not base + 50 <= m < base + 140]
        store = self.store(minutes)
        bars = store.window("SOL-USDT", bars=200, end=base + 199, source="exchange")
        self.assertEqual(len(bars), 60, "the 60 minutes after the 90-minute hole")
        raw = store.window("SOL-USDT", bars=200, end=base + 199, source="exchange", max_fill=None)
        self.assertEqual(len(raw), 110)


class StalePidFile(unittest.TestCase):
    def test_stop_job_never_signals_an_unrelated_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            victim = subprocess.Popen(["sleep", "30"])
            self.addCleanup(lambda: (victim.kill(), victim.wait()))
            with patch.object(hc, "path_for", lambda name: os.path.join(tmp, name)):
                with open(hc._pid_path("bingx-x02"), "w") as handle:
                    handle.write(str(victim.pid))
                hc.stop_job("bingx-x02")
            time.sleep(0.3)
            self.assertIsNone(victim.poll(), "an unrelated process reusing the pid must survive")


class RefreshTimer(Isolated):
    def test_a_wall_clock_step_does_not_change_the_refresh_interval(self):
        real = time
        factor = 3600.0  # one real second is one clock hour
        t0 = real.time()
        offset = {"s": 0.0}
        shim = types.SimpleNamespace(
            time=lambda: t0 + (real.time() - t0) * factor + offset["s"], sleep=real.sleep,
            monotonic=lambda: real.monotonic() * factor, strftime=real.strftime, gmtime=real.gmtime,
            perf_counter=real.perf_counter,
        )
        ready = {"phase": "ready", "ready": True, "pct": 100}
        with patch.object(ht, "publish", lambda blob: dict(blob)), patch.object(ht, "read_job", lambda: dict(ready)), \
                patch.object(ht, "time", shim):
            done = {}
            start = real.monotonic()

            def run():
                ht.wait_for_refresh(1, dict(ready))
                done["hours"] = real.monotonic() - start

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            real.sleep(0.25)
            offset["s"] += 2 * 3600.0  # NTP jumps forward two hours
            worker.join(10)
        self.assertGreater(done.get("hours", 0), 0.9, "a +2h clock step must not end the wait early")


class ResumeAfterRestart(Isolated):
    def setUp(self):
        super().setUp()
        guard = patch.object(ht, "OUT_DIR", self.tmp)
        guard.start()
        self.addCleanup(guard.stop)

    def resume(self, job, body=None):
        if body is not None:
            with open(os.path.join(self.tmp, "start-body.json"), "w", encoding="utf-8") as handle:
                json.dump(body, handle)
        started = []
        with patch.object(ht, "thread_alive", lambda: False), patch.object(ht, "_runner_alive", lambda: False), \
                patch.object(ht, "read_job", lambda: dict(job)), \
                patch.object(ht, "start_test", lambda b: started.append(dict(b)) or {"ok": True}):
            out = ht.resume_after_restart()
        return out, started

    def test_a_continuous_run_the_desk_left_going_starts_again_with_its_own_settings(self):
        out, started = self.resume({"continuous": True, "phase": "ready", "hours": 20}, {"hours": 20, "minPf": 1.2, "continuous": True})
        self.assertEqual(out, {"ok": True})
        self.assertEqual(started[0]["minPf"], 1.2)

    def test_a_stopped_or_idle_job_is_left_alone(self):
        for phase in ("stopped", "idle", ""):
            with self.subTest(phase):
                out, started = self.resume({"continuous": True, "phase": phase}, {"hours": 20})
                self.assertIsNone(out)
                self.assertEqual(started, [])

    def test_a_one_off_run_is_not_restarted(self):
        out, started = self.resume({"continuous": False, "phase": "ready"}, {"hours": 20})
        self.assertIsNone(out)
        self.assertEqual(started, [])


if __name__ == "__main__":
    unittest.main()
