"""Offline strategy-switch contract; no process start, credentials or venue calls."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "pulse"))
sys.path.insert(0, str(ROOT / "scripts" / "regression"))
from common import synth_bars
from exit_engine import ExitBook
from modules import resolve
from risk_variants import VariantBook
from set_engine import SetBook, EXTRA_PACKS
import pulse_trader as pt
import set_engine as se


SMALL = {"setSlRatios": [1], "setMinStep": 3, "setStepMax": 3,
         "trailVariants": ["0.6:0.36"], "histLookbackAuto": False,
         "holdAuto": False, "setEntryConf": .3, "exitIgnoreTp": True}


class StrategySwitches(unittest.TestCase):
    def test_normal_entries_off_preserves_internal_sets_and_additional_types(self):
        book = SetBook(); book.load({**SMALL, "stratNormal": False, "stratTrend": True,
                                    "stagesEnabled": False, "setGateMinTrades": 0})
        for st in book.by_idx:
            st.gate_n, st.gate_pf, st.max_dd_s = 50, 3, 0
            self.assertEqual(book.base_validation_reason(st), "")
            self.assertEqual(book.gate_reason(st), "type-disabled" if st.kind == "base" else "")
        self.assertEqual({st.kind for st in book.by_idx}, {"base", "exit", "trail"})
        self.assertIn("trend", book.packs)

    def test_either_trailing_switch_disables_all_new_trailing_sets(self):
        for flag, module in ((False, True), (True, False), (False, False), (True, True)):
            with self.subTest(flag=flag, module=module):
                ov = {**SMALL, "stratTrailing": flag, "modules": {"strategy.trailing": module}}
                book = SetBook(); book.load(ov)
                self.assertEqual(any(st.kind == "trail" for st in book.by_idx), flag and module)
                self.assertEqual(resolve(ov)["strategy.trailing"], flag and module)
                self.assertTrue(any(st.kind == "base" for st in book.by_idx))

    def test_all_packs_disabled_does_not_silently_enable_indications(self):
        ov = {**SMALL, "stratGeneral": False, "stratIndications": False, "stratShortRange": False,
              **{switch: False for switch, _, _ in EXTRA_PACKS.values()}}
        book = SetBook(); book.load(ov)
        self.assertEqual(book.packs, [])
        self.assertEqual(book.by_idx, [])

    def test_general_off_does_not_change_additional_strategy_tapes(self):
        ov = {**SMALL, "stratIndications": False, "stratTrend": True, "stratBreakout": True,
              "stratShortRange": True, "stratTrailing": False, "timeStopS": 600}
        bars = synth_bars(82, 600, drift=.001, vol=.004)
        outputs = []
        for general in (True, False):
            book = SetBook(); book.load({**ov, "stratGeneral": general})
            book.bars["FIXTURE"] = bars
            book.bar_t["FIXTURE"] = 1_700_000_000 + 599 * 60
            rows = {st.id: [] for st in book.by_idx}
            # Controlled opportunities isolate processing from signal rarity.
            with patch.object(se, "pack_signals", side_effect=lambda bars, packs, *a, **kw:
                              {pack: [(1, .9, "fixture")] * len(bars) for pack in packs}):
                book._replay_symbol("FIXTURE", rows, book.bar_t["FIXTURE"] + 60)
            outputs.append({sid: tape for sid, tape in rows.items() if not sid.startswith("general:")})
        self.assertEqual(outputs[0], outputs[1])
        for pack in ("trend", "breakout", "shortrange"):
            self.assertTrue(any(tape for sid, tape in outputs[1].items() if sid.startswith(pack + ":")), pack)

    def test_trailing_off_retires_identity_without_erasing_owned_history(self):
        book = SetBook(); book.load(SMALL)
        st = next(st for st in book.by_idx if st.kind == "trail")
        st.live = [{"pnl_pct": -.01, "net_pnl_pct": -.012}]
        book.load({**SMALL, "stratTrailing": False})
        self.assertNotIn(st.id, book.sets)
        self.assertIs(book.retired[st.id], st)
        self.assertEqual(len(st.live), 1)

    def test_trailing_off_stops_adjustments_but_keeps_existing_stop(self):
        for side, peak, stop, px in (("LONG", 102, 100.8, 101), ("SHORT", 98, 99.2, 99)):
            with self.subTest(side=side):
                pulse = pt.Pulse.__new__(pt.Pulse)
                pulse.strat_trail = False
                pulse.sets = SimpleNamespace(sets={}, hist_time_bars=100, scratch_bars=10, scratch_min=.001)
                pulse.lane_honors_tp = lambda _p: False
                pulse.software_exit = lambda p, kind, level, price: price <= level if p.side == "LONG" else price >= level
                moved, closed = [], []
                pulse.replace_sl = lambda p, level: moved.append(level) or True
                pulse.close_pos = lambda p, price, why: closed.append(why)
                pos = pt.Position("FIXTURE", side, 1, 100, 1000, 98 if side == "LONG" else 102, 110, peak,
                                  set_id="general:1m:tr0.6:0.36", trail_set_id="general:1m:tr0.6:0.36",
                                  trail_arm=.003, trail_give=.002, sl_pct=.02, base_entry=100, trail=stop)
                pulse.manage_set_lane(pos, px, 1010, 10, .01)
                self.assertEqual(moved, [])
                self.assertEqual(pos.trail, stop)
                pulse.manage_set_lane(pos, stop, 1011, 11, 0)
                self.assertEqual(closed, ["sl"])

    def test_trailing_off_disables_peak_following_and_auto_recalculation(self):
        for ov in ({"stratTrailing": False}, {"modules": {"strategy.trailing": False}}):
            ex = ExitBook(); ex.load(ov)
            variants = VariantBook(); variants.load(ov)
            self.assertFalse(ex.peak_on)
            self.assertTrue(ex.lock_on, "one-time protection is not peak-following")
            self.assertFalse(variants.trail_auto)
            self.assertFalse(resolve(ov)["strategy.trailRecalc"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
