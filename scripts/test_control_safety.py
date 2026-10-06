"""Control-order safety: stops stay inside liquidation, an unverifiable size
is never reported protected, and aggregate entries merge instead of
overwriting the book."""
import os
import pathlib
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace as NS

os.environ.setdefault("CTS_DATA_DIR", tempfile.mkdtemp(prefix="cts-ctrl-safety-"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
import pulse_trader as pt  # noqa: E402

SYM = "X-USDT"


def pulse(px=100.0):
    p = object.__new__(pt.Pulse)
    p.px = {SYM: px}
    p.last_px = {SYM: px}
    p.contracts = {SYM: pt.Contract(SYM, 0.001, 0.001, 3, 2, 1.0, 150)}
    return p


def position(side="LONG", liq=0.0, qty=1.0):
    pos = pt.Position(symbol=SYM, side=side, qty=qty, entry=100.0, opened_at=time.time() - 60,
                      sl=0.0, tp=0.0, peak=100.0)
    pos.liq = liq
    pos.sl_pct = 0.009
    return pos


class LiquidationClampTests(unittest.TestCase):
    def test_long_stop_moves_inside_liquidation(self):
        p, pos = pulse(), position("LONG", liq=99.3)
        sl = p.clamp_ctrl_price(pos, "sl", 99.1)  # 0.9 % stop sits beyond liq 99.3
        self.assertGreater(sl, 99.3 * 1.001)
        self.assertLess(sl, 100.0)

    def test_short_stop_moves_inside_liquidation(self):
        p, pos = pulse(), position("SHORT", liq=100.7)
        sl = p.clamp_ctrl_price(pos, "sl", 100.9)
        self.assertLess(sl, 100.7 * 0.999)
        self.assertGreater(sl, 100.0)

    def test_stop_inside_liquidation_is_unchanged(self):
        p, pos = pulse(), position("LONG", liq=95.0)
        self.assertAlmostEqual(p.clamp_ctrl_price(pos, "sl", 99.1), 99.1, places=6)


class BannedListSizeTests(unittest.TestCase):
    def test_oversized_pair_is_not_reported_protected_while_order_list_is_banned(self):
        p = pulse()
        pos = position(qty=0.5)
        pos.sl_oid, pos.tp_oid = "111", "222"
        pos.ctrl_qty = 1.0  # old pair still sized for the pre-partial quantity
        p.open = {"k": pos}
        p.ctrl_skip = {}
        p.api = NS(path_cd={"/openApi/swap/v2/trade/openOrders": time.time() + 60})
        p.per_config_controls = lambda pos: True
        p.position_key = lambda pos: "k"
        p.legacy_position_key = lambda pos: "k"
        p.position_is_ours = lambda pos: True
        p.exchange_position_active = lambda pos: True
        p._controls_waiting_for_position = lambda pos: False
        p.desired_sl_tp = lambda pos: (99.0, 101.0, 99.0, 101.0)
        p.place_ctrl = lambda *a, **k: ""
        p.ensure_controls(pos)
        self.assertFalse(pos.controls_ok)
        self.assertEqual(pos.ctrl_qty, 1.0)


class AggregateMergeTests(unittest.TestCase):
    def test_keep_pair_helper_is_off_in_overall_mode(self):
        p = pulse()
        pos = position()
        pos.sl_oid = "1"
        orig = pt.overall_controls.enabled
        try:
            pt.overall_controls.enabled = lambda *_: True
            self.assertFalse(p._keep_pair_until_replaced(pos))
            pt.overall_controls.enabled = lambda *_: False
            self.assertTrue(p._keep_pair_until_replaced(pos))
            pos.sl_oid = ""
            self.assertFalse(p._keep_pair_until_replaced(pos))
        finally:
            pt.overall_controls.enabled = orig


if __name__ == "__main__":
    unittest.main()
