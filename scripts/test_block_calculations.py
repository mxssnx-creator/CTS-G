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
    clamp_eval_pos_count,
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
        lane.pf_ring[2] = [-0.0045] * 50
        lane.parent_pf_ring = [-0.0045] * 50
        loss = b.pf_decision(lane, 2, intern_pf=1.5)
        self.assertFalse(loss["coldStart"])
        self.assertFalse(loss["passesProfitFactor"])
        self.assertTrue(loss["internOnly"])
        lane.pf_ring[2] = [0.001] * 50
        lane.parent_pf_ring = [0.001] * 50
        win = b.pf_decision(lane, 2, intern_pf=INTERN_PF)
        self.assertTrue(win["passesProfitFactor"])
        self.assertFalse(win["internOnly"])
        self.assertAlmostEqual(win["observedProfitFactor"], POSITIVE_PF)

    def test_book_cost_pct_changes_observed_pf(self):
        net = 0.0015  # +1R at 0.15%, +1.5R at 0.10%
        cheap = self.book("cheap.json", positionCostPct=0.10)
        dear = self.book("dear.json", positionCostPct=0.15)
        lane = BlockLane("C", "LONG", 10.0, 100.0)
        lane.pf_ring[1] = [net] * 50
        lane.parent_pf_ring = [net] * 50
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
        lane.pf_ring[2] = [-1] * 50
        pick = b.pick_emit(b.evaluate_counts(lane, 1, 2.0))
        self.assertEqual(pick["blockCount"], 3)

    def test_main_eval_insufficient_is_valid_and_losers_are_intern_only(self):
        self.assertEqual(clamp_eval_pos_count(2), 5)
        self.assertEqual(clamp_eval_pos_count(99), 75)
        b = self.book(blockEvalPosCount=50, blockMaxStack=6, blockVolumeRatio=0.25)
        self.assertEqual(b.eval_pos_count, 50)
        cold = BlockLane("C", "LONG", 10.0, 100.0)
        ev = b.main_stage_eval(cold, 1)
        self.assertTrue(ev["liveOk"])
        self.assertTrue(ev["insufficientSample"])
        self.assertFalse(b.pf_decision(cold, 1, intern_pf=1.0)["passesProfitFactor"])
        self.assertTrue(b.pf_decision(cold, 1, intern_pf=POSITIVE_PF)["passesProfitFactor"])
        lane = BlockLane("L", "LONG", 10.0, 100.0)
        lane.pf_ring[1] = [-0.01] * 50
        lane.pf_ring[2] = [0.004] * 50
        lane.pf_ring[3] = [0.004] * 12
        lose = b.main_stage_eval(lane, 1)
        win = b.main_stage_eval(lane, 2)
        short = b.main_stage_eval(lane, 3)
        self.assertTrue(lose["internOnly"])
        self.assertFalse(lose["liveOk"])
        self.assertTrue(win["liveOk"])
        self.assertTrue(short["liveOk"])
        self.assertTrue(short["insufficientSample"])
        rows = b.evaluate_counts(lane, live_n=1, intern_pf=1.5)
        emit = {int(r["blockCount"]) for r in rows if r.get("kind") == "regular" and float(r.get("requestedAddQty") or 0) > 0}
        self.assertNotIn(1, emit)
        self.assertIn(2, emit)

    def test_set_engine_block_main_is_independent_per_count_and_kind(self):
        from set_engine import SetBook, hist_fill
        book = SetBook()
        book.block_eval_pos = 50
        book.real_min_pf = POSITIVE_PF
        losers = []
        winners = []
        for i in range(50):
            lose = hist_fill(1000 + i, "XRP-USDT", 1, -0.02, 60, "block:tp", ind_kind="signals")
            lose.update(strategy="block", block_count=2, set_id="indications:sl0.6:st8")
            losers.append(lose)
            win = hist_fill(2000 + i, "XRP-USDT", 1, 0.02, 60, "block:tp", ind_kind="trend")
            win.update(strategy="block", block_count=2, set_id="indications:sl0.6:st8")
            winners.append(win)
        short = hist_fill(3000, "XRP-USDT", 1, -0.02, 60, "block:tp", ind_kind="active")
        short.update(strategy="block", block_count=3, set_id="indications:sl0.6:st8")
        book.strategy_hist = {"block": losers + winners + [short]}
        book.score_block_main()
        self.assertFalse(book.block_main_live_ok(2, indication="signals"))
        self.assertTrue(book.block_main_live_ok(2, indication="trend"))
        self.assertTrue(book.block_main_live_ok(3, indication="active"))
        self.assertTrue(book.block_main_live_ok(4))

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

    def test_overall_real_pf_does_not_double_subtract_cost_from_net_ring(self):
        import time as time_mod
        from types import SimpleNamespace
        import pulse_trader as pt
        from position_cost import POSITIVE_PF, last_n_cost_pf, net_pnl_pct

        p = object.__new__(pt.Pulse)
        p.position_cost_pct = 0.10
        p.coord = SimpleNamespace(min_pf=1.10, real_eval=3, stage_min_pf={"real": 1.10})
        p.block = self.book("overall-real.json", blockMaxStack=3, blockVolumeRatio=0.25)
        lane = p.block.register_parent("SOL-USDT", "LONG", 10.0, 100.0)
        gross = 0.003  # +2R at 0.10% cost → PF 1.20
        net = net_pnl_pct(gross, 0.10)
        now = time_mod.time()
        p.closed = [
            SimpleNamespace(
                symbol="SOL-USDT", side="LONG", pnl=gross, pnl_pct=gross, t=now - i,
                ours=True, member_count=1, exchange_confirmed=True, client_id=f"c{i}",
            )
            for i in range(6)
        ]
        lane.parent_pf_ring = [net] * 6
        ratio = p.block_overall_real_pf("SOL-USDT", "LONG")
        want = last_n_cost_pf(p.closed, 3, 0.10, ordered=True)["ratio"]
        self.assertGreater(ratio, 0.0)
        self.assertAlmostEqual(ratio, want, places=4)
        self.assertGreaterEqual(ratio + 1e-9, POSITIVE_PF)
        mixed = last_n_cost_pf(
            list(p.closed) + [SimpleNamespace(pnl_pct=net, pnl=net, t=0.0) for _ in range(6)],
            3, 0.10, ordered=True,
        )["ratio"]
        self.assertNotAlmostEqual(ratio, mixed, places=4)
        self.assertEqual(p.block_overall_real_n("SOL-USDT", "LONG"), 6)

    def test_overall_real_n_matches_ring_when_closes_are_below_need(self):
        import time as time_mod
        from types import SimpleNamespace
        import pulse_trader as pt
        from position_cost import net_pnl_pct

        p = object.__new__(pt.Pulse)
        p.position_cost_pct = 0.10
        p.coord = SimpleNamespace(min_pf=1.10, real_eval=3, stage_min_pf={"real": 1.10})
        p.block = self.book("overall-n-mismatch.json")
        lane = p.block.register_parent("BCH-USDT", "LONG", 5.0, 100.0)
        now = time_mod.time()
        p.closed = [
            SimpleNamespace(
                symbol="BCH-USDT", side="LONG", pnl=0.003, pnl_pct=0.003, t=now - i,
                ours=True, member_count=1,
            )
            for i in range(2)
        ]
        lane.parent_pf_ring = [net_pnl_pct(0.003, 0.10)] * 8
        self.assertGreaterEqual(p.block_overall_real_pf("BCH-USDT", "LONG") + 1e-9, 1.10)
        self.assertEqual(p.block_overall_real_n("BCH-USDT", "LONG"), 8)
        self.assertEqual(p._block_overall_real_state("BCH-USDT", "LONG")["source"], "ring")

    def test_overall_real_last_n_is_chronological_not_insertion_order(self):
        from types import SimpleNamespace
        import pulse_trader as pt

        p = object.__new__(pt.Pulse)
        p.position_cost_pct = 0.10
        p.coord = SimpleNamespace(min_pf=1.10, real_eval=3, stage_min_pf={"real": 1.10})
        p.block = self.book("overall-chrono.json")
        p.block.register_parent("XRP-USDT", "SHORT", 4.0, 1.0)
        # Newest-first insertion: three recent losses, then older wins. Last-N Real
        # must use the newest three (losses), not the trailing insertion wins.
        p.closed = [
            SimpleNamespace(symbol="XRP-USDT", side="SHORT", pnl=-0.003, pnl_pct=-0.003, t=30.0 + i,
                            ours=True, member_count=1)
            for i in range(3)
        ] + [
            SimpleNamespace(symbol="XRP-USDT", side="SHORT", pnl=0.003, pnl_pct=0.003, t=10.0 + i,
                            ours=True, member_count=1)
            for i in range(3)
        ]
        self.assertEqual(p.block_overall_real_pf("XRP-USDT", "SHORT"), 0.0)
        self.assertEqual(p.block_overall_real_n("XRP-USDT", "SHORT"), 6)

    def test_snapshot_intern_uses_overall_real_not_the_floor(self):
        from types import SimpleNamespace
        import pulse_trader as pt
        from position_cost import INTERN_PF, net_pnl_pct

        p = object.__new__(pt.Pulse)
        p.position_cost_pct = 0.10
        p.block_overall = True
        p.coord = SimpleNamespace(min_pf=1.10, real_eval=3, stage_min_pf={"real": 1.10})
        p.block = self.book("overall-snap.json", blockMaxStack=3, blockVolumeRatio=0.25)
        p.closed = []
        p.open = {}
        lane = p.block.register_parent("SOL-USDT", "LONG", 10.0, 100.0)
        lane.parent_pf_ring = [0.0] * 3  # intern 1.00
        snap = p.block.snapshot(intern_pf_lookup=p._block_snapshot_intern_pf)
        row = next(c for c in snap["lanes"][0]["counts"] if c["n"] == 1)
        self.assertFalse(row["pass"])
        self.assertEqual(row["internPf"], 0.0)
        lane.parent_pf_ring = [net_pnl_pct(0.003, 0.10)] * 3
        snap_ok = p.block.snapshot(intern_pf_lookup=p._block_snapshot_intern_pf)
        row_ok = next(c for c in snap_ok["lanes"][0]["counts"] if c["n"] == 1)
        self.assertTrue(row_ok["pass"])
        self.assertGreater(INTERN_PF, 0.0)

    def test_overall_real_pf_ring_fallback_when_closes_are_missing(self):
        from types import SimpleNamespace
        import pulse_trader as pt
        from position_cost import net_pnl_pct

        p = object.__new__(pt.Pulse)
        p.position_cost_pct = 0.10
        p.coord = SimpleNamespace(min_pf=1.10, real_eval=3, stage_min_pf={"real": 1.10})
        p.block = self.book("overall-ring.json")
        lane = p.block.register_parent("XRP-USDT", "LONG", 4.0, 1.0)
        p.closed = []
        self.assertEqual(p.block_overall_real_pf("XRP-USDT", "LONG"), 0.0)
        lane.parent_pf_ring = [net_pnl_pct(0.003, 0.10)] * 3
        self.assertGreaterEqual(p.block_overall_real_pf("XRP-USDT", "LONG") + 1e-9, 1.10)
        self.assertEqual(p.block_overall_real_n("XRP-USDT", "LONG"), 3)
        lane.parent_pf_ring = [0.0] * 3  # intern 1.00 after cost — not extra size
        self.assertEqual(p.block_overall_real_pf("XRP-USDT", "LONG"), 0.0)

    def test_overall_real_includes_merged_member_closes_in_last_n(self):
        """Physical parent last-N includes merged lots. Dropping them fakes Real."""
        from types import SimpleNamespace
        import pulse_trader as pt

        p = object.__new__(pt.Pulse)
        p.position_cost_pct = 0.10
        p.coord = SimpleNamespace(min_pf=1.10, real_eval=3, stage_min_pf={"real": 1.10})
        p.block = self.book("overall-merged.json")
        p.block.register_parent("SOL-USDT", "LONG", 8.0, 100.0)
        # Three older single-lot wins, then three recent merged losses.
        p.closed = [
            SimpleNamespace(symbol="SOL-USDT", side="LONG", pnl=0.003, pnl_pct=0.003, t=10.0 + i,
                            ours=True, member_count=1)
            for i in range(3)
        ] + [
            SimpleNamespace(symbol="SOL-USDT", side="LONG", pnl=-0.003, pnl_pct=-0.003, t=40.0 + i,
                            ours=True, member_count=2)
            for i in range(3)
        ]
        self.assertEqual(p.block_overall_real_pf("SOL-USDT", "LONG"), 0.0)
        self.assertEqual(p.block_overall_real_n("SOL-USDT", "LONG"), 6)
        self.assertEqual(len(p.overall_side_closes("SOL-USDT", "LONG")), 6)

    def test_overall_real_timestamp_last_n_does_not_need_pre_sorted_list(self):
        from types import SimpleNamespace
        import pulse_trader as pt

        p = object.__new__(pt.Pulse)
        p.position_cost_pct = 0.10
        p.coord = SimpleNamespace(min_pf=1.10, real_eval=3, stage_min_pf={"real": 1.10})
        p.block = self.book("overall-ts.json")
        p.block.register_parent("BCH-USDT", "SHORT", 4.0, 1.0)
        # Unsorted: newest losses first, older wins after. Last-N Real = losses.
        p.closed = [
            SimpleNamespace(symbol="BCH-USDT", side="SHORT", pnl=-0.003, pnl_pct=-0.003, t=50.0,
                            ours=True, member_count=1),
            SimpleNamespace(symbol="BCH-USDT", side="SHORT", pnl=0.003, pnl_pct=0.003, t=10.0,
                            ours=True, member_count=1),
            SimpleNamespace(symbol="BCH-USDT", side="SHORT", pnl=-0.003, pnl_pct=-0.003, t=60.0,
                            ours=True, member_count=1),
            SimpleNamespace(symbol="BCH-USDT", side="SHORT", pnl=0.003, pnl_pct=0.003, t=11.0,
                            ours=True, member_count=1),
            SimpleNamespace(symbol="BCH-USDT", side="SHORT", pnl=-0.003, pnl_pct=-0.003, t=70.0,
                            ours=True, member_count=1),
            SimpleNamespace(symbol="BCH-USDT", side="SHORT", pnl=0.003, pnl_pct=0.003, t=12.0,
                            ours=True, member_count=1),
        ]
        self.assertEqual(p.block_overall_real_pf("BCH-USDT", "SHORT"), 0.0)


if __name__ == "__main__":
    unittest.main()
