#!/usr/bin/env python3
"""Independent, causal oracle for disjoint windows and Exec attribution."""
from dataclasses import asdict
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
import stage_eval as stage_eval_module
from indication_extra import (CTS_C_TECHNICAL, THIN_INDICATOR_SETTINGS, bollinger_reclaim, keltner_cross,
                              resolve, rsi_trend_reclaim, sar_retest, sar_signal, sar_state)
from stage_eval import AutoFloors, execute, gate_metrics, relation_rows


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

        def meets_floor(block, negative=False):
            gain = math.fsum(x for x in block if x > 0)
            loss = math.fsum(-x for x in block if x < 0)
            if negative:
                gain, loss = loss, gain
            threshold = floor(decision) if callable(floor) else floor
            return gain > 0 and gain >= threshold * loss

        if long and not meets_floor(long):
            return True, None
        negative_short = w.short_mode == "negative"
        if not meets_floor(short, negative_short) or (negative_short and math.fsum(short) >= -1e-12):
            return True, None
        net = math.fsum(last)
        negative_last = w.last_mode == "negative"
        if (not meets_floor(last, negative_last) or
                not (net < -1e-12 if negative_last else net > 1e-12)):
            return True, None
        values = long + short + last
        if not meets_floor(values):
            return True, None
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
    def test_rule_reload_discards_in_flight_tape_and_scores_the_complete_universe(self):
        block = {"short": [4], "last": [1], "shortModes": ["positive"], "lastModes": ["positive"]}
        config = {"stageLastN": {"base": {**block, "long": [10]},
                                  **{s: {**block, "longModes": ["none"]} for s in sg.STAGES[1:]}},
                  "histKeepLastComplete": True, "histLookbackAuto": False, "histLookbackBars": 240,
                  "histMinBars": 60, "minPfStepAuto": False, "setMinStep": 4, "setStepMax": 4,
                  "stratIndications": False, "stratTrailing": False, "exitSets": False,
                  "setSlRatios": [1.0], "directionMode": "both"}
        changed = {**config, "lastNBasis": "count"}
        book = se.SetBook()
        book.load(config)
        names = ("AAA-USDT", "BBB-USDT")
        now = 48 * 3600.0
        book.bars = {s: [[100.0, 102.0, 99.0, 101.0, 1.0]] * 120 for s in names}
        calls = []
        change_during_replay = False

        def replay(symbol, rows, decision, on_step=None):
            calls.append(symbol)
            for st in book.by_idx:
                rows[st.id].extend(dict(t=decision - 120 * i - names.index(symbol) * 60,
                                        symbol=symbol, side="LONG", pnl=.019, pnl_pct=.02,
                                        hold_s=30, reason="fixture", set_id=st.id)
                                   for i in range(40, 0, -1))
            if change_during_replay:
                book.load(changed)
            return {}

        book._replay_symbol = replay
        book.replay_all(now=now, expected_symbols=names)
        st = book.by_idx[0]
        self.assertTrue(st.exec_grid.ready)
        self.assertTrue(book.progress.ready)

        change_during_replay = True
        book.replay_all(now=now, expected_symbols=names)
        change_during_replay = False
        self.assertEqual(calls, [*names, names[0]])
        self.assertFalse(book._hist_rows)
        self.assertIsNone(st.exec_grid)
        self.assertTrue(book._lookback_pending)

        book.replay_all(now=now, symbols=names[:1], expected_symbols=names)
        self.assertFalse(book.progress.ready)
        self.assertEqual(book.progress.sets_done, 0)
        self.assertIsNone(book.pick_any(st.pack))
        book.load(changed)  # A routine reload must leave the fixed-window rebuild pending.
        self.assertTrue(book._lookback_pending)
        book.replay_all(now=now, symbols=names[1:], expected_symbols=names)
        self.assertFalse(book.progress.ready)
        book.replay_all(now=now, symbols=names[:1], expected_symbols=names)
        self.assertTrue(book.progress.ready)
        self.assertFalse(book._lookback_pending)
        self.assertEqual(st.n, 80)
        self.assertTrue(st.exec_grid.ready)

    def test_auto_pf_refits_when_late_training_closes_arrive(self):
        book = se.SetBook()
        book.load({"stageLastN": {}, "minPfByStep": True, "minPfStepAuto": True,
                   "setMinStep": 4, "setStepMax": 4, "stratIndications": False, "setSlRatios": [1.0]})
        st = next(st for st in book.by_idx if st.kind == "base")
        symbol, hour = "BTC-USDT", float(48 * 3600)
        rows = [(hour - (100 - i) * 60, symbol, "LONG", .005, .006, 30, "tp", st.id) for i in range(103)]
        book._hist_rows[symbol] = {st.id: rows[:70]}
        book._hist_replay_bar_at[symbol] = rows[69][0] - se.BAR_S
        with book._lock:
            book._learn_grid_floors((symbol,), hour + 15)
            incomplete = book._grid_auto_models[st.id]
            book._hist_rows[symbol] = {st.id: rows[:100]}
            book._hist_replay_bar_at[symbol] = hour - 2 * se.BAR_S
            book._learn_grid_floors((symbol,), hour + 30)
            complete = book._grid_auto_models[st.id]
            self.assertGreater(len(complete.closes), len(incomplete.closes))
            self.assertLess(complete.closes[-1], hour)
            book._hist_rows[symbol] = {st.id: rows}
            book._hist_replay_bar_at[symbol] = hour + se.BAR_S
            book._learn_grid_floors((symbol,), hour + 130)
            self.assertIs(book._grid_auto_models[st.id], complete)

    def test_exec_replay_uses_the_live_kind_policy_and_stable_set_rank(self):
        win = {"short": [4], "last": [1], "shortModes": ["positive"], "lastModes": ["positive"]}
        book = se.SetBook()
        book.load({"stageLastN": {"base": {**win, "long": [10]},
                                  **{s: {**win, "longModes": ["none"]} for s in sg.STAGES[1:]}},
                   "minPfStepAuto": False, "setMinStep": 4, "setStepMax": 4, "histEnabled": True,
                   "stratIndications": False, "stratTrailing": False, "exitSets": False,
                   "setSlRatios": [.5, 1.0], "directionMode": "split",
                   "blockEnabled": False, "dcaEnabled": False})
        choices = [st for st in book.by_idx if st.kind == "base" and st.side == 1]
        self.assertEqual(len(choices), 2)
        first, second = choices
        symbol, hour = "BTC-USDT", 200 * 3600
        dates = list(range(hour, hour + 3600, 60))
        past = [hour - 60 * i for i in range(160, 0, -1)]
        entries = [t - 30 for t in past]
        gross = [.02] * len(past)
        tapes = SimpleNamespace(by_set={st.idx: (past, entries, gross) if st in choices else ([], [], [])
                                        for st in book.by_idx}, cf=se.cost_as_frac(book.cost_pct))
        def tape_window(idx, now, lookback_s, n):
            tx, te, pp = tapes.by_set[idx]
            end = sum(t < now for t in tx)
            lo = sum(t < now - lookback_s for t in tx[:end])
            start = max(lo, end - n)
            return tx[start:end], te[start:end], pp[start:end]
        tapes.window = tape_window
        # The second Set becomes ready first in the live candidate index.
        for st in (second, first):
            rows = [(t, symbol, "LONG", .019, .02, 30, "tp", st.id) for t in past]
            book.set_processed(st, rows, hour + 60)
            book._score_one(st, now=hour + 60)
        self.assertEqual(first.exec_grid.rank, second.exec_grid.rank)
        self.assertIs(book.pick_any(first.pack, side=1), first)
        self.assertEqual(gate_metrics(book, tapes, first, hour + 60)[1], book.lane_rank_key(first))
        # An old Negative-lane preference must not hide the new Exec stage.
        book.lane_order = ("neg",)
        bars = [[100.0, 102.0, 100.0, 100.0] for _ in dates]
        signals = {first.pack: [1] * len(dates)}
        confidence = {first.pack: [.9] * len(dates)}
        shadow = {symbol: (0, signals, confidence, {})}
        report = execute(book, [symbol], {symbol: bars}, {symbol: dates}, shadow, tapes, [hour])
        self.assertEqual({r["set"] for r in report if "set" in r}, {first.id})
        self.assertTrue(all(r["execLaneId"] == book.exec_lane_id(first) for r in report if "set" in r))
        with patch.object(stage_eval_module, "gate_until", return_value=float("inf")), \
                patch.object(stage_eval_module, "gate_metrics", wraps=stage_eval_module.gate_metrics) as cached_gates:
            cached_report = execute(book, [symbol], {symbol: bars}, {symbol: dates}, shadow, tapes, [hour], refresh_s=120)
        with patch.object(stage_eval_module, "gate_until", side_effect=lambda _book, _tapes, _st, now: now), \
                patch.object(stage_eval_module, "gate_metrics", wraps=stage_eval_module.gate_metrics) as repeated_gates:
            repeated_report = execute(book, [symbol], {symbol: bars}, {symbol: dates}, shadow, tapes, [hour], refresh_s=120)
        self.assertEqual(cached_gates.call_count, len(book.by_idx))
        self.assertGreater(repeated_gates.call_count, cached_gates.call_count)
        project = lambda rows: [(r.get("t_in"), r.get("set"), r.get("symbol"), r.get("reason"), r.get("net"))
                                for r in rows if "set" in r]
        self.assertEqual(project(cached_report), project(repeated_report))
        tapes.__dict__.pop("_exec_grid_query", None)
        parallel_report = execute(book, [symbol], {symbol: bars}, {symbol: dates}, shadow, tapes, [hour],
                                  refresh_s=120, rank_workers=2)
        self.assertEqual(project(cached_report), project(parallel_report))
        deny_axis = SimpleNamespace(min_pf_linked=True,
                                    judge=lambda _rows, _streak, min_pf=None: {"allow": False, "metrics": {"last15Pf": 0.0}},
                                    net_streak=lambda _rows: 0,
                                    slot_cap=lambda *_args, **_kwargs: 1,
                                    axes={})
        tapes.__dict__.pop("_exec_grid_query", None)
        with patch.object(stage_eval_module, "axis_judge", wraps=stage_eval_module.axis_judge) as axis_checks, \
                patch.object(book, "grid_history", wraps=book.grid_history) as grid_builds:
            axis_blocked = execute(book, [symbol], {symbol: bars}, {symbol: dates}, shadow, tapes, [hour],
                                   axis_coord=deny_axis, refresh_s=120)
        self.assertFalse([r for r in axis_blocked if "set" in r])
        axis_sets = {(call.args[3].idx, call.args[5]) for call in axis_checks.call_args_list}
        self.assertEqual(grid_builds.call_count, len(book.by_idx) + len(axis_sets))
        expected_axis_snapshots = {float(hour + 60 + 120 * i) for i in range(30)}
        self.assertTrue({call.args[4] for call in axis_checks.call_args_list}.issubset(expected_axis_snapshots))
        self.assertTrue(all(not item[1].streams and len(item[1].axis_stream_tail) <= se.PROC_KEEP
                            for item in tapes.__dict__["_exec_grid_query"].values()))
        book.processing_overlay.update(stratNormal=False, axisPrevEnabled=False, axisLastEnabled=False,
                                       axisContEnabled=False, axisPauseEnabled=False)
        self.assertIsNone(book.pick_any(first.pack, side=1))
        self.assertFalse(gate_metrics(book, tapes, first, hour + 60)[0])
        disabled = execute(book, [symbol], {symbol: bars}, {symbol: dates}, shadow, tapes, [hour])
        self.assertFalse([r for r in disabled if "set" in r])

    def test_replay_position_cap_counts_open_slots_and_releases_closed_ones(self):
        block = {"short": [4], "last": [1], "shortModes": ["positive"], "lastModes": ["positive"]}
        book = se.SetBook()
        book.load({"stageLastN": {"base": {**block, "long": [10]},
                                  **{s: {**block, "longModes": ["none"]} for s in sg.STAGES[1:]}},
                   "minPfStepAuto": False, "setMinStep": 4, "setStepMax": 4, "minSlDistancePct": .1,
                   "stratIndications": False, "stratTrailing": False, "exitSets": False,
                   "setSlRatios": [1.0], "directionMode": "both", "timeStopS": 3600})
        st = book.by_idx[0]
        now, names = 200 * 3600, ["AAA-USDT", "BBB-USDT"]
        history = [now - 60*i for i in range(90, 0, -1)]
        tapes = SimpleNamespace(by_set={st.idx: (history, [t-30 for t in history], [.02]*len(history))},
                                cf=se.cost_as_frac(book.cost_pct))
        def tape_window(idx, now, lookback_s, n):
            tx, te, pp = tapes.by_set[idx]
            end = sum(t < now for t in tx)
            lo = sum(t < now - lookback_s for t in tx[:end])
            start = max(lo, end - n)
            return tx[start:end], te[start:end], pp[start:end]
        tapes.window = tape_window
        bars = {s: [[100, 100.1, 99.9, 100]]*8 for s in names}
        times = {s: [now + 60*i for i in range(8)] for s in names}
        shadow = {s: (0, {st.pack: [1,0,0,0,1,0,0,0]}, {st.pack: [.9]*8}, {}) for s in names}

        def replay(cap):
            return [r for r in execute(book, names, bars, times, shadow, tapes, [now],
                                       include_open=True, max_open=cap) if 'symbol' in r]

        self.assertEqual({r['symbol'] for r in replay(0)}, set(names))
        self.assertEqual([(r['symbol'],r['open_at_end']) for r in replay(1)], [(names[0],True)])
        bars[names[0]][2] = [100, 102, 99.9, 101]
        shadow[names[0]][1][st.pack][4]=0
        final = replay(1)
        self.assertEqual([r['symbol'] for r in final], names)
        self.assertLess(final[0]['t'], final[1]['t_in'])

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
        model = book._grid_auto_models[base.id]
        self.assertEqual(model.at(early), 1.24)
        updated = auto_set_pf.RollingFloors(close, entry, [*net[:80], *([500] * (len(net) - 80))],
                                           book.pf_step_auto_n, book.pf_set_auto_min, 1.24,
                                           book.step_auto_candidates(), book.pf_step_auto_tol, book.pf_auto_factor)
        self.assertEqual(model.at(early + 3600), updated.at(early + 3600))

    def test_packed_auto_pf_matches_training_window_observations(self):
        rng = random.Random(53)
        closes = [60 * i for i in range(64 * 60)]
        entries = [close - rng.randrange(1, 350) for close in closes]
        nets = []
        for i in range(len(closes)):
            if i % 90 >= 35:
                nets.append(.012 if rng.random() < .75 else -.005)
            else:
                nets.append(.006 if rng.random() < .31 else -.011)
        previous = 12
        options = (20, 1.04, (1.08, 1.15, 1.35, 1.70, 2.5, 3.5), .05, .3)
        model = auto_set_pf.RollingFloors(closes, entries, nets, previous, *options)
        results = []
        for hour in (0, 1, 14, 27, 48, 49, 52, 64, 71):
            now = hour * 3600
            lower, upper = auto_set_pf.training_window(now)
            values = auto_set_pf.observations(closes, entries, nets, previous, lower, upper)
            expected = auto_set_pf.fit(values, *options)
            self.assertEqual(model.learned(now), expected)
            self.assertEqual(model.at(now), 1.04 if expected is None else expected)
            results.append(expected)
        self.assertIsNone(results[0])
        self.assertTrue(any(floor is not None for floor in results))

    def test_auto_hold_set_learns_only_from_processed_hold_tape(self):
        book = se.SetBook()
        book.load({"stageLastN": {}, "minPfByStep": True, "minPfStepAuto": True,
                   "holdAuto": True, "setMinStep": 4, "setStepMax": 4, "stratIndications": False})
        st = next(st for st in book.by_idx if st.kind == "base")
        self.assertTrue(book.auto_holds(st))
        symbol = "TEST-USDT"
        cost = se.cost_as_frac(book.cost_pct)
        rows = [(100 + i * 60, symbol, "LONG", .01, .01 + cost, 30, "time", st.id) for i in range(200)]
        book._hist_rows[symbol] = {st.id: [row[:4] + (-.01,) + row[5:] for row in rows]}
        now = 4 * 3600
        with book._lock:
            book._learn_grid_floors((symbol,), now)
            self.assertNotIn(st.id, book._grid_auto_models)
            book._learn_held_grid_floor(st, rows, now)
        model = book._grid_auto_models[st.id]
        self.assertEqual(model._entry_pfs[-1], math.inf)
        self.assertAlmostEqual(model._net_returns[-1], .01)
        self.assertTrue(book.grid_history(st, rows).evaluate(now, streams=True).counts[0].qualified)

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
        self.assertEqual(len(THIN_INDICATOR_SETTINGS), 57)

        path = Path(__file__).resolve().parents[1] / "presets" / "exec-indication-profiles.json"
        book = se.SetBook()
        book.load({"stageLastN": {}, "stratGeneral": False, "stratIndications": False, "directionMode": "split",
                   "indicationConfigs": json.loads(path.read_text())["rsiSar"]})
        self.assertEqual(len(book.packs), 30)
        self.assertEqual(book.config_report["dropped"], [])
        self.assertTrue(all({st.side for st in book.by_idx if st.pack == pack} == {-1, 1} for pack in book.packs))

    def test_trend_retest_and_channel_signals_mirror_and_require_the_last_close(self):
        def rows(prices, width):
            return [[c, c + width, c - width, c, 25] for c in prices]

        rising = [100 + i * .2 for i in range(75)]
        top = rising[-1]
        sar = [100 + i * .05 for i in range(80)]
        cases = (
            (lambda b: rsi_trend_reclaim(b, 7, 24), rows(rising + [top - .7, top - .98, top - 1.33, top + .4], .16)),
            (lambda b: sar_retest(b, .01, .01, .1), rows(sar + [sar[-1] - .25, sar[-1] - .2, sar[-1] + .14], .1)),
            (lambda b: bollinger_reclaim(b, 12), rows([99, 101] * 6 + [97, 99], .16)),
            (lambda b: keltner_cross(b, 12), rows([100 + .08 * i for i in range(60)] + [105.72], .16)),
        )
        for fn, bars in cases:
            self.assertIsNone(fn(bars[:-1]))
            self.assertEqual(fn(bars), (1, .72))
            self.assertEqual(fn([[250 - o, 250 - lo, 250 - hi, 250 - c, v] for o, hi, lo, c, v in bars]), (-1, .72))
            self.assertIsNone(fn([[100] * 4 + [10] for _ in range(len(bars))]))

        profiles = json.loads((Path(__file__).resolve().parents[1] / "presets/exec-indication-profiles.json").read_text())
        names = {name for row in profiles["rsiSar"] + profiles["confirmed"] for name in row["names"]}
        self.assertEqual(names, set(THIN_INDICATOR_SETTINGS))
        book = se.SetBook()
        book.load({"stageLastN": {}, "stratGeneral": False, "stratIndications": False,
                   "directionMode": "split", "indicationConfigs": profiles["confirmed"]})
        self.assertEqual(book.config_report["dropped"], [])
        self.assertEqual(len(book.packs), 27)
        self.assertEqual(set(st.side for st in book.by_idx), {-1, 1})

    def test_inverse_exec_profiles_build_distinct_directional_sets(self):
        profiles = json.loads((Path(__file__).resolve().parents[1] / "presets/exec-indication-profiles.json").read_text())
        book = se.SetBook()
        book.load({"stageLastN": {}, "stratGeneral": False, "stratIndications": False,
                   "directionMode": "split", "indicationConfigs": profiles["inverseExec"]})
        self.assertEqual(book.config_report["dropped"], [])
        self.assertEqual(len(book.packs), 4)
        self.assertEqual(len(book.by_idx), 8)
        self.assertTrue(all({st.side for st in book.by_idx if st.pack == pack} == {-1, 1} for pack in book.packs))
        self.assertTrue(all("@I~cfg" in st.id and st.kind == "base" for st in book.by_idx))

    def test_shared_thin_frames_match_separate_profile_calculations(self):
        rng = random.Random(71)
        prices = [100.0]
        for _ in range(1_864):
            prices.append(prices[-1] * rng.uniform(0.996, 1.005))
        bars = [[o, max(o, c) * 1.0004, min(o, c) * .9996, c, 20.0]
                for o, c in zip(prices, prices[1:])]
        packs = [f"extra.{name}" for name in THIN_INDICATOR_SETTINGS]
        for index in (0, 21, 78, 190, 491, 900, 1_800, 1_863):
            expected = {p: se._variant_signal(bars[:index + 1], [p.removeprefix("extra.")]) for p in packs}
            self.assertEqual(se.bar_signal(bars, index, packs, {}, 60.0 * (index + 1)), expected)

    def test_real_and_exec_short_thirty_read_their_own_parents(self):
        base = {"long": [10], "short": [4], "last": [1], "shortModes": ["positive"], "lastModes": ["positive"]}
        child = {"short": [4], "last": [1], "longModes": ["none"],
                 "shortModes": ["positive"], "lastModes": ["positive"]}
        grid = sg.StageGrid.from_settings({"base": base, "main": child, "real": {**child, "short": [30]},
                                           "exec": {**child, "short": [28, 30]}})
        rows = [(60.0 * t, 60.0 * t - 1, .005) for t in range(95)]
        actual = sg.StageHistory(grid, *zip(*rows), 2400, lambda _: 1.1).evaluate(95 * 60, streams=True)
        counts, expected = plain(grid, rows, 95 * 60, 2400, 1.1)
        self.assertTrue(actual.ready)
        self.assertEqual(actual.path[-2].short, 30)
        self.assertEqual(actual.path[-1].short, 30)
        self.assertEqual(actual.counts[-1].qualified, counts[-1][2])
        self.assertEqual(actual.streams[-1], expected[actual.path])
        self.assertEqual(actual.selected_blocks[-1][1].count, 30)
        self.assertEqual(actual.selected_blocks[-1][2].count, 1)

    def test_summary_evaluation_keeps_stage_pf_without_order_streams(self):
        base = {"long": [10], "short": [4], "last": [1], "shortModes": ["positive"], "lastModes": ["positive"]}
        child = {"short": [4], "last": [1], "longModes": ["none"],
                 "shortModes": ["positive"], "lastModes": ["positive"]}
        grid = sg.StageGrid.from_settings({"base": base, "main": child, "real": child, "exec": child})
        rows = [(60.0 * t, 60.0 * t - 1, .005) for t in range(100)]
        history = sg.StageHistory(grid, *zip(*rows), 2400, lambda _: 1.1)
        summary = history.evaluate(100 * 60, summary=True)
        full = history.evaluate(100 * 60, streams=True)
        bounded = history.evaluate(100 * 60, stream_tail=7)
        self.assertEqual((summary.path, summary.counts, summary.post_evidence, summary.validated_base),
                         (full.path, full.counts, full.post_evidence, full.validated_base))
        self.assertEqual(bounded.axis_stream_tail, full.streams[2][-7:])
        self.assertTrue(summary.ready)
        self.assertEqual(summary.streams, ())
        self.assertEqual(summary.row_ids, ())
        self.assertEqual(summary.validated_base_ids, ())
        self.assertEqual(summary.selected_blocks, ())
        self.assertEqual(bounded.streams, ())
        self.assertEqual(bounded.row_ids, ())

    def test_decision_context_keeps_all_parent_memberships_and_original_ids(self):
        class FullHistory(sg.StageHistory):
            def _decision_tape(self, now):
                return self.root

        grid = sg.StageGrid.from_settings({"base": {"long": [10, 20], "short": [4, 8], "last": [1, 2]},
                                           "main": {"short": [4, 8], "last": [1, 2]},
                                           "real": {"short": [4, 8], "last": [1, 2]},
                                           "exec": {"short": [4, 8, 20, 30], "last": [1, 2]}})
        rng = random.Random(18)
        closes = [i // 2 for i in range(2_600)]
        entries = [t - (180 if i % 45 == 0 else rng.randrange(0, 9)) for i, t in enumerate(closes)]
        net = [rng.choice((.004, .006, .008, -.003, -.005)) for _ in closes]
        settings = (grid, closes, entries, net, 85, lambda _n: 1.08)
        bounded, full = sg.StageHistory(*settings), FullHistory(*settings)
        for now in (1150, 1225, 1090, 1280):
            self.assertLess(len(bounded._decision_tape(now).ids), len(closes))
            self.assertEqual(bounded._decision_tape(now), bounded._decision_tape(now))
            new, old = (x.evaluate(now, streams=True, limit=9) for x in (bounded, full))
            self.assertEqual((new.path, new.row_ids, new.streams, new.validated_base_ids),
                             (old.path, old.row_ids, old.streams, old.validated_base_ids))
            for a, b in zip(new.counts, old.counts):
                self.assertEqual((a.evaluated, a.sample_full, a.qualified, a.orders), (b.evaluated, b.sample_full, b.qualified, b.orders))
                self.assertAlmostEqual(a.gain, b.gain)
                self.assertAlmostEqual(a.loss, b.loss)
            for a, b in zip((*new.post_evidence, new.validated_base), (*old.post_evidence, old.validated_base)):
                self.assertEqual(a.count, b.count)
                self.assertAlmostEqual(a.gain, b.gain)
                self.assertAlmostEqual(a.loss, b.loss)
            if new.ready:
                self.assertEqual(bounded.active_real(new.path, now, 14), full.active_real(old.path, now, 14))

    def test_default_paths_and_signed_windows(self):
        grid = sg.StageGrid.from_settings({})
        self.assertEqual(grid.capacities(), (350, 49000, 10780000, 2371600000))
        for depth in range(4):
            choices = grid.windows(depth, 30)
            self.assertEqual({(w.last, w.last_mode) for w in choices},
                             {(size, sign) for size in (1, 2, 3, 4, 5) for sign in ("positive", "negative")})
            self.assertEqual({w.short for w in choices if w.short_mode == "negative"}, {4, 8, 12})
            self.assertEqual({w.short for w in choices if w.short_mode == "positive"},
                             set(sg.LATE_SHORT if depth >= 2 else sg.SHORT))
            if depth:
                self.assertEqual({w.long for w in choices}, {0, 30})
        grid_setting = grid.settings()
        self.assertEqual(sg.StageGrid.from_settings(grid_setting), grid)
        with self.assertRaises(ValueError):
            sg.StageGrid.from_settings({"exec": {"short": [16], "shortModes": ["negative"]}})
        extended = sg.StageGrid.from_settings({"exec": {"short": [12, 20, 24, 28, 30]}})
        self.assertEqual({w.short for w in extended.windows(3, 40) if w.short_mode == "negative"}, {12})
        self.assertEqual({w.short for w in extended.windows(3, 40) if w.short_mode == "positive"}, {12, 20, 24, 28, 30})
        self.assertEqual(sg.StageGrid.from_settings({"real": {"short": [30]}}).options[2].shorts, (30,))
        with self.assertRaises(ValueError):
            sg.StageGrid.from_settings({"main": {"short": [30]}})

    def test_auto_floor_is_local_to_parent_window_sign_and_training_prefix(self):
        rng = random.Random(730)
        rows = [(i * 300.0, i * 300.0 - 10, rng.choice((.022, .019, -.010, -.041, .015, -.005)))
                for i in range(128)]
        policy = sg.WindowAuto(1.12, 12, (1.1, 1.25, 1.5, 2.0, 3.0), .01, 0.0)
        at = 11 * 3600
        lookback = 3600 * 4
        grid = sg.StageGrid.from_settings({})
        sg_auto = sg.StageHistory(grid, *zip(*rows), lookback, lambda _: 1.12, window_auto=policy)
        observed_fits = []
        original = auto_set_pf.fit
        def record(pairs, *args):
            observed_fits.append(tuple(pairs))
            return original(pairs, *args)
        def reference(ids, n, offset, kind):
            pairs = []
            for i in ids:
                close, entry, net = rows[i]
                if close >= at:
                    break
                visible = [rows[j][2] for j in ids if entry - lookback <= rows[j][0] < entry]
                if len(visible) < n + offset:
                    continue
                block = visible[len(visible)-offset-n:len(visible)-offset]
                gain = math.fsum(max(0.0, v) for v in block)
                loss = math.fsum(max(0.0, -v) for v in block)
                numerator, denominator = (loss, gain) if kind == "negative" else (gain, loss)
                if numerator > 0 and (kind not in sg.SIGN_MODES or numerator > denominator + sg.NEG_EPS):
                    pairs.append((numerator/denominator if denominator else math.inf, net))
            return pairs
        probe = []
        with patch.object(auto_set_pf, "fit", side_effect=record):
            for ids in (range(len(rows)), tuple(i for i in range(len(rows)) if i % 3 != 1)):
                tape = sg._Tape(sg_auto, ids)
                for n, offset, kind in ((4, 2, "pf"), (4, 3, "pf"), (5, 0, "positive"), (5, 0, "negative")):
                    actual = tape.floor(n, offset, kind, at + 30)
                    expected = reference(ids, n, offset, kind)
                    self.assertEqual(len(observed_fits[-1]), len(expected))
                    for (x, nx), (y, ny) in zip(observed_fits[-1], expected):
                        self.assertEqual(nx, ny)
                        self.assertAlmostEqual(x, y, places=10)
                    probe.append(actual)
                self.assertEqual(tape.floor(1, 0, "positive", at), 1.12)
                self.assertEqual(tape.floor(1, 0, "negative", at), 1.12)
        self.assertEqual(len(observed_fits), 8)
        self.assertGreater(len(set(observed_fits)), 4)
        future = [v if c < at else 900.0 for c, _, v in rows]
        unaffected = sg.StageHistory(grid, sg_auto.closes, sg_auto.entries, future, lookback,
                                     lambda _: 1.12, window_auto=policy)
        self.assertEqual(unaffected.root.floor(4, 2, "pf", at + 30), probe[0])
        self.assertEqual(sg._Tape(sg_auto, tuple(range(10))).floor(4, 2, "pf", at), 1.12)

    def test_positive_and_negative_last_require_configured_pf(self):
        grid = sg.StageGrid.from_settings({})
        for kind, sign in (("positive", 1), ("negative", -1)):
            for floor, allowed in ((1.2, True), (1.4, False)):
                owner = sg.StageHistory(grid, (1, 2), (.5, 1.5), (.01 * sign, -.008 * sign),
                                        100, lambda n: floor)
                self.assertEqual(owner.root.test(2, 2, kind, 3) is not None, allowed)

    def test_negative_blocks_cannot_pass_a_losing_total_or_seed_a_child(self):
        grid = sg.StageGrid.from_settings({})
        w = sg.Window(0, 4, 1, "negative", "negative")
        rows = [(i, i - .1, -.01) for i in range(1, 12)]
        history = sg.StageHistory(grid, *zip(*rows), 100, lambda _: 1.1)
        tape = history.root
        self.assertIsNotNone(tape.test(4, 10, "negative", 12))
        self.assertIsNotNone(tape.test(1, 11, "negative", 12))
        for depth in (1, 2, 3):
            self.assertFalse(tape.passes(w, depth, 12))
            self.assertEqual(tape.child_mask(w, depth), 0)
        mixed = [(.02 if i % 5 else -.002) for i in range(1, 13)]
        owner = sg.StageHistory(grid, tuple(range(1, 13)), tuple(i-.1 for i in range(1, 13)),
                                mixed, 100, lambda _: 1.1)
        eligible = sg.Window(0, 4, 1, "negative", "positive")
        self.assertTrue(owner.root.passes(eligible, 3, 11))
        self.assertTrue(owner.root.child_mask(eligible, 3) & (1 << 10))

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
            base_positions = {row for path, stream in paths.items() if len(path) == 1 for row in stream}
            expected_base = tuple(i for i, row in enumerate(rows) if row in base_positions)
            self.assertEqual(got.validated_base_ids, expected_base)
            self.assertEqual(got.validated_base.count, len(expected_base))
            for tally, (n, full, qualified, gain, loss, orders) in zip(got.counts, expected):
                self.assertEqual((tally.evaluated, tally.sample_full, tally.qualified, tally.orders), (n, full, qualified, orders))
                self.assertAlmostEqual(tally.gain, gain)
                self.assertAlmostEqual(tally.loss, loss)
            self.assertEqual(got.ready, any(len(k) == 4 for k in paths))
            if got.path:
                self.assertEqual(got.streams, tuple(paths[got.path[:i]] for i in range(1, len(got.path) + 1)))
                for w, total, blocks in zip(got.path, got.selected_evidence, got.selected_blocks):
                    self.assertEqual(tuple(e.count for e in blocks), (w.long, w.short, w.last))
                    self.assertEqual(total, sum(blocks, sg.Evidence()))
                    for ev, sign in zip(blocks[1:], (w.short_mode, w.last_mode)):
                        self.assertEqual(ev.gain < ev.loss, sign == "negative")
                for i, (evidence, stream) in enumerate(zip(got.post_evidence, got.streams), 1):
                    self.assertEqual(evidence.count, len(stream))
                    self.assertAlmostEqual(evidence.gain, math.fsum(max(0, r.net) for r in stream))
                    self.assertAlmostEqual(evidence.loss, math.fsum(max(0, -r.net) for r in stream))
                    if i > 1:
                        self.assertLessEqual(set(got.row_ids[i - 1]), set(got.row_ids[i - 2]))
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
        short_output = history.evaluate(111, streams=True, limit=2)
        self.assertEqual(short_output.post_evidence, history.evaluate(111, streams=True).post_evidence)
        report = short_output.report(grid, "set1")
        self.assertEqual(report["parentIds"]["base"], "set1")
        self.assertEqual(report["parentIds"]["exec"], report["setIds"]["real"])
        self.assertTrue(all(report["selectedBlocks"][stage]["last"]["orders"] for stage in sg.STAGES))
        self.assertGreater(short_output.validated_base.count, len(short_output.row_ids[0]))

    def test_native_and_replay_relations_read_every_qualified_base_lane_once(self):
        book = se.SetBook()
        book.load({"stageLastN": {"base":{"long":[10,20],"short":[4],"last":[1,2]}},
                   "setMinStep": 4, "setStepMax": 4, "stratIndications": False,
                   "minPfStepAuto":False})
        st = next(st for st in book.by_idx if st.kind == "base")
        cost = se.cost_as_frac(book.cost_pct)
        rng = random.Random(8)
        tape = [(60*i, "ADA-USDT" if i%2 else "BTC-USDT", "LONG", 0.0,
                 cost+rng.choice((.014,.02,.028,-.003,-.016)), 30, "tp", st.id)
                for i in range(1,160)]
        now = 160*60
        got = book.grid_history(st,tape).evaluate(now,streams=True,limit=2)
        self.assertTrue(got.validated_base_ids)
        self.assertGreater(len(got.validated_base_ids),len(got.row_ids[0]))
        seen = []
        book.set_processed(st,tape,now,relations=SimpleNamespace(add=lambda cell,rows: seen.extend(rows)),
                           grid_result=got)
        self.assertEqual(st.exec_grid.validated_base, got.validated_base)
        self.assertEqual(st.exec_grid.validated_base_ids, ())
        self.assertEqual((st.exec_grid.row_ids, st.exec_grid.streams), ((), ()))
        self.assertEqual(st.exec_grid.report(book.stage_grid, st.id), got.report(book.stage_grid, st.id))
        tx = [row[0] for row in tape]
        te = [row[0]-row[5] for row in tape]
        tapes = SimpleNamespace(by_set={st.idx:(tx,te,[row[4] for row in tape])},cf=cost,
                                symbols_by_set={st.idx:tuple(row[1] for row in tape)})
        self.assertEqual(seen, relation_rows(book,tapes,st,now))
        self.assertEqual(len(seen),got.validated_base.count)
        self.assertEqual(len({(r[0],r[1],r[2]) for r in seen}),len(seen))

    def test_rolling_thresholds_do_not_change_older_memberships(self):
        downstream = {"short": [4], "last": [1], "longModes": ["none"]}
        grid = sg.StageGrid.from_settings({"base": {"long": [10], "short": [4], "last": [1]},
                                         "main": downstream, "real": downstream, "exec": {**downstream, "short": [20, 30]}})
        rng = random.Random(32)
        rows = [sg.StageOrder(i, i - (i % 4), rng.choice((.01, .02, .04, -.017))) for i in range(760)]
        at = lambda decision: 1.10 if decision < 420 else 1.65
        now, lookback = 650, 550
        history = sg.StageHistory(grid, *zip(*rows), lookback, lambda _: 1.65, entry_floor=at)
        full = history.evaluate(now, streams=True)
        _, paths = plain(grid, rows, now, lookback, at)
        self.assertTrue(full.ready)
        self.assertEqual(full.streams, tuple(paths[full.path[:i]] for i in range(1, 5)))
        self.assertNotEqual(full.streams[0], sg.StageHistory(grid, *zip(*rows), lookback, lambda _: 1.65).stream(
            full.path, now - lookback, now)[0])
        shortened = [r for r in rows if r.close < now]
        older = sg.StageHistory(grid, *zip(*shortened), lookback, lambda _: 1.65, entry_floor=at)
        self.assertEqual(full, older.evaluate(now, streams=True))

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
        self.assertEqual(sg.Evidence(2, 0, 0).report()["pf"], 1.0)
        self.assertIsNone(sg.Evidence().report()["pf"])

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

    @unittest.skipIf(sg.np is None, "optional vector operations unavailable")
    def test_scoped_auto_vector_mask_matches_scalar_at_hour_boundaries(self):
        block = {"short": [4, 8], "last": [1, 2]}
        grid = sg.StageGrid.from_settings({"base": {**block, "long": [10, 20]},
                                           **{s: {**block, "longModes": ["none"]} for s in sg.STAGES[1:]}})
        policy = sg.WindowAuto(1.14, 10, (1.1, 1.25, 1.5), .05, 0.0)
        rng = random.Random(61)
        closes = [float(i * 60 + 60) for i in range(540)]
        entries = [t - rng.choice((1, 60, 61, 290)) for t in closes]
        nets = [rng.choice((.003, .005, .007, -.001, -.004, -.006)) for _ in closes]
        args = grid, closes, entries, nets, 6 * 3600, lambda _: 1.14
        vector = sg.StageHistory(*args, window_auto=policy)
        scalar = sg.StageHistory(*args, window_auto=policy)
        for minute in (479, 480, 481, 540):
            now = minute * 60.0
            with patch.object(sg, "np", None):
                expected = scalar.evaluate(now, streams=True)
            self.assertEqual(vector.evaluate(now, streams=True), expected)

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

    def test_exec_reentry_rests_only_its_side_and_uses_settled_net(self):
        pulse = pt.Pulse.__new__(pt.Pulse)
        pulse.sets = SimpleNamespace(stage_grid=True, pause_minutes=lambda *_: 9, cooldown_bars=2)
        pulse.cooldown, pulse.ignore_syms = {}, {}
        pulse.position_cost_pct = .176
        pos = pt.Position("TEST-USDT", "LONG", 1, 100, 601, 99, 102, 100, exec_lane_id="lane")
        with patch.object(pt.time, "time", return_value=609):
            pulse.rest_position_slot(pos, 0)
            self.assertTrue(pulse.entry_slot_rests(pos.symbol, "LONG", 609))
            self.assertFalse(pulse.entry_slot_rests(pos.symbol, "LONG", 660))
            self.assertFalse(pulse.entry_slot_rests(pos.symbol, "SHORT", 609))
            pause = pulse.symbol_backoff(pos.symbol, .0014, 62, "tp", True, "general", 1,
                                         net_pnl_pct=.0003)
            self.assertEqual(pause, 540)
            pulse.rest_position_slot(pos, pause)
            pulse.rest_position_slot(pos, 90)
            self.assertTrue(pulse.entry_slot_rests(pos.symbol, "LONG", 1000))
            self.assertFalse(pulse.entry_slot_rests(pos.symbol, "SHORT", 1000))
            pulse.cooldown[pos.symbol] = 1200
            self.assertTrue(pulse.entry_slot_rests(pos.symbol, "SHORT", 1000))

    def test_exec_cash_pf_includes_reported_funding_on_restart(self):
        pulse = pt.Pulse.__new__(pt.Pulse)
        win = pt.Closed(1795254001, "BTC-USDT", "LONG", 1, 100, 101, .9, .01, "tp", 60,
                        funding_usdt=-.3, exec_lane_id="lane", filled_orders=2, net_pnl_pct=.009)
        loss = pt.Closed(1795254002, "BTC-USDT", "SHORT", 1, 100, 101, -.6, -.01, "sl", 60,
                         funding_usdt=.1, exec_lane_id="lane", filled_orders=2, net_pnl_pct=-.006)
        for row in (win, loss):
            pulse._exec_note(row)
        before = pulse.exec_scope["stage:exec"].copy()
        self.assertAlmostEqual(before["cash_gain"] / before["cash_loss"], 1.2)
        self.assertAlmostEqual(before["return_gain"] / before["return_loss"], 1.5)
        self.assertAlmostEqual(before["reported_funding"], -.2)
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "closed.jsonl"
            ledger.write_text("".join(json.dumps(asdict(row)) + "\n" for row in (win, loss)))
            with patch.object(pt, "TRADES_PATH", str(ledger)), patch.object(pt, "downtime_intervals", return_value=()), \
                    patch.object(pt.time, "time", return_value=1795254090):
                pulse._exec_boot()
        self.assertEqual(pulse.exec_scope["stage:exec"], before)

    def test_flat_probe_preserves_one_way_and_hedge_slots(self):
        pulse = pt.Pulse.__new__(pt.Pulse)
        pos = pt.Position("TEST-USDT", "SHORT", 1, 100, 601, 101, 98, 100)
        for side, qty in (("BOTH", "-1"), ("SHORT", "1"), ("SHORT", "NaN")):
            response = {"code": 0, "data": [{"symbol": pos.symbol, "positionSide": side, "positionAmt": qty}]}
            pulse.api = SimpleNamespace(get=lambda _: response)
            self.assertFalse(pulse._exchange_flat(pos))
        response["data"][0].update(positionSide="LONG", positionAmt="1")
        self.assertTrue(pulse._exchange_flat(pos))

    def test_exec_entry_scan_reuses_pack_side_ranks_across_200_symbols(self):
        pulse = pt.Pulse.__new__(pt.Pulse)
        book = se.SetBook()
        book.stage_grid = sg.StageGrid.from_settings({})
        pulse.sets, pulse.open, pulse.cooldown = book, {}, {}
        symbols = [f"TEST{i}-USDT" for i in range(200)]
        pulse.cooldown[pulse.position_key(symbols[0], "LONG")] = 1000
        signals = [("scoped", 1, .9, "test"), ("general", 1, .8, "test"), ("general", -1, .7, "test")]
        pulse._entry_sig = {symbol: (540, signals) for symbol in symbols}
        with patch.object(pt, "SYMBOLS", symbols), patch.object(book, "entry_signals_pending", return_value=False), \
                patch.object(book, "symbol_allows", side_effect=lambda pack, sym: pack != "scoped" or sym == symbols[-1]), \
                patch.object(pulse, "_scope_choice", return_value=(object(), "pos", "")) as pick:
            candidates = pulse.set_entry_candidates(now=610)
        self.assertEqual(len(candidates), 399)
        self.assertEqual(pick.call_count, 3)
        self.assertIn("set:scoped:", candidates[pulse.position_key(symbols[-1], "LONG")][3])
        self.assertIn("set:general:", candidates[pulse.position_key(symbols[1], "LONG")][3])


if __name__ == "__main__":
    unittest.main()
