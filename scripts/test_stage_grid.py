#!/usr/bin/env python3
"""Independent, causal oracle for disjoint windows and Exec attribution."""
import json
import math
from pathlib import Path
import random
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server" / "pulse"))
import pulse_trader as pt
import set_engine as se
import stage_grid as sg
import auto_set_pf
from indication_extra import CTS_C_TECHNICAL, THIN_INDICATOR_SETTINGS, resolve, sar_state, sar_signal
from stage_eval import AutoFloors


def plain(grid, rows, now, lookback, floor):
    counts = [[0, 0, 0, 0.0, 0.0, 0] for _ in sg.STAGES]
    choices = {}

    def evidence(tape, decision, w):
        seen = [rows[i][2] for i in tape if decision - lookback <= rows[i][0] < decision]
        if len(seen) < w.need:
            return False, None
        long = seen[-w.need:-w.short - w.last] if w.long else []
        short = seen[-w.short - w.last:-w.last]
        last = seen[-w.last:]

        def positive(block):
            gain = math.fsum(x for x in block if x > 0)
            loss = math.fsum(-x for x in block if x < 0)
            return gain > 0 and gain >= floor * loss

        if long and not positive(long):
            return True, None
        if not (positive(short) if w.short_mode == "positive" else math.fsum(short) < -1e-12):
            return True, None
        net = math.fsum(last)
        if not (net > 1e-12 if w.last_mode == "positive" else net < -1e-12):
            return True, None
        values = long + short + last
        return True, (len(values), math.fsum(x for x in values if x > 0), math.fsum(-x for x in values if x < 0))

    def walk(tape, path):
        depth = len(path)
        for w in grid.windows(depth, path[0].long if path else 0):
            n, full, passes, gain, loss, orders = counts[depth]
            counts[depth][0] += 1
            complete, ev = evidence(tape, now, w)
            counts[depth][1] += complete
            if ev is None:
                continue
            counts[depth][2] += 1
            counts[depth][3] += ev[1]
            counts[depth][4] += ev[2]
            counts[depth][5] += ev[0]
            next_path = (*path, w)
            history = tuple(i for i in tape if evidence(tape, rows[i][1], w)[1] is not None)
            choices[next_path] = tuple(rows[i] for i in history if now - lookback <= rows[i][0] < now)
            if len(next_path) < len(sg.STAGES):
                walk(history, next_path)
    walk(tuple(range(len(rows))), ())
    return counts, choices


