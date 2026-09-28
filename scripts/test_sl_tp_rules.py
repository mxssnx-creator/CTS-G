"""Operator risk rules: SL/TP protection always on, SL never above 3x TP."""
import pathlib
import random
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server/pulse'))
import modules
from position_cost import SL_TP_MAX_FACTOR, cap_sl_to_tp, resolve_sl_tp
import test_all_valid_entries as harness


class SlTpRules(unittest.TestCase):
    def test_cap_tightens_stop_and_lifts_tp_only_when_the_floor_binds(self):
        self.assertEqual(SL_TP_MAX_FACTOR, 3.0)
        sl, tp = cap_sl_to_tp(0.012, 0.003, 0.004)
        self.assertAlmostEqual(sl, 0.009)
        self.assertAlmostEqual(tp, 0.003)
        sl, tp = cap_sl_to_tp(0.004, 0.001, 0.004)
        self.assertAlmostEqual(sl, 0.004)
        self.assertAlmostEqual(tp, 0.004 / 3)
        self.assertEqual(cap_sl_to_tp(0.003, 0.005, 0.004), (0.003, 0.005))

    def test_resolved_entry_ranges_never_exceed_three_times_tp(self):
        rng = random.Random(3)
        for _ in range(2000):
            sl_min = rng.choice((0.001, 0.004))
            for bind in (True, False):
                sl, tp, _ = resolve_sl_tp(
                    base_sl=rng.uniform(0, 0.03), base_tp=rng.uniform(0, 0.02),
                    sl_min=sl_min, sl_max=0.03, tp_min=0.001, tp_max=0.05,
                    ind_sl=rng.choice((0, rng.uniform(0, 0.03))), ind_tp=rng.choice((0, rng.uniform(0, 0.02))),
                    sl_to_tp=rng.uniform(0.1, 3.0), bind_sl_to_tp=bind)
                self.assertLessEqual(sl, 3 * tp + 1e-12, (sl, tp, bind))
                self.assertGreaterEqual(sl + 1e-12, sl_min)

    def test_control_prices_keep_the_stop_within_three_times_tp(self):
        h = harness.AllValidEntries(); h.setUp(); self.addCleanup(h.doCleanups)
        p = h.pulse(h.book(1))
        p.sl_max = 0.03
        p.exits.opt_sl_max = 0.03
        p.place('X-USDT', 1, 'trend', .9, selected_set=p.sets.by_idx[0])
        pos = next(iter(p.open.values()))
        pos.sl_pct, pos.tp_pct = 0.02, 0.004
        sl, tp, _, _ = p.opt_fracs(pos)
        self.assertLessEqual(sl, 3 * tp + 1e-12)
        sl_px, tp_px = p.max_range_prices(pos)
        self.assertLessEqual(abs(pos.entry - sl_px), 3 * abs(tp_px - pos.entry) + 1e-9)

    def test_stored_control_orders_off_cannot_disable_protection(self):
        self.assertTrue(modules.resolve({'controlOrders': False})['exec.controls'])
        self.assertTrue(modules.resolve({'modules': {'exec.controls': False}})['exec.controls'])


if __name__ == '__main__':
    unittest.main()
