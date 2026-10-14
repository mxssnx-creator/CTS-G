"""Causal stage ledger: windows read only closes before t; seats and gates."""
import os
import sys
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server", "pulse"))

import stage_ledger as SL  # noqa: E402

H = SL.H_MS


def tape(rs, start=0, step=H, side=1):
    exits = [start + (k + 1) * step for k in range(len(rs))]
    return SL.Tape([e - step for e in exits], exits, rs, [side] * len(rs))


class LedgerTests(unittest.TestCase):
    def test_close_at_t_is_not_visible_at_t(self):
        tp = tape([0.01, 0.02])
        t = np.array([H, H + 1, 2 * H, 2 * H + 1], dtype=float)
        self.assertEqual(tp.idx_before(t).tolist(), [0, 1, 1, 2])

    def test_base_needs_trades_profit_pf_and_lcb(self):
        cfg = SL.StageConfig(long_h=1000, min_trades=4, min_pf=1.1)
        t = np.array([100 * H], dtype=float)
        self.assertTrue(SL.base_eval(tape([0.02, -0.01, 0.02, 0.03, 0.01]), t, cfg)["ok"][0])
        self.assertFalse(SL.base_eval(tape([0.02, -0.01, 0.02]), t, cfg)["ok"][0])  # too few
        self.assertFalse(SL.base_eval(tape([0.01, -0.02, 0.01, -0.02, 0.01]), t, cfg)["ok"][0])  # net < 0
        # profitable but too noisy: one big win, many losses → LCB <= 0
        self.assertFalse(SL.base_eval(tape([0.2, -0.03, -0.03, -0.03, -0.03, -0.03]), t, cfg)["ok"][0])

    def test_window_forgets_old_closes(self):
        cfg = SL.StageConfig(long_h=5, min_trades=3, min_pf=1.0)
        tp = tape([0.02] * 5 + [-0.01] * 5)
        res = SL.base_eval(tp, np.array([5.5 * H, 10.5 * H]), cfg)
        self.assertEqual(res["ok"].tolist(), [True, False])

    def test_main_pre_window_and_live_gate(self):
        cfg = SL.StageConfig(pre_h=3, min_pf=1.1, last_n=3, last_n_min_pf=1.0)
        tp = tape([0.02] * 6 + [-0.01, -0.01, -0.01])
        t = np.array([6.5 * H, 9.5 * H])
        self.assertEqual(SL.main_eval(tp, t, cfg, np.array([True, True])).tolist(), [True, False])
        self.assertEqual(SL.live_ok(tp, t, cfg).tolist(), [True, False])
        self.assertTrue(SL.live_ok(tape([-0.01]), np.array([5 * H]), cfg)[0])  # fewer than N: open

    def test_seat_best_cell_and_portfolio(self):
        score = np.full((3, 2, 2), -np.inf)
        score[0, 1, 0], score[0, 0, 0] = 5.0, 1.0
        score[1, 0, 0] = 3.0
        score[2, 1, 0] = 4.0
        score[1, 1, 1] = 2.0
        seated = SL.seat(score, portfolio=2)
        self.assertEqual(seated[:, 0].tolist(), [1, -1, 1])
        self.assertEqual(seated[:, 1].tolist(), [-1, 1, -1])

    def test_forward_by_stage_and_guard(self):
        fw = SL.forward_by_stage(["", "Base", "Main", "Real", "Live"], [-0.01, 0.02, -0.01, 0.03, 0.01])
        self.assertEqual([fw[k]["n"] for k in ("Base", "Main", "Real", "Live")], [4, 3, 2, 1])
        self.assertFalse(SL.lane_guard([-0.01] * 100, n=100)["liveOrders"])
        self.assertTrue(SL.lane_guard([-0.01] * 50, n=100)["liveOrders"])
        self.assertTrue(SL.lane_guard([], live_orders=False, shadow_r=[0.02, -0.01] * 50, n=100)["liveOrders"])

    def test_events(self):
        e = np.array([0, H, 5 * H, 5 * H], dtype=float)
        self.assertEqual(SL.events(e, np.array([1, 1, 1, -1]), 4.0), 3)


if __name__ == "__main__":
    unittest.main()
