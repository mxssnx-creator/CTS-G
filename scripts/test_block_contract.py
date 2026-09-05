"""No-network regressions for the six-count/base-1 Block sizing contract."""
import math
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server" / "pulse"))
from block_engine import BlockBook, calculate_block_volume_multiplier, normalize_block_counts
from set_engine import SetBook
import pulse_trader as trader


class BlockContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def book(self, **cfg):
        return BlockBook(str(pathlib.Path(self.tmp.name) / "book.json"), {"defaultMinPF": 1.1, **cfg})

    def test_base_one_and_inactive_inputs_are_neutral(self):
        for count, ratio in [(0, 1), (-1, 1), (4, 0), (4, float("nan")), (float("inf"), 1)]:
            self.assertEqual(calculate_block_volume_multiplier(count, ratio), 1)
        self.assertEqual(calculate_block_volume_multiplier(4, .25), 2)

    def test_original_three_four_counts_example(self):
        b = self.book()
        self.assertEqual(b.formula(3, 4)["targetBlockQty"], 6)
        self.assertEqual(b.formula(3, 6)["targetBlockQty"], 6)
        self.assertEqual(b.max_stack, 6)
        self.assertEqual(b.snapshot()["countN"], 6)

    def test_grid_is_additive_and_bounded_even_with_held_counts(self):
        for base in [.001, 1, 3, 1000]:
            for ratio in [.05, .1, .15, .25, .4, .8, 1, 2]:
                for cap in [1, 1.1, 1.5, 2]:
                    b = self.book(blockVolumeRatio=ratio, blockMaxVolumeMultiplier=cap)
                    lane = b.register_parent("XRP-USDT", "LONG", base, 1)
                    lane.held_factor = {n: float(n) for n in range(1, 7)}
                    for count in range(1, 7):
                        with self.subTest(base=base, ratio=ratio, cap=cap, count=count):
                            f = b.formula(base, count, lane)
                            self.assertAlmostEqual(f["targetBlockQty"], base * (1 + min(count * ratio, cap - 1)))
                            self.assertLessEqual(f["targetBlockQty"], base * 2 + 1e-10)
                            self.assertGreaterEqual(f["stepQty"], 0)
                    b.lanes.clear()  # Each grid cell has an independent parent.

    def test_partial_fills_only_request_remaining_target(self):
        b = self.book(blockCounts=[4])
        lane = b.register_parent("BCH-USDT", "LONG", 3, 100)
        row = b.pick_emit(b.evaluate_counts(lane, 1, 2))
        self.assertEqual(row["requestedAddQty"], 3)
        b.record_fill(lane, row, 1, "a", "1")
        row = b.pick_emit(b.evaluate_counts(lane, 1, 2))
        self.assertEqual(row["requestedAddQty"], 2)
        b.record_fill(lane, row, 2, "a", "1")
        self.assertIsNone(b.pick_emit(b.evaluate_counts(lane, 1, 2)))
        self.assertEqual(lane.base_qty + lane.confirmed_add, 6)

    def test_paused_or_failing_counts_do_not_block_other_counts(self):
        b = self.book(blockVolumeRatio=.1)
        lane = b.register_parent("SOL-USDT", "LONG", 3, 100)
        b.pause_count(lane, 1)
        lane.pf_ring[2] = [-1] * 5
        rows = b.evaluate_counts(lane, 1, 2)
        self.assertEqual(len([r for r in rows if r["kind"] == "regular"]), 6)
        self.assertEqual(b.pick_emit(rows)["blockCount"], 3)
        self.assertEqual(len([r for r in rows if r["kind"] == "active-live"]), 1)

    def test_counts_and_symbols_do_not_double_count_or_share_results(self):
        b = self.book()
        lane = b.register_parent("SOL-USDT", "SHORT", 3, 100)
        lane.pf_ring[3] = [-1]
        b.count_tape[3] = [100, 100, 100]
        self.assertEqual(b.last_n_avg(3, lane), (-1, 1))
        self.assertEqual(b.last_n_avg(2, lane), (0, 0))
        lane.held_factor[3] = 3
        lane.parent_pf_ring = [100] * 25
        self.assertEqual(b.vol_factor(3, lane), 3)

    def test_loss_holds_then_own_positive_settlement_pauses_across_restart(self):
        cfg = {"blockCounts": [3], "blockVolumeRatio": .1}
        b = self.book(**cfg)
        lane = b.register_parent("SOL-USDT", "LONG", 3, 100)
        row = b.pick_emit(b.evaluate_counts(lane, 1, 2))
        b.record_fill(lane, row, row["requestedAddQty"], "a", "1")
        b.on_parent_close("SOL-USDT", "LONG", -1)
        self.assertEqual(lane.held_factor[3], 3)
        self.assertEqual(lane.pause_remaining.get(3, 0), 0)
        restarted = self.book(**cfg)
        lane = restarted.register_parent("SOL-USDT", "LONG", 3, 100)
        self.assertEqual(restarted.vol_factor(3, lane), 3)
        row = restarted.pick_emit(restarted.evaluate_counts(lane, 1, 2))
        self.assertAlmostEqual(row["requestedAddQty"], .9)
        restarted.record_fill(lane, row, row["requestedAddQty"], "b", "2")
        restarted.on_parent_close("SOL-USDT", "LONG", 1)
        self.assertEqual(lane.held_factor[3], 1)
        self.assertEqual(lane.pause_remaining[3], 1)
        self.assertEqual(lane.pause_remaining.get(2, 0), 0)

    def test_empty_count_selection_disables_all_orders_not_evaluations(self):
        b = self.book(blockCounts=[])
        lane = b.register_parent("XRP-USDT", "LONG", 3, 1)
        rows = b.evaluate_counts(lane, 1, 2)
        self.assertEqual(len(rows), 6)
        self.assertIsNone(b.pick_emit(rows))
        self.assertEqual(normalize_block_counts([True, None, 1, 2.5, "3", 6, 6, math.nan]), [1, 6])

    def test_historic_and_live_use_same_absolute_target(self):
        s = object.__new__(SetBook)
        s.block_vr, s.block_stack, s.block_max_multiplier, s.block_counts = .25, 6, 2, list(range(1, 7))
        s._rearm_stops = lambda *args: None
        p = {"parent": 3, "qty": 3, "entry": 100, "side": 1, "adds": 0}
        b = self.book()
        for n in range(1, 7):
            s._maybe_block_add(p, [110, 110, 110, 110], .01, .01)
            self.assertAlmostEqual(p["qty"], b.formula(3, n)["targetBlockQty"])

    def test_control_repair_stops_immediately_when_parent_is_retired(self):
        for phase in ["pair", "banned", "normal"]:
            p = trader.Pulse.__new__(trader.Pulse)
            pos = trader.Position("SOL-USDT", "LONG", 1, 100, 1, 99, 101, 100)
            p.open = {"SOL-USDT": pos}
            p.ctrl_skip = {}
            p.px = {"SOL-USDT": 100}
            p.api = SimpleNamespace(path_cd={"/openApi/swap/v2/trade/openOrders": float("inf")} if phase == "banned" else {})
            p.per_config_controls = lambda pos: False
            p.desired_sl_tp = lambda pos: (99, 101, 99, 101)
            p.list_orders = lambda symbol: []
            calls = []
            def pair(pos):
                if phase == "pair":
                    calls.append("pair")
                    p.open.clear()
            def single(pos, kind, price):
                calls.append(kind)
                p.open.clear()
                return ""
            p.place_ctrl_pair = pair
            p.place_ctrl = single
            p.ensure_controls(pos)
            self.assertEqual(calls, ["pair"] if phase == "pair" else ["sec-sl"])

    def test_first_cycle_reconciles_before_control_repairs(self):
        p = trader.Pulse.__new__(trader.Pulse)
        p.halt_reason = None
        p.halted = False
        p.cycle = 0
        events = []
        p._budget = lambda: None
        p.refresh_tickers = lambda: None
        p.seed_px_bars = lambda: None
        p.sync_own_fills = lambda: events.append("fills")
        p.adopt_exchange_positions = lambda: events.append("reconcile")
        p.priority_controls = lambda: events.append("controls") or 0
        p.manage = lambda: None
        p.maybe_entries = lambda: events.append("entry-evaluation")
        with patch.multiple(trader, STOP_PATH=self.tmp.name + "/STOP", PAUSE_PATH=self.tmp.name + "/PAUSE", STOP_ALL=self.tmp.name + "/ALL"):
            p._one_cycle()
        self.assertEqual(events, ["fills", "reconcile", "controls", "entry-evaluation"])


if __name__ == "__main__":
    unittest.main()
