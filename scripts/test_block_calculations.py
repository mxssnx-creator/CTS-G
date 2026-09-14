"""Intensive first-principles checks for Block sizing, remainder and PositionCost PF.

These tests pin the live formula independently of the exchange: extra size is
sequential remainder under a 2× parent cap, intern 1.00 is never real 1.10,
and overlay ratio 1.0 with a 3-count stack shares the extra across live rungs.
"""
from __future__ import annotations

import math
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server" / "pulse"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from block_engine import (
    BLOCK_MAX_VOLUME_MULTIPLIER,
    BlockBook,
    BlockLane,
    calculate_block_max_additional_ratio,
    calculate_block_minimum_profit_factor,
    calculate_block_volume_increment_ratio,
    cost_pf_from_net_fracs,
    shared_block_volume_ratio,
)
from position_cost import (
    INTERN_PF,
    POSITION_COST_PCT_DEFAULT,
    POSITIVE_PF,
    cost_as_frac,
    last_n_cost_pf,
    net_pnl_pct,
    ratio_from_r,
    signed_result_r,
)


def hand_target(base: float, n: int, vr: float, cap: float = 2.0) -> float:
    extra = min(max(0.0, cap - 1.0), n * vr)
    return base * extra


class BlockCalculationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def book(self, name="book.json", **cfg):
        return BlockBook(str(pathlib.Path(self.tmp.name) / name), {"defaultMinPF": POSITIVE_PF, **cfg})

    def test_raw_increment_is_count_times_ratio_and_never_sums(self):
        self.assertEqual(calculate_block_volume_increment_ratio(1, 1.0), 1.0)
        self.assertEqual(calculate_block_volume_increment_ratio(3, 1.0), 3.0)
        self.assertEqual(calculate_block_max_additional_ratio(3, 1.0), 1.0)
        self.assertEqual(
            calculate_block_volume_increment_ratio(1, 1)
            + calculate_block_volume_increment_ratio(2, 1)
            + calculate_block_volume_increment_ratio(3, 1),
            6.0,
        )

    def test_share_only_when_first_count_would_eat_the_cap(self):
        self.assertAlmostEqual(shared_block_volume_ratio(1.0, 3, 1.0), 1 / 3)
        self.assertAlmostEqual(shared_block_volume_ratio(2.0, 6, 1.0), 1 / 6)
        self.assertAlmostEqual(shared_block_volume_ratio(0.25, 6, 1.0), 0.25)
        self.assertAlmostEqual(shared_block_volume_ratio(0.8, 6, 1.0), 0.8)
        self.assertAlmostEqual(shared_block_volume_ratio(1.0, 1, 1.0), 1.0)
        self.assertAlmostEqual(shared_block_volume_ratio(1.0, 0, 1.0), 1.0)
        self.assertAlmostEqual(shared_block_volume_ratio(0.1, 6, 0.1), 0.1 / 6)

    def test_default_quarter_ladder_hits_cap_at_n4(self):
        b = self.book(blockMaxStack=6, blockVolumeRatio=0.25)
        self.assertAlmostEqual(b.effective_volume_ratio(), 0.25)
        expected = [0.25, 0.50, 0.75, 1.00, 1.00, 1.00]
        for n, inc in enumerate(expected, 1):
            f = b.formula(10.0, n)
            self.assertAlmostEqual(f["volumeIncrement"], inc)
            self.assertAlmostEqual(f["targetAddQty"], 10.0 * inc)
            self.assertAlmostEqual(f["targetBlockQty"], 10.0 * (1 + inc))
            self.assertLessEqual(f["targetBlockQty"], 20.0 + 1e-12)
        self.assertAlmostEqual(b.step_qty(10.0, 4), 2.5)
        self.assertAlmostEqual(b.step_qty(10.0, 5), 0.0)
        self.assertAlmostEqual(b.step_qty(10.0, 6), 0.0)

    def test_overlay_ratio_one_stack_three_shares_live_counts_only(self):
        b = self.book(blockMaxStack=3, blockVolumeRatio=1.0)
        self.assertEqual(b.volume_ratio, 1.0)
        self.assertAlmostEqual(b.effective_volume_ratio(), 1 / 3)
        self.assertEqual(b.live_counts(), [1, 2, 3])
        steps = [b.step_qty(30.0, n) for n in range(1, 7)]
        self.assertEqual([round(s, 8) for s in steps[:3]], [10.0, 10.0, 10.0])
        self.assertTrue(all(s > 0 for s in steps[:3]))
        self.assertAlmostEqual(sum(steps[:3]), 30.0)
        self.assertAlmostEqual(b.formula(30.0, 3)["targetBlockQty"], 60.0)
        self.assertTrue(all(abs(s) < 1e-12 or n not in b.live_counts() for n, s in enumerate(steps, 1) if n > 3))

    def test_preview_six_is_not_used_as_the_live_share_denominator(self):
        b = self.book(blockMaxStack=3, blockVolumeRatio=1.0)
        self.assertNotAlmostEqual(b.effective_volume_ratio(), 1 / 6)
        self.assertAlmostEqual(shared_block_volume_ratio(1.0, len(b.counts), 1.0), 1 / 6)
        self.assertAlmostEqual(shared_block_volume_ratio(1.0, len(b.live_counts()), 1.0), 1 / 3)

    def test_sequential_remainder_equals_step_and_stops_at_two_x(self):
        b = self.book(blockMaxStack=3, blockVolumeRatio=1.0)
        lane = b.register_parent("SOL-USDT", "LONG", 9.0, 100.0)
        filled = 0.0
        for n in (1, 2, 3):
            pick = b.pick_emit(b.evaluate_counts(lane, live_n=1, intern_pf=1.5))
            self.assertIsNotNone(pick)
            self.assertEqual(int(pick["blockCount"]), n)
            qty = float(pick["requestedAddQty"])
            self.assertAlmostEqual(qty, 3.0)
            b.record_fill(lane, pick, qty, f"c{n}", f"o{n}")
            filled += qty
            self.assertAlmostEqual(lane.confirmed_add, filled)
            self.assertAlmostEqual(lane.base_qty + lane.confirmed_add, 9.0 + filled)
        self.assertAlmostEqual(filled, 9.0)
        self.assertAlmostEqual(lane.base_qty + lane.confirmed_add, 18.0)
        self.assertIsNone(b.next_unsatisfied(lane))
        self.assertIsNone(b.pick_emit(b.evaluate_counts(lane, live_n=1, intern_pf=1.5)))

    def test_full_cap_rung_needs_configured_pf_above_real_floor(self):
        b = self.book(blockMaxStack=3, blockVolumeRatio=1.0, blockProfitFactorRatio=1.1)
        lane = BlockLane("X", "LONG", 9.0, 100.0, confirmed_add=6.0, satisfied={1: True, 2: True})
        f = b.formula(9.0, 3)
        self.assertAlmostEqual(f["volumeIncrement"], 1.0)
        self.assertAlmostEqual(f["blockMinPF"], 1.11)
        self.assertIsNone(b.pick_emit(b.evaluate_counts(lane, 1, intern_pf=POSITIVE_PF)))
        self.assertIsNotNone(b.pick_emit(b.evaluate_counts(lane, 1, intern_pf=1.11)))

    def test_partial_fill_requests_only_the_leftover(self):
        b = self.book(blockCounts=[1], blockVolumeRatio=1.0, blockMaxStack=1)
        lane = b.register_parent("XRP-USDT", "LONG", 8.0, 100.0)
        pick = b.pick_emit(b.evaluate_counts(lane, 1, 2))
        self.assertAlmostEqual(pick["requestedAddQty"], 8.0)
        b.record_fill(lane, pick, 3.0, "a", "1")
        pick = b.pick_emit(b.evaluate_counts(lane, 1, 2))
        self.assertAlmostEqual(pick["requestedAddQty"], 5.0)
        b.record_fill(lane, pick, 5.0, "b", "2")
        self.assertIsNone(b.pick_emit(b.evaluate_counts(lane, 1, 2)))
        self.assertAlmostEqual(lane.base_qty + lane.confirmed_add, 16.0)

    def test_long_and_short_are_independent_books(self):
        b = self.book(blockVolumeRatio=0.25, blockMaxStack=3)
        long = b.register_parent("BCH-USDT", "LONG", 10.0, 100.0)
        short = b.register_parent("BCH-USDT", "SHORT", 4.0, 100.0)
        pick = b.pick_emit(b.evaluate_counts(long, 1, POSITIVE_PF))
        b.record_fill(long, pick, pick["requestedAddQty"], "L", "1")
        self.assertAlmostEqual(short.confirmed_add, 0.0)
        self.assertEqual(b.next_unsatisfied(short), 1)
        self.assertAlmostEqual(b.formula(short.base_qty, 1)["targetAddQty"], 1.0)

    def test_merge_parent_grows_anchor_and_scales_targets(self):
        b = self.book(blockVolumeRatio=0.25, blockMaxStack=3)
        lane = b.register_parent("SOL-USDT", "LONG", 10.0, 100.0)
        b.merge_parent("SOL-USDT", "LONG", 5.0, 130.0)
        self.assertAlmostEqual(lane.base_qty, 15.0)
        self.assertAlmostEqual(lane.confirmed_add, 0.0)
        self.assertAlmostEqual(lane.base_entry, (100.0 * 10 + 130.0 * 5) / 15.0)
        self.assertAlmostEqual(b.formula(lane.base_qty, 1)["targetAddQty"], 3.75)

    def test_held_factor_does_not_change_size(self):
        b = self.book(blockVolumeRatio=0.25, blockMaxStack=3)
        lane = BlockLane("H", "LONG", 10.0, 100.0)
        lane.held_factor = {1: 1.0, 2: 2.0, 3: 3.0}
        lane.pf_ring = {1: [-0.01], 2: [-0.01, -0.01], 3: [-0.01, -0.01, -0.01]}
        for n in (1, 2, 3):
            self.assertAlmostEqual(b.step_qty(10.0, n, lane), 2.5)
            self.assertGreaterEqual(b.vol_factor(n, lane), 1.0)

    def test_min_pf_uses_the_same_increment_as_the_order_target(self):
        b = self.book(blockVolumeRatio=0.25, blockMaxStack=3, blockProfitFactorRatio=1.1)
        for n in range(1, 4):
            f = b.formula(10.0, n)
            want = calculate_block_minimum_profit_factor(POSITIVE_PF, 1.1, f["volumeIncrement"])
            self.assertAlmostEqual(f["blockMinPF"], want)
            d = b.pf_decision(BlockLane("X", "LONG", 10.0, 100.0), n, intern_pf=1.5)
            self.assertAlmostEqual(d["configuredMinimumProfitFactor"], f["blockMinPF"])

    def test_wide_ratio_min_pf_uses_capped_increment_not_raw_ratio(self):
        b = self.book(blockMaxStack=1, blockVolumeRatio=2.0, blockCounts=[1], blockProfitFactorRatio=1.1)
        f = b.formula(10.0, 1)
        self.assertAlmostEqual(f["volumeIncrement"], 1.0)
        self.assertAlmostEqual(f["blockMinPF"], 1.11)
        d = b.pf_decision(BlockLane("W", "LONG", 10.0, 100.0), 1, intern_pf=1.5)
        self.assertAlmostEqual(d["configuredMinimumProfitFactor"], 1.11)

    def test_intern_one_cannot_pass_real_floor(self):
        b = self.book()
        cold = BlockLane("I", "LONG", 10.0, 100.0)
        self.assertFalse(b.pf_decision(cold, 1, intern_pf=INTERN_PF)["passesProfitFactor"])
        self.assertTrue(b.pf_decision(cold, 1, intern_pf=POSITIVE_PF)["passesProfitFactor"])
        self.assertFalse(b.pick_emit(b.evaluate_counts(cold, 1, intern_pf=INTERN_PF)))
        self.assertIsNotNone(b.pick_emit(b.evaluate_counts(cold, 1, intern_pf=POSITIVE_PF)))

    def test_cost_net_one_r_is_real_pf_at_any_position_cost(self):
        for cost in (0.10, 0.15, 0.20):
            net = cost_as_frac(cost)
            self.assertAlmostEqual(cost_pf_from_net_fracs([net] * 8, cost), POSITIVE_PF)
            gross = net + cost_as_frac(cost)
            via_rows = last_n_cost_pf([{"t": i, "pnl_pct": gross} for i in range(8)], 8, cost)
            self.assertAlmostEqual(via_rows["ratio"], POSITIVE_PF, places=4)
            self.assertAlmostEqual(ratio_from_r(signed_result_r(gross, cost)), POSITIVE_PF)

    def test_warm_loss_blocks_and_warm_win_passes_on_position_cost(self):
        b = self.book(positionCostPct=0.10)
        lane = BlockLane("P", "LONG", 10.0, 100.0, confirmed_add=2.5, satisfied={1: True})
        lane.pf_ring[2] = [-0.0045] * 8
        lane.parent_pf_ring = [-0.0045] * 8
        loss = b.pf_decision(lane, 2, intern_pf=1.5)
        self.assertFalse(loss["coldStart"])
        self.assertFalse(loss["passesProfitFactor"])
        lane.pf_ring[2] = [0.001] * 8
        lane.parent_pf_ring = [0.001] * 8
        win = b.pf_decision(lane, 2, intern_pf=INTERN_PF)
        self.assertTrue(win["passesProfitFactor"])
        self.assertAlmostEqual(win["observedProfitFactor"], POSITIVE_PF)

    def test_book_cost_pct_changes_observed_pf(self):
        net = 0.0015  # +1R at 0.15%, +1.5R at 0.10%
        cheap = self.book("cheap.json", positionCostPct=0.10)
        dear = self.book("dear.json", positionCostPct=0.15)
        lane = BlockLane("C", "LONG", 10.0, 100.0)
        lane.pf_ring[1] = [net] * 8
        lane.parent_pf_ring = [net] * 8
        cheap_d = cheap.pf_decision(lane, 1, intern_pf=INTERN_PF)
        dear_d = dear.pf_decision(lane, 1, intern_pf=INTERN_PF)
        self.assertAlmostEqual(dear_d["observedProfitFactor"], POSITIVE_PF)
        self.assertAlmostEqual(cheap_d["observedProfitFactor"], 1.15)
        self.assertNotAlmostEqual(cheap_d["observedProfitFactor"], dear_d["observedProfitFactor"])

    def test_close_stores_cost_net_and_opposite_side_stays_live(self):
        b = self.book(blockVolumeRatio=0.25, blockMaxStack=3)
        long = b.register_parent("SOL-USDT", "LONG", 10.0, 100.0)
        short = b.register_parent("SOL-USDT", "SHORT", 8.0, 100.0)
        pick = b.pick_emit(b.evaluate_counts(long, 1, POSITIVE_PF))
        b.record_fill(long, pick, pick["requestedAddQty"], "c", "o")
        gross = 0.003
        net = net_pnl_pct(gross, 0.15)
        b.on_parent_close("SOL-USDT", "LONG", 1.5, pnl_pct=net)
        self.assertFalse(long.active)
        self.assertEqual(long.base_qty, 0.0)
        self.assertAlmostEqual(long.parent_pf_ring[-1], net)
        self.assertTrue(short.active)
        self.assertEqual(short.base_qty, 8.0)

    def test_pause_and_failed_pf_do_not_gate_other_counts(self):
        b = self.book(blockVolumeRatio=0.1, blockMaxStack=6)
        lane = b.register_parent("SOL-USDT", "LONG", 10.0, 100.0)
        b.pause_count(lane, 1)
        lane.pf_ring[2] = [-1] * 5
        pick = b.pick_emit(b.evaluate_counts(lane, 1, 2.0))
        self.assertEqual(pick["blockCount"], 3)

    def test_active_live_is_a_view_of_the_same_remainder(self):
        b = self.book(blockVolumeRatio=0.25, blockMaxStack=3)
        lane = b.register_parent("SOL-USDT", "LONG", 10.0, 100.0)
        rows = b.evaluate_counts(lane, live_n=3, intern_pf=POSITIVE_PF)
        regular = [r for r in rows if r["kind"] == "regular"]
        active = [r for r in rows if r["kind"] == "active-live"]
        self.assertEqual(len(regular), 6)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["blockCount"], 1)
        self.assertAlmostEqual(active[0]["requestedAddQty"], regular[0]["requestedAddQty"])
        pick = b.pick_emit(rows)
        self.assertEqual(pick["kind"], "regular")

    def test_historic_set_engine_matches_live_book_targets(self):
        from set_engine import SetBook
        s = object.__new__(SetBook)
        s.block_vr = shared_block_volume_ratio(1.0, 3, 1.0)
        s.block_stack, s.block_max_multiplier, s.block_counts = 3, 2.0, [1, 2, 3]
        s._rearm_stops = lambda *args: None
        pos = {"parent": 9.0, "qty": 9.0, "entry": 100.0, "side": 1, "adds": 0}
        live = self.book(blockMaxStack=3, blockVolumeRatio=1.0)
        for n in range(1, 4):
            s._maybe_block_add(pos, [110, 110, 110, 110], 0.01, 0.01)
            self.assertAlmostEqual(pos["qty"], live.formula(9.0, n)["targetBlockQty"])
        self.assertAlmostEqual(pos["qty"], 18.0)

    def test_grid_never_exceeds_cap_across_ratios(self):
        for base in (0.001, 1.0, 3.0, 1000.0):
            for ratio in (0.05, 0.25, 0.5, 1.0, 2.0):
                for cap in (1.1, 1.5, 2.0):
                    b = self.book(
                        f"g-{base}-{ratio}-{cap}.json",
                        blockVolumeRatio=ratio,
                        blockMaxVolumeMultiplier=cap,
                        blockMaxStack=6,
                    )
                    vr = b.effective_volume_ratio()
                    prev = 0.0
                    for n in range(1, 7):
                        f = b.formula(base, n)
                        want = hand_target(base, n, vr, cap)
                        self.assertAlmostEqual(f["targetAddQty"], want)
                        self.assertLessEqual(f["targetBlockQty"], base * cap + 1e-9)
                        self.assertGreaterEqual(f["stepQty"], -1e-15)
                        self.assertAlmostEqual(f["targetAddQty"], prev + f["stepQty"])
                        prev = f["targetAddQty"]

    def test_formula_matches_hand_calc_for_known_examples(self):
        b = self.book(blockVolumeRatio=0.25, blockMaxStack=6, blockProfitFactorRatio=1.1)
        # base=1, n=1: extra 0.25, tot 1.25, minPF = 1 + 0.1*1.1*0.25 = 1.0275
        f = b.formula(1.0, 1)
        self.assertAlmostEqual(f["targetBlockQty"], 1.25)
        self.assertAlmostEqual(f["blockMinPF"], 1.0275)
        # original 3/4-count example: base 3, n=4 hits 2×
        self.assertAlmostEqual(b.formula(3.0, 4)["targetBlockQty"], 6.0)
        self.assertAlmostEqual(b.formula(3.0, 6)["targetBlockQty"], 6.0)


if __name__ == "__main__":
    unittest.main()
