"""Actual Axis tapes: causal gates, identity, native exits and own return PF."""
import pathlib
import sys
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server/pulse"))
from axis_grid import _axis_accept, _gate_masks, axis_ranges, calculate_axes, replay_parent_closes
from coord_engine import AXIS_SPECS
from indication_adjustment_grid import capture_parent_events
from position_cost import clears_pf, last_n_cost_pf
from set_engine import SetBook, SetState, trim_hist

NOW = 1_800_000_000.0


def book():
    b = SetBook()
    b.pf_n = b.min_samples = b.main_eval = b.real_eval = 5
    b.min_pf = 1.10
    b.stage_min_pf = {s: 1.10 for s in ("base", "main", "real")}
    b.cooldown_bars = 0
    b.sl_min = b.tp_min = 0.001
    b.hist_time_bars = 8
    b.hist_honor_tp = True
    b.scratch_min = 0.2
    return b


def parent(ident="P", config="tactic:trend:12:1m", *, trailing=False):
    return SetState(id=ident, pack="indications", tf="1m", sl_ratio=1.0,
        trail_key="test" if trailing else "", trail_arm=0.003 if trailing else 0,
        trail_give=0.001 if trailing else 0, kind="trail" if trailing else "base",
        tp_pct=0.005, step=5, indication_kind="tactic", indication_config=config,
        indication_symbol="X", template_set_id="template:" + ident)


def bars(count=60, sign=1):
    out = []
    for i in range(count):
        px = 100.0 * (1.006 if sign > 0 else 0.994) ** i
        out.append([px, px * 1.0001, px * 0.9999, px, 100.0])
    return out


def scored(b, p, tape, lane, now=NOW):
    p.hist = capture_parent_events(b, "X", tape, p, lane, 0, now)
    b._score_pair((p, None))
    return p


class AxisWindows(unittest.TestCase):
    def test_admission_never_rounds_a_below_floor_parent_or_axis_into_a_pass(self):
        b=book(); b.cost_pct=.1
        start=NOW-20*60
        # PF1.09996 displays1.1000, but remains below the1.10 admission floor.
        close_move=.001*(1+(1.09996-1)/.1)
        rows=[dict(t=start+i*60,side="LONG",symbol="X",pnl_pct=close_move) for i in range(5)]
        lane=[(1,.9,"cfg") for i in range(20)]
        mask=_gate_masks(b,rows,[("last",1)],lane,0,start,1)[("last",1)]
        self.assertFalse(mask.any())
        # The parent mean is strong, while the latest axis-only observation
        # lies below its own floor; neither gate can borrow rounded evidence.
        rows=[dict(row,pnl_pct=.004) for row in rows]
        rows[-1]["pnl_pct"]=close_move
        mask=_gate_masks(b,rows,[("last",1)],lane,0,start,1)[("last",1)]
        self.assertFalse(mask[5:].any())

    def test_grid_matches_canonical_specs_and_optional_bounds(self):
        b = book()
        expected = [(a, n) for a, spec in AXIS_SPECS.items()
                    for n in range(spec["min"], spec["max"] + 1, spec["step"])]
        self.assertEqual(axis_ranges(b), expected)
        b.axis_settings = {"prev": {"enabled": False}, "last": {"maxWindow": 2}}
        self.assertFalse(any(a == "prev" for a, _ in axis_ranges(b)))
        self.assertEqual([n for a, n in axis_ranges(b) if a == "last"], [1, 2])

    def test_previous_excludes_latest_window_and_pause_is_cost_net(self):
        rows = [{"t": i, "pnl_pct": 0.004, "side": "LONG", "symbol": "X"} for i in range(4)]
        rows += [{"t": 4 + i, "pnl_pct": 0.0005, "side": "LONG", "symbol": "X"} for i in range(4)]
        self.assertTrue(_axis_accept("prev", 4, rows, 0.1, 1.1))
        self.assertFalse(_axis_accept("last", 4, rows, 0.1, 1.1))
        self.assertFalse(_axis_accept("pause", 4, rows, 0.1, 0.9), "small gross gains are net losses")
        self.assertFalse(_axis_accept("prev", 4, rows[:7], 0.1, 1.1))

    def test_vector_masks_match_closed_only_native_windows_and_ddt(self):
        b = book()
        b.pf_n, b.ddt_n = 9, 12
        start = NOW - 70 * 60
        # Profitable and losing regimes exercise both native PF and DDT
        # rejection. Equal-time closes never count for that decision.
        moves = [0.006] * 12 + [-0.007] * 4 + [0.009] * 12 + [-0.0004] * 5 + [0.008] * 8
        rows = [dict(t=start + (2 * i + 1) * 60, side="LONG", symbol="X",
                     pnl_pct=value) for i, value in enumerate(moves)]
        lane = [(1 if i % 3 else -1, 0.9, "cfg") for i in range(90)]
        for cost, max_dd in ((0.1, 300), (0.1, 7200), (0.0, 300)):
            b.cost_pct, b.max_dd_s = cost, max_dd
            masks = _gate_masks(b, rows, axis_ranges(b), lane, 4, start, 1)
            for i, signal in enumerate(lane):
                closed = [r for r in rows if r["t"] < start + i * 60]
                base = (len(closed) >= b.eval_need()
                        and clears_pf(last_n_cost_pf(closed, b.pf_n, cost, ordered=True, simple=True)["ratio"], b.min_pf)
                        and b.ddt_when_enough(closed, b.eval_need(), ordered=True)["maxS"] <= max_dd)
                for axis, count in axis_ranges(b):
                    expected = (i >= 4 and signal[0] == 1 and base
                                and _axis_accept(axis, count, closed, cost, b.min_pf))
                    self.assertEqual(bool(masks[(axis, count)][i]), expected,
                                     (cost, max_dd, i, axis, count))