class StageGridTest(unittest.TestCase):
    def test_auto_pf_independence_and_strict_boundaries(self):
        early = 72 * 3600
        rows = [(early + i * 60, early + i * 60 - 30, net)
                for i, net in enumerate([.01, .02, -.034, .01, .02, -.026, .05] * 15)]
        close, entry, net = zip(*rows)
        past = auto_set_pf.observations(close, entry, net, 3, early, early + 80 * 60)
        later_changed = [v if c < early + 80 * 60 else 500 for c, v in zip(close, net)]
        self.assertEqual(past, auto_set_pf.observations(close, entry, later_changed, 3, early, early + 80 * 60))
        repeated = [(1.08, -.02), (1.4, .03), (1.4, .025), (1.4, -.006)] * 8
        self.assertEqual(auto_set_pf.fit(repeated, 10, 1.04, (1.1, 1.2, 1.4), .05, .5), 1.1)
        self.assertIsNone(auto_set_pf.fit(repeated[:7], 10, 1.04, (1.1, 1.2, 1.4), .05, .5))
        self.assertIsNone(auto_set_pf.fit(repeated[:16] + [(1.4, -.03)] * 16, 10, 1.04, (1.1, 1.2, 1.4), .05, .5))

        book = se.SetBook()
        book.load({"stageLastN": {}, "minPfByStep": True, "minPfStepAuto": True, "minPfStepAutoScope": "step",
                   "setMinStep": 4, "setStepMax": 4, "stratIndications": False, "setSlRatios": [1.0],
                   "autoPfDefaultByRange": {"minimal": 1.24}})
        base = next(st for st in book.by_idx if st.kind == "base")
        empty = next(st for st in book.by_idx if st.id != base.id)
        self.assertTrue(book.auto_pf_per_set(base))
        self.assertEqual(book.floor_of(base), (1.24, "auto-range-default"))
        cost = se.cost_as_frac(book.cost_pct)
        gross = [x + cost for x in net]
        by_set = {st.idx: (close, entry, gross) if st == base else ((), (), ()) for st in book.by_idx}
        symbol = "TEST-USDT"
        book._hist_rows[symbol] = {base.id: [(c, symbol, "LONG", g, g, c - e, "tp", base.id)
                                             for c, e, g in zip(close, entry, gross)]}
        now = early + 3 * 3600
        with book._lock:
            book._learn_grid_floors((symbol,), now)
        native = dict(book.set_floor_auto)
        AutoFloors(book, SimpleNamespace(by_set=by_set, cf=cost)).apply(book, now)
        self.assertEqual(book.set_floor_auto, native)
        self.assertNotIn(empty.id, book.set_floor_auto)

    def test_cts_c_technical_packs(self):
        bars = [[100 + i * .02, 100.05 + i * .02, 99.95 + i * .02, 100 + i * .02, 100] for i in range(160)]
        final = [103.2, 105.2, 103.1, 105.0, 200]
        self.assertEqual(len(CTS_C_TECHNICAL), 27)
        self.assertEqual(resolve("ctsc_efficiency_n6_t1")(bars), (1, .75))
        self.assertEqual(resolve("ctsc_volume_break_n6_t1")([*bars, final]), (1, .75))
        self.assertIsNone(resolve("ctsc_volume_break_n6_t1")([*bars, [*final[:4], 100]]))
        self.assertEqual(resolve("ctsc_atr_break_n6_t1")([*bars, final]), (1, .75))

        path = Path(__file__).resolve().parents[1] / "presets" / "exec-indication-profiles.json"
        presets = json.loads(path.read_text())
        book = se.SetBook()
        book.load({"stageLastN": {}, "stratGeneral": False, "stratIndications": False, "directionMode": "split",
                   "exitSets": True, "exitIgnoreTp": True, "setTrailingDistance": "both",
                   "indicationConfigs": presets["ctsC"] + presets["common"]})
        self.assertEqual(len(book.packs), 11)
        self.assertEqual(book.config_report["dropped"], [])
        self.assertEqual({st.side for st in book.by_idx}, {-1, 1})
        self.assertEqual({st.kind for st in book.by_idx}, {"base", "exit", "trail"})
        self.assertEqual(len({st.id for st in book.by_idx}), len(book.by_idx))

    def test_rsi_and_sar_thin_variants_are_causal_and_directional(self):
        warm = [[100 + i * 0.2, 100.1 + i * 0.2, 99.9 + i * 0.2, 100 + i * 0.2, 20] for i in range(18)]
        side, stop, flip = sar_state(warm, .02, .02, .2)
        self.assertEqual((side, flip), (1, False))
        self.assertLessEqual(stop, warm[-2][2])
        bearish = [103.4, 103.5, 101.0, 101.1, 20]
        side, stop, flip = sar_state(warm + [bearish], .02, .02, .2)
        self.assertEqual((side, flip), (-1, True))
        self.assertGreaterEqual(stop, max(bearish[1], warm[-1][1]))
        self.assertEqual(sar_signal(warm + [bearish], .02, .02, .2, True), (-1, .72))
        self.assertIsNone(sar_signal([[10, 10, 10, 10, 0]] * 18, .02, .02, .2, False))
        prices = [100 - i * 0.2 for i in range(80)] + [90]
        bars = [[c, c + .03, c - .03, c, 20] for c in prices]
        self.assertIsNone(resolve("rsi_reclaim_n5_b25_t1")(bars[:-1]))
        self.assertEqual(resolve("rsi_reclaim_n5_b25_t1")(bars), (1, .72))
        mirror = [[200 - c, 200 - l, 200 - h, 200 - c, v] for _, h, l, c, v in bars]
        self.assertEqual(resolve("rsi_reclaim_n5_b25_t1")(mirror), (-1, .72))
        self.assertEqual(len(THIN_INDICATOR_SETTINGS), 30)

        path = Path(__file__).resolve().parents[1] / "presets" / "exec-indication-profiles.json"
        book = se.SetBook()
        book.load({"stageLastN": {}, "stratGeneral": False, "stratIndications": False, "directionMode": "split",
                   "indicationConfigs": json.loads(path.read_text())["rsiSar"]})
        self.assertEqual(len(book.packs), 30)
        self.assertEqual(book.config_report["dropped"], [])
        self.assertTrue(all({st.side for st in book.by_idx if st.pack == pack} == {-1, 1} for pack in book.packs))

    def test_default_paths_and_signed_windows(self):
        grid = sg.StageGrid.from_settings({})
        self.assertEqual(grid.capacities(), (350, 49000, 6860000, 960400000))
        for depth in range(4):
            choices = grid.windows(depth, 30)
            self.assertEqual({w.last_mode for w in choices}, {"positive", "negative"})
            self.assertEqual({w.short for w in choices if w.short_mode == "negative"}, {4, 8, 12})
            if depth:
                self.assertEqual({w.long for w in choices}, {0, 30})
        grid_setting = grid.settings()
        self.assertEqual(sg.StageGrid.from_settings(grid_setting), grid)
        with self.assertRaises(ValueError):
            sg.StageGrid.from_settings({"exec": {"short": [16], "shortModes": ["negative"]}})

    def test_each_parent_and_sign_matches_plain_walk(self):
        downstream = {"short": [4], "last": [1], "longModes": ["none", "inherit"]}
        grid = sg.StageGrid.from_settings({"base": {"long": [10, 20], "short": [4], "last": [1, 2]},
                                         "main": downstream, "real": downstream, "exec": downstream})
        rng = random.Random(8)
        rows = [sg.StageOrder(i // 2, i // 2 - i % 4, (.004 if i >= 130 and i % 10 else rng.choice([.002, .003, .004, -.001, -.002, -.003])))
                for i in range(220)]
        history = sg.StageHistory(grid, *zip(*rows), 80, lambda _: 1.05)
        examples = []
        for now in (27, 58, 96, 106, 111):
            expected, paths = plain(grid, rows, now, 80, 1.05)
            got = history.evaluate(now, streams=True)
            for tally, (n, full, qualified, gain, loss, orders) in zip(got.counts, expected):
                self.assertEqual((tally.evaluated, tally.sample_full, tally.qualified, tally.orders), (n, full, qualified, orders))
                self.assertAlmostEqual(tally.gain, gain)
                self.assertAlmostEqual(tally.loss, loss)
            self.assertEqual(got.ready, any(len(k) == 4 for k in paths))
            if got.path:
                self.assertEqual(got.streams, tuple(paths[got.path[:i]] for i in range(1, len(got.path) + 1)))
            if got.ready:
                examples.append(got.path)
            for key in paths:
                self.assertEqual(history.stream(key, now - 80, now)[-1], paths[key])
        self.assertTrue(examples, "fixture must qualify at Exec")
        expected, paths = plain(grid, rows, 111, 80, 1.05)
        finals = [path for path in paths if len(path) == 4]
        self.assertGreater(len(finals), 1)
        best = history.evaluate(111).path
        other = next(path for path in finals if path != best)
        identity = sg.lane_id("set1", other)
        self.assertEqual(grid.path("set1", identity), other)
        self.assertEqual(grid.path("set2", identity), ())
        this_real = tuple((row.close, row.net) for row in paths[other[:3]])
        self.assertEqual(history.active_real(other, 111, 200), this_real)
        self.assertIsNone(history.active_real(other, 1, 200))

    def test_equal_clock_and_insufficient_lanes(self):
        grid = sg.StageGrid.from_settings({})
        rows = [sg.StageOrder(i, i-1, .01) for i in range(1, 16)]
        history = sg.StageHistory(grid, *zip(*rows), 60, lambda _: 1.05)
        self.assertEqual(history.evaluate(15).counts[0].sample_full, 0)
        result = history.evaluate(16)
        self.assertEqual(result.counts[0].sample_full, 4)   # one L10/S4/Last1 combination, four sign pairs
        self.assertEqual(result.counts[0].qualified, 1)
        self.assertEqual(result.counts[1].sample_full, 0)
        self.assertFalse(result.ready)

    @unittest.skipIf(sg.np is None, "optional vector operations unavailable")
    def test_vector_mask_matches_scalar(self):
        grid = sg.StageGrid.from_settings({})
        rng = random.Random(23)
        rows = [sg.StageOrder(i // 2, i // 2 - i % 4, rng.choice([.001, -.001, -.003, .003150000000001, 0, 2e-13]))
                for i in range(500)]
        without = lambda xs: len(xs) >= 3 and math.fsum(xs) < .001
        vector = sg.StageHistory(grid, *zip(*rows), 99, lambda _: 1.05, main_filter=without, filter_need=3)
        with patch.object(sg, "np", None):
            scalar = sg.StageHistory(grid, *zip(*rows), 99, lambda _: 1.05,
                                     main_filter=without, filter_need=3).evaluate(238, streams=True)
        self.assertEqual(vector.evaluate(238, streams=True), scalar)

    def test_exec_gate_mirror_sides_and_settlement(self):
        book = se.SetBook()
        book.load({"stageLastN": {"enabled": True}, "histEnabled": True, "setStepMax": 3, "setMinStep": 3})
        st = book.by_idx[0]
        rows = [(60.0*i, "TEST-USDT", "LONG", .01, .011, 1.0, "tp", st.id) for i in range(1, 161)]
        st.hist = [dict(zip(se._ROW_KEYS, row)) for row in rows]
        book._score_one(st, 161*60.0)
        lane = book.exec_lane_id(st)
        self.assertEqual(book.gate_reason(st), "")
        self.assertTrue(lane.startswith(st.id + "/base:"))
        rejected = book.by_idx[1]
        rejected.hist = st.hist[:25]
        book._score_one(rejected, 161*60.0)
        self.assertEqual(book.exec_lane_id(rejected), "")
        self.assertTrue(book.gate_reason(rejected))
        pulse = pt.Pulse.__new__(pt.Pulse)
        pulse.sets, pulse.open, pulse.owned_syms, pulse.seen_fill_cids = book, {}, set(), set()
        pulse.volume_factor, pulse.equity, pulse.size_pct = 1, 1000, 1
        self.assertEqual(pulse.sized_notional(), 2)
        pulse.strict_size_cap, pulse.size_max = True, 25
        self.assertEqual(pulse.strict_entry_budget(), 2)
        book.group_gate = book.config_gate = True
        gate = SimpleNamespace(scope_allows=lambda *_: (False, "stale-real-scope"), axes={"last": SimpleNamespace(enabled=True)})
        with patch.object(pulse, "coord", gate, create=True), patch.object(se.time, "time", return_value=161*60.0):
            self.assertEqual(pulse._scope_choice(st.pack), (st, "pos", ""))
        book.group_gate = book.config_gate = False
        long = pt.Position("TEST-USDT", "LONG", 1, 100, 1, 99, 102, 100, exec_lane_id=lane, order_id="101", set_id=st.id)
        short = pt.Position("TEST-USDT", "SHORT", 1, 100, 1, 101, 98, 100, exec_lane_id=lane, order_id="201", set_id=st.id)
        pulse.track_position(long)
        pulse.track_position(short)
        self.assertEqual(len(pulse.open), 2)
        self.assertEqual(book._held_exec_paths[st.id][lane], 2)
        self.assertTrue(pulse.quantity_controls_only())
        self.assertIs(pulse.tracked_position(long.symbol, "SHORT"), short)
        long.fee_legs = {"101": {}, "102": {}}
        long.control_fill_state = {"102": {}, "103": {}}
        self.assertEqual(pt.count_position_orders(long), 3)
        with tempfile.TemporaryDirectory() as tmp, patch.object(pt, "OPEN_PATH", str(Path(tmp) / "open.json")):
            self.assertTrue(pulse.save_open_book())
            pulse.forget_position(long)
            pulse.forget_position(short)
            pulse._load_open_book()
        restored = pulse.tracked_position(long.symbol, "LONG")
        self.assertEqual(restored.exec_lane_id, lane)
        pulse.forget_position(restored)
        self.assertEqual(book._held_exec_paths[st.id][lane], 1)
        self.assertEqual(len(pulse.open), 1)
        settled = pt.Closed(161*60.0, long.symbol, long.side, 1, 100, 101, .9, .01, "tp", 60,
                            exec_lane_id=lane, filled_orders=3, set_id=st.id, net_pnl_pct=.009)
        loss = pt.Closed(162*60.0, long.symbol, long.side, 1, 100, 99, -.6, -.01, "sl", 60,
                         exec_lane_id=lane, filled_orders=2, set_id=st.id, net_pnl_pct=-.006)
        pulse._exec_note(settled)
        pulse._exec_note(loss)
        report = pulse.execution_snapshot()["exec"]
        self.assertEqual((report["activePositions"], report["activeFilledOrders"], report["closedFilledOrders"]), (1, 1, 5))
        self.assertEqual(report["pf"], 1.5)
        before = st.exec_grid
        book.on_live_close(loss)
        self.assertIs(st.exec_grid, before)


if __name__ == "__main__":
    unittest.main()
