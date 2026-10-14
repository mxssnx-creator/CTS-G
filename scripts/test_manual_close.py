"""Manual (outside-the-engine) closes keep the engine processing correctly.

Contract:
- a position closed by hand on the venue is booked as a realized local close
  (account stats) with reason ``manual-close``; it never becomes Set evidence;
- its SL/TP controls are removed from the venue;
- while the absence is being confirmed no controls are re-placed on the flat
  side (that used to trigger a 30 s pause of protection on every symbol);
- the exact lane is held, so the same order/position is not re-opened, while
  other lanes keep entering;
- an engine close that meets an already-flat venue position is the same
  manual close, never an exchange-confirmed fill at an invented price.
"""
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "server/pulse"))
import pulse_trader as pt  # noqa: E402
import test_engine_adversarial as adv  # noqa: E402

SYM = adv.SYM
CTRL = ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT")


class ManualClose(adv.AdvBase):
    def close_by_hand(self, side="LONG", passes=12):
        self.ex.positions[(SYM, side)] = 0.0
        start = len(self.ex.requests)
        for _ in range(passes):
            self.p.adopt_exchange_positions()
            self.tick(seconds=6.0)
        return start

    def placed_controls_since(self, start):
        return [b for m, path, b in self.ex.requests[start:]
                if m != "GET" and str(b.get("type") or "") in CTRL and not b.get("cancelOrderId")]

    def test_full_manual_close_is_booked_and_cleans_controls(self):
        for mode in ("overall", "per-config"):
            with self.subTest(mode=mode):
                p, ex = self.adv(mode)
                self.enter("LONG", 0)
                self.tick()
                trades_before = p.wins + p.losses
                self.close_by_hand()
                self.assertFalse(p.open, "book still holds the manually closed lot")
                rows = [r for r in p.closed if str(r.reason) == "manual-close"]
                self.assertEqual(len(rows), 1, [r.reason for r in p.closed])
                self.assertFalse(rows[0].exchange_confirmed, "manual close must not pose as an exchange fill")
                self.assertEqual(p.wins + p.losses, trades_before + 1, "manual close not counted in account stats")
                self.assertEqual(ex.own_controls("LONG"), [], "own SL/TP survive on the venue")

    def test_no_control_replacement_on_a_flat_side(self):
        for mode in ("overall", "per-config"):
            with self.subTest(mode=mode):
                p, ex = self.adv(mode)
                pos = self.enter("LONG", 0)
                self.enter("SHORT", 1)  # venue stays non-empty: no empty-snapshot guard
                self.tick()
                p.adopt_exchange_positions()
                self.clock.offset += 60.0  # past the 45 s propagation window
                start = self.close_by_hand(passes=3)
                self.assertEqual(float(getattr(pos, "exchange_qty", 0.0) or 0.0), 0.0)
                self.assertEqual(self.placed_controls_since(start), [],
                                 "controls were re-placed on a flat side")
                self.assertLessEqual(float(p.ctrl_skip.get("__stale_position__", 0.0) or 0.0), self.clock.time(),
                                     "a flat side paused protection on every symbol")

    def test_same_lane_is_not_reopened_but_other_lanes_trade(self):
        for mode in ("overall", "per-config"):
            with self.subTest(mode=mode):
                p, ex = self.adv(mode)
                self.enter("LONG", 0)
                self.tick()
                self.close_by_hand()
                self.assertFalse(p.open)
                self.clock.offset += 120.0  # well past every cooldown
                p.place(SYM, 1, "trend", .9, selected_set=p.sets.by_idx[0])
                self.assertFalse(p.open, "the manually closed lane re-opened")
                fresh = self.enter("LONG", 1)  # a different configuration still enters
                self.assertEqual(fresh.set_id, p.sets.by_idx[1].id)

    def test_lane_hold_expires(self):
        p, ex = self.adv("per-config")
        self.enter("LONG", 0)
        self.tick()
        self.close_by_hand()
        self.clock.offset += float(getattr(p, "manual_lane_hold_s", pt.MANUAL_LANE_HOLD_S)) + 60.0
        self.enter("LONG", 0)  # hold over: the lane may trade again
        self.assertEqual(len(p.open), 1)

    def test_hold_disabled_restores_reentry(self):
        p, ex = self.adv("per-config")
        p.manual_lane_hold_s = 0.0
        self.enter("LONG", 0)
        self.tick()
        self.close_by_hand()
        self.clock.offset += 120.0
        self.enter("LONG", 0)
        self.assertEqual(len(p.open), 1)

    def test_engine_close_on_flat_venue_is_a_manual_close(self):
        for mode in ("overall", "per-config"):
            with self.subTest(mode=mode):
                p, ex = self.adv(mode)
                pos = self.enter("LONG", 0)
                self.tick(seconds=5.0)  # still inside the 45 s window: engine thinks it is live
                ex.positions[(SYM, "LONG")] = 0.0
                p.close_pos(pos, ex.mark[SYM], "test-exit")
                self.assertFalse(p.open)
                rows = [r for r in p.closed if str(r.reason) == "manual-close"]
                self.assertEqual(len(rows), 1, [(r.reason, r.exchange_confirmed) for r in p.closed])
                self.assertFalse(rows[0].exchange_confirmed)
                self.assertFalse([r for r in p.closed if r.exchange_confirmed],
                                 "a flat venue was booked as an exchange-confirmed fill")
                self.assertTrue(p.manual_lane_held(SYM, "LONG", pos.execution_lane, pos.set_id, pos.pack))


if __name__ == "__main__":
    unittest.main()