class OwnAxisTapes(unittest.TestCase):
    def test_terminal_open_capture_preserves_identity_protection_and_closed_pf(self):
        b, p = book(), parent(trailing=True)
        p.tp_pct = .04
        b.hist_honor_tp, b.hist_time_bars = False, 100
        count, start = 60, NOW - 59 * 60
        tape = []
        for i in range(count):
            px = 100 * (1.00006 ** i)
            tape.append([px, px * 1.00005, px * .99995, px, 100])
        lane = [(0, 0., "")] + [(1, .9, "cfg")] * (count-1)
        captures = {p.id: [dict(t=start-i*60, side="LONG", symbol="X", pnl_pct=.008)
                            for i in range(36, 0, -1)]}
        b._bar_end_ts["X"] = NOW
        children = calculate_axes(b, "X", tape, [p], lane, 0, NOW, capture=True,
                                  parent_captures=captures)
        self.assertEqual(len(children), 2 * len(axis_ranges(b)))
        self.assertEqual(len(children.open), len(axis_ranges(b)))
        self.assertEqual(len({r["set_id"] for r in children.open}), len(children.open))
        by_id = {s.id: s for s in children}
        with b.sl_scope("X"):
            sl, tp = b.pair_sl_tp(p.tp_pct, p.sl_ratio)
        for row in children.open:
            child = by_id[row["set_id"]]
            self.assertEqual(row["parent_set_id"], p.id)
            self.assertEqual(row["ind_config"], p.indication_config)
            self.assertEqual(row["axis_key"], child.axis_key)
            self.assertEqual(row["volume_ratio"], child.volume_ratio)
            self.assertEqual((row["side"], row["reason"], row["_entry"], row["_exit"]),
                             ("LONG", "open", 1, count-1))
            self.assertEqual(row["entry_ts"], start + 60)
            self.assertEqual(row["t"], NOW)
            self.assertEqual(row["_qty"], 1.)
            self.assertEqual(row["_entry_px"], tape[1][3])
            self.assertEqual(row["_exit_px"], tape[-1][3])
            self.assertAlmostEqual(row["stop_loss"], tape[1][3] * (1-sl))
            self.assertAlmostEqual(row["take_profit"], tape[1][3] * (1+tp))
            self.assertAlmostEqual(row["peak"], tape[-1][1])
            self.assertAlmostEqual(row["trail_stop"], tape[-1][1] * (1-p.trail_give))
            self.assertTrue(row["open_position"])
            self.assertFalse(row["confirmed_close"])
            self.assertEqual(child.n, 0)
            self.assertFalse(child.hist)
            self.assertFalse(child.active, "an unrealized positive mark cannot pass closed PF")
        bounded = calculate_axes(b, "X", tape, [p], lane, 0, NOW, parent_captures=captures)
        self.assertFalse(bounded.open)
        self.assertTrue(all(not s.hist and s.n == 0 for s in bounded))

    def test_last_bar_new_entries_are_open_marks_and_rejected_masks_stay_empty(self):
        b, p, tape = book(), parent(), bars()
        start = NOW - (len(tape)-1)*60
        lane = [(1, .9, "cfg")] * len(tape)
        captures = {p.id: [dict(t=start-i*60, side="LONG", symbol="X", pnl_pct=.008)
                            for i in range(36, 0, -1)]}
        children = calculate_axes(b, "X", tape, [p], lane, 0, NOW, capture=True,
                                  parent_captures=captures)
        self.assertTrue(children.open)
        for row in children.open:
            self.assertEqual(row["_entry"], len(tape)-1)
            self.assertEqual((row["hold_s"], row["pnl_pct"]), (0., 0.))
            self.assertEqual(row["side"], "LONG")
            child = next(s for s in children if s.id == row["set_id"])
            self.assertEqual(child.n, len(child.hist))
            self.assertTrue(all(r["reason"] != "open" for r in child.hist))
        cold = calculate_axes(b, "X", tape, [p], lane, 0, NOW, capture=True,
                              parent_captures={p.id: []})
        self.assertFalse(cold.open)
        self.assertTrue(all(not s.hist for s in cold))

    def test_cached_captures_are_used_and_funding_losses_cannot_bootstrap(self):
        b, p, tape = book(), parent(), bars()
        lane = [(1, 0.9, "cfg")] * len(tape)
        positive = replay_parent_closes(b, "X", tape, p, lane, 0, NOW)
        scored(b, p, tape, lane)
        with patch("indication_adjustment_grid.capture_parent_events", side_effect=AssertionError("recapture")):
            good = calculate_axes(b, "X", tape, [p], lane, 0, NOW, capture=True,
                                  parent_captures={p.id: positive})
            # The audit's settled funding adjustments are authoritative;
            # raw positive price candles cannot replace supplied evidence.
            losses = [dict(row, pnl_pct=-0.008) for row in positive]
            bad = calculate_axes(b, "X", tape, [p], lane, 0, NOW, capture=True,
                                 parent_captures={p.id: losses})
        self.assertTrue(any(child.hist for child in good))
        self.assertTrue(all(not child.hist for child in bad))

    def test_grouped_children_match_scalar_price_replay_for_each_entry_mask(self):
        import math
        start, count = NOW - 99 * 60, 100
        tape = []
        for i in range(count):
            px = 100 * math.exp(.0003 * i + .008 * math.sin(i * .47))
            tape.append([px, px * 1.003, px * .997, px, 100])
        lane = [(1 if i % 17 < 9 else -1, .9, "cfg") for i in range(count)]
        # This exact cached history is known at its timestamps and permits
        # both directions; each child's own closes still use actual OHLC.
        evidence = [dict(t=start + i * 60, pnl_pct=.008, side=side, symbol="X")
                    for i in range(-35, 85, 2) for side in ("LONG", "SHORT")]
        for trailing, honor_tp, cooldown in ((False, True, 0), (True, True, 2), (True, False, 1)):
            b, p = book(), parent(trailing=trailing)
            b.hist_honor_tp, b.cooldown_bars = honor_tp, cooldown
            b.hist_time_bars, b.scratch_s, b.scratch_min = 17, 8 * 60, .0008
            b._bar_end_ts["X"] = NOW
            exits = {i: ("mean-revert", tape[i][3] * 1.001) for i in range(0, count, 11)}
            children = calculate_axes(b, "X", tape, [p], lane, 3, NOW, capture=True,
                                      lane_exits=exits, parent_captures={p.id: evidence})
            masks = {side: _gate_masks(b, [r for r in evidence if r["side"] == side],
                                      axis_ranges(b), lane, 3, start, direction)
                     for side, direction in (("LONG", 1), ("SHORT", -1))}
            for child in children:
                side = child.stage_ledger["parentDirection"]
                axis, number = child.axis_key.split(":")
                mask = masks[side][(axis, int(number))]
                filtered = [s if mask[i] else (0, 0., "") for i, s in enumerate(lane)]
                expected = replay_parent_closes(b, "X", tape, p, filtered, 3, NOW,
                                                lane_exits=exits, side=side)
                actual = list(child.hist)
                self.assertEqual([(r["t"], r["hold_s"], r["reason"]) for r in actual],
                                 [(r["t"], r["hold_s"], r["reason"]) for r in expected],
                                 (trailing, honor_tp, cooldown, child.axis_key, side))
                for got, want in zip(actual, expected):
                    self.assertAlmostEqual(got["pnl_pct"], want["pnl_pct"], places=12)

    def test_every_count_has_exact_parent_direction_and_own_price_tape(self):
        b, p, tape = book(), parent(), bars()
        lane = [(1, 0.9, "cfg")] * len(tape)
        scored(b, p, tape, lane)
        children = calculate_axes(b, "X", tape, [p], lane, 0, NOW, capture=True)
        self.assertEqual(len(children), len(axis_ranges(b)) * 2)
        self.assertEqual(len({s.id for s in children}), len(children))
        longs = [s for s in children if s.stage_ledger["parentDirection"] == "LONG"]
        self.assertTrue(all(s.hist and s.kind == "axis" for s in longs))
        for state in longs:
            self.assertEqual(state.parent_set_id, p.id)
            self.assertEqual(state.template_set_id, p.template_set_id)
            self.assertEqual(state.indication_config, p.indication_config)
            self.assertEqual(state.indication_symbol, "X")
            self.assertEqual(state.volume_ratio, state.relative_count * 0.01)
            self.assertLess(len(state.hist), len(p.hist), "unknown future parent closes cannot bootstrap a child")
            self.assertTrue(all(r["set_id"] == state.id and r["parent_set_id"] == p.id for r in state.hist))
            self.assertTrue(all(r["strategy"] == "axis" and r["axis_key"] == state.axis_key for r in state.hist))
            own = last_n_cost_pf(state.hist, b.pf_n, b.cost_pct, ordered=True)
            self.assertAlmostEqual(state.base_pf, own["ratio"], places=5)
        shorts = [s for s in children if s.stage_ledger["parentDirection"] == "SHORT"]
        self.assertTrue(all(not s.hist and not s.active for s in shorts))

    def test_axis_waits_for_actual_parent_base_closes(self):
        b, p, tape = book(), parent(), bars()
        lane = [(1, 0.9, "cfg")] * len(tape)
        scored(b, p, tape, lane)
        states = calculate_axes(b, "X", tape, [p], lane, 0, NOW, capture=True)
        last = next(s for s in states if s.axis_key == "last:1" and s.hist)
        prev = next(s for s in states if s.axis_key == "prev:4" and s.hist)
        start = NOW - (len(tape) - 1) * 60
        self.assertEqual(last.hist[0]["t"], start + 7 * 60)  # five earlier closes, next entry, then own close
        self.assertEqual(prev.hist[0]["t"], start + 10 * 60)  # eight earlier closes, next entry, then close
        self.assertEqual(last.hist[0]["hold_s"], 60.0)
        entry_time = last.hist[0]["t"] - last.hist[0]["hold_s"]
        self.assertEqual(sum(r["t"] < entry_time for r in p.hist), 5)
        self.assertEqual(sum(r["t"] <= entry_time for r in p.hist), 6,
                         "the equal-timestamp close must not help qualify this entry")

    def test_distinct_parent_configs_never_share_identity_or_metrics(self):
        b, tape = book(), bars()
        lane = [(1, 0.9, "cfg")] * len(tape)
        a, c = parent("A", "tactic:trend:12:1m"), parent("C", "tactic:trend:20:5m")
        c.tp_pct = 0.001  # each gross win only covers cost, despite classic PF99
        for p in (a, c):
            scored(b, p, tape, lane)
        with self.assertRaisesRegex(ValueError, "one indication kind/configuration"):
            calculate_axes(b, "X", tape, [a, c], lane, 0, NOW)
        states = (calculate_axes(b, "X", tape, [a], lane, 0, NOW)
                  + calculate_axes(b, "X", tape, [c], lane, 0, NOW))
        self.assertTrue(any(s.hist for s in states if s.parent_set_id == "A"))
        self.assertTrue(all(not s.hist for s in states if s.parent_set_id == "C"))
        self.assertFalse({s.id for s in states if s.parent_set_id == "A"} &
                         {s.id for s in states if s.parent_set_id == "C"})

    def test_native_scalar_closes_match_columnar_parent_capture(self):
        b = book()
        tape = bars()
        lane = [(1, 0.9, "cfg")] * len(tape)
        for trailing in (False, True):
            p = parent(trailing=trailing)
            if trailing:
                b.hist_honor_tp = False
                # A completed high arms protection; the next low crosses it.
                for i in range(1, len(tape), 3):
                    tape[i][1] *= 1.004
                    if i + 1 < len(tape):
                        tape[i + 1][2] *= 0.99
            expected = capture_parent_events(b, "X", tape, p, lane, 0, NOW)
            actual = replay_parent_closes(b, "X", tape, p, lane, 0, NOW)
            self.assertEqual([(r["t"], r["reason"]) for r in actual],
                             [(r["t"], r["reason"]) for r in expected])
            for got, want in zip(actual, expected):
                self.assertAlmostEqual(got["pnl_pct"], want["pnl_pct"], places=12)

    def test_native_general_keeps_058_confidence_floor(self):
        b, tape = book(), bars()
        lane = [(1, 0.55, "cfg")] * len(tape)
        independent = parent("independent")
        self.assertTrue(replay_parent_closes(b, "X", tape, independent, lane, 0, NOW))
        for kind, pack in (("general", "indications"), ("combined", "indications"),
                           ("tactic", "general")):
            native = parent("native-" + kind + "-" + pack)
            native.indication_kind, native.pack = kind, pack
            self.assertEqual(replay_parent_closes(b, "X", tape, native, lane, 0, NOW), [],
                             (kind, pack, "native pack cannot use the independent-type .52 floor"))
            qualified = [(1, 0.60, "cfg")] * len(tape)
            self.assertTrue(replay_parent_closes(b, "X", tape, native, qualified, 0, NOW))

    def test_future_change_does_not_rewrite_past_child_closes(self):
        b, p = book(), parent()
        early = bars(count=40)
        lane = [(1, 0.9, "cfg")] * len(early)
        old_end = NOW - 20 * 60
        scored(b, p, early, lane, old_end)
        old = calculate_axes(b, "X", early, [p], lane, 0, old_end, capture=True)
        full = bars(count=60)
        # Unseen future regime crashes, leaving parent direction rejected now.
        for i in range(40, 60):
            px = full[39][3] * 0.98 ** (i - 39)
            full[i] = [px, px * 1.001, px * 0.999, px, 100]
        extended_lane = [(1, 0.9, "cfg")] * len(full)
        scored(b, p, full, extended_lane)
        later = calculate_axes(b, "X", full, [p], extended_lane, 0, NOW, capture=True)
        by = {s.id: s for s in later}
        for child in old:
            past = [r for r in by[child.id].hist if r["t"] <= old_end]
            self.assertEqual(past, child.hist)

    def test_retention_keeps_total_count_and_parent_gates_use_full_capture(self):
        b, p, tape = book(), parent(), bars(count=160)
        lane = [(1, 0.9, "cfg")] * len(tape)
        scored(b, p, tape, lane)
        p.hist = p.hist[-96:]  # runtime retained tape must not erase early closures
        states = calculate_axes(b, "X", tape, [p], lane, 0, NOW)
        last = next(s for s in states if s.axis_key == "last:1" and s.hist)
        self.assertEqual(len(last.hist), 96)
        self.assertEqual(last.n, 153)

    def test_compaction_preserves_own_axis_identity_volume_and_return_pf(self):
        b, p, tape = book(), parent(), bars(count=120)
        lane = [(1, 0.9, "cfg")] * len(tape)
        scored(b, p, tape, lane)
        states = calculate_axes(b, "X", tape, [p], lane, 0, NOW, capture=True)
        state = next(s for s in states if s.axis_key == "last:3" and s.hist)
        before = last_n_cost_pf(state.hist, b.pf_n, b.cost_pct, ordered=True)
        compact = trim_hist(state.hist, 32)
        self.assertEqual(len(compact), 32)
        for row in compact:
            self.assertEqual(row["strategy"], "axis")
            self.assertEqual(row["set_id"], state.id)
            self.assertEqual(row["parent_set_id"], p.id)
            self.assertEqual(row["axis_key"], "last:3")
            self.assertEqual(row["relative_count"], 3)
            self.assertEqual(row["volume_ratio"], 0.03)
            self.assertEqual(row["ind_config"], p.indication_config)
        after = last_n_cost_pf(compact, b.pf_n, b.cost_pct, ordered=True)
        self.assertEqual(after["ratio"], before["ratio"])
        self.assertEqual(after["netAvg"], before["netAvg"])


if __name__ == "__main__":
    unittest.main()
