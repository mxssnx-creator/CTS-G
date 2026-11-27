"""Clocked configurations and the fixed-geometry study's execution rules."""
from __future__ import annotations

import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from indication_sweep import catalogue, compile_kernel, kernel, signals_for
import confirm_indication_sweep as confirm
import set_engine as se
from stage_grid import STAGES, StageGrid, StageHistory, WindowAuto


class IndicationTimingTests(unittest.TestCase):
    def test_each_configuration_keeps_its_clock_and_hold(self):
        case = dict(name="rsi_reclaim_n5_b25_t5", step=5, ratio=.75, hold=25, clock=5)
        settings = confirm.settings([case, dict(case, clock=3, hold=60)], .11)
        book = se.SetBook()
        book.load(settings)
        self.assertEqual(len(book.packs), 2)
        self.assertEqual(len(book.by_idx), 4)
        self.assertEqual(set(book.ind_settings["signalIntervals"].values()), {3, 5})
        self.assertEqual({st.hold_bars for st in book.by_idx}, {25, 60})
        self.assertEqual(len(set(st.id for st in book.by_idx)), 4)
        book.load(dict(settings, holdEnabled=False))
        self.assertTrue(all(st.hold_bars == 0 for st in book.by_idx))

    def test_replay_and_single_bar_calculate_only_at_due_close(self):
        case = dict(name="rsi_reclaim_n5_b25_t5", step=5, ratio=.75, hold=25, clock=5)
        book = se.SetBook()
        book.load(confirm.settings([case], .11))
        pack = book.packs[0]
        bars = [[100, 101, 99, 100, 1] for _ in range(5)]
        answer = (1, .72, case["name"])
        base = 1_800_000_060
        with patch.object(se, "thin_signals", return_value={case["name"]: answer}) as calculate:
            memo = {}
            sampled = se.pack_signals(bars, book.packs, book.ind_settings, base, 0, cache=memo)
            self.assertEqual(calculate.call_count, 1)
            self.assertEqual(len(memo), 1)
            self.assertEqual(se.pack_signals(bars, book.packs, book.ind_settings, base, 0, cache=memo), sampled)
            self.assertEqual(calculate.call_count, 1)
            self.assertEqual(sampled[pack], [(0, 0.0, "")] * 4 + [answer])
            for index in range(5):
                live = se.bar_signal(bars, index, book.packs, book.ind_settings, base + index * 60)
                self.assertEqual(live[pack], sampled[pack][index])
            self.assertEqual(calculate.call_count, 2)

    def test_one_minute_default_retains_an_existing_set_identity(self):
        case = dict(name="rsi_reclaim_n5_b25_t1", step=5, ratio=.75, hold=0, clock=1)
        settings = confirm.settings([case], .11)
        book = se.SetBook()
        book.load(settings)
        before = list(book.sets)
        settings = json.loads(json.dumps(settings))
        del settings["indicationConfigs"][0]["signalIntervalMinutes"]
        del settings["indicationConfigs"][0]["holdMinutes"]
        book.load(settings)
        self.assertEqual(list(book.sets), before)
        self.assertNotIn("signalIntervals", book.ind_settings)


class ResearchReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.calc = kernel(compile_kernel(Path(cls.directory.name)))

    def test_fixed_exits_match_engine_for_both_directions(self):
        generator = np.random.default_rng(189)
        steps = generator.normal(0, .20, 240)
        close = 100 + np.cumsum(steps)
        opening = close - steps + generator.normal(0, .15, len(steps))
        high = np.maximum(close, opening) + .15
        low = np.minimum(close, opening) - .15
        candles = np.column_stack((opening, high, low, close, np.ones(len(close))))
        bars = candles.tolist()
        case = dict(name="rsi_reclaim_n5_b25_t1", step=8, ratio=.75, hold=17, clock=1)
        book = se.SetBook()
        book.load(confirm.settings([case], .11))
        config = confirm.sg.lane_cfg(book)
        for st in book.by_idx:
            idx = np.empty(len(bars), np.int32)
            gross = np.empty(len(bars), np.float64)
            self.calc.exits(candles, len(bars), st.tp_pct, st.tp_pct * st.sl_ratio,
                           st.hold_bars, st.side, idx, gross)
            directions, confidence = [st.side] * len(bars), [1.] * len(bars)
            acc = se.BarIndex(bars)
            for entry in range(len(bars)):
                lane = se.SetLane(st, "TEST-USDT")
                closed = lane.scan(entry, len(bars), bars, directions, confidence, True,
                                   config, [entry], acc)
                direct = se.SetLane(st, "TEST-USDT").scan(entry, len(bars), bars, directions,
                                                        confidence, True, config, [entry])
                self.assertEqual(direct, closed)
                if not closed:
                    self.assertEqual(idx[entry], len(bars))
                else:
                    self.assertEqual(idx[entry], closed[0]["exit_i"])
                    self.assertAlmostEqual(gross[entry], closed[0]["pnl_pct"], places=14)

    def test_completed_frame_signals_are_prefix_invariant(self):
        candidates = ("rsi_reclaim_n5_b25_t5", "psar_classic_t5", "adx_onset_n7_a18_t5", "donch_n8_t5")
        probes = tuple(p for p in catalogue() if p.name in candidates)
        self.assertEqual(len(probes), len(candidates))
        close = 100. + np.sin(np.arange(750) / 9)
        bars = np.column_stack((close, close + .1, close - .1, close, np.ones(len(close))))
        whole = signals_for(bars, probes)
        np.testing.assert_array_equal(whole[:, :600], signals_for(bars[:600], probes))
        self.assertFalse(np.any(whole[:, np.arange(750) % 5 != 4]))

    def test_early_exec_search_matches_native_admission_with_causal_auto_pf(self):
        options = {"short": [4, 8], "last": [1, 2]}
        grid = StageGrid.from_settings({
            "base": {**options, "long": [10]},
            **{stage: {**options, "longModes": ["none"]} for stage in STAGES[1:]},
        })
        rng = random.Random(32)
        closes = tuple(60 * i for i in range(280))
        entries = tuple(t - 30 for t in closes)
        values = tuple(rng.choice((.007, .01, .03, -.01)) for _ in closes)
        policy = WindowAuto(1.12, 8, (1.05, 1.10, 1.20), .03, 0.)
        expected = []
        for at in (120, 800, 3900, 7800, 10920, 13800, 16800):
            reference = StageHistory(grid, closes, entries, values, 9000, lambda _: 1.12, window_auto=policy)
            actual = StageHistory(grid, closes, entries, values, 9000, lambda _: 1.12, window_auto=policy)
            native = reference.evaluate(at)
            fast = confirm.first_exec_path(actual, at)
            reused = confirm.first_exec_path(actual, at, whole_tape=True)
            expected.append(native.ready)
            self.assertEqual(fast.ready, native.ready)
            self.assertEqual(reused, fast)
            if fast.ready:
                self.assertTrue(actual.active_real(fast.path, at, 100))
        self.assertIn(True, expected)
        self.assertIn(False, expected)

    def test_realized_and_open_positions_keep_separate_pnl(self):
        market = {"A-USDT": np.array([[100, 101, 99, 100, 1], [100, 102, 100, 101, 1],
                                     [101, 103, 100, 102, 1]])}
        closed = [{"symbol": "A-USDT", "t_in": 60, "t": 120, "net": .0089,
                   "tr": {"entry_px": 100, "dir": 1}}]
        opened = [{"symbol": "A-USDT", "t_in": 60, "open_at_end": True,
                   "entry": 100, "mark_net": -.0211, "side": "SHORT"}]
        drawdown = confirm.drawdowns(closed, opened, market, 0, 0, 3, .0011)
        self.assertEqual(drawdown["realized_dd_pct"], 0.)
        self.assertAlmostEqual(drawdown["marked_dd_pct"], .0122, places=12)


if __name__ == "__main__":
    unittest.main()
