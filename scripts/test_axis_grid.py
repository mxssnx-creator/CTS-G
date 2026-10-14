"""Actual Axis tapes: causal gates, identity, native exits and own return PF."""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server/pulse"))
from axis_grid import _axis_accept, axis_ranges, calculate_axes, replay_parent_closes
from coord_engine import AXIS_SPECS
from indication_adjustment_grid import capture_parent_events
from position_cost import last_n_cost_pf
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


class OwnAxisTapes(unittest.TestCase):
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
