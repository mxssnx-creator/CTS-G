"""Live processing: ratios applied once, SL-min floors, min qty, batch completeness."""
from __future__ import annotations

import pathlib
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server" / "pulse"))
import bingx_fast
import pulse_trader as pt


def _pulse(**extra):
    p = pt.Pulse.__new__(pt.Pulse)
    p.volume_factor = 1.0
    p.vol1h = {}
    p.open = {}
    p.px = {"X-USDT": 100.0}
    p.last_px = {"X-USDT": 100.0}
    p.ctrl_skip = {"X-USDT:LONG": 9e12, "sync:X-USDT:LONG": 9e12}
    p.sl_min = 0.002
    p.sl_max = 0.03
    p.tp_min = 0.003
    p.tp_max = 0.03
    p.exits = NS(enabled=False, opt_sl_min=0.001, opt_sl_max=0.009)
    p.contracts = {
        "X-USDT": pt.Contract("X-USDT", 0.1, 0.01, 2, 2, 5.0, 50),
    }
    p.coord = NS(size_mult=lambda n: 1.0)
    p.control_orders_per_config = True
    for name, value in extra.items():
        setattr(p, name, value)
    return p


def _long(sl_pct=0.0015, sl=99.85, tp=100.60, key="X-USDT:LONG"):
    pos = pt.Position(
        "X-USDT", "LONG", 0.05, 100.0, 1.0, sl, tp, 100.0,
        sl_pct=sl_pct, tp_pct=0.006, ours=True, set_id="s1", pack="general",
        client_id="own-1", execution_lane="normal:s1",
        control_group_key=key, control_range_key="sl0015-tp0060",
        control_sl_bp=15, control_tp_bp=60,
        system_id=pt.SYSTEM_ID, connection=pt.CONN_SHORT, tracking_scope=pt.TRACKING_SCOPE,
    )
    return pos


class LiveProcessingTests(unittest.TestCase):
    def test_sized_notional_applies_set_ratio_once(self):
        p = _pulse()
        with patch.object(pt, "TARGET_NOTIONAL", 10.0):
            base = p.sized_notional(ratio=1.0)
            doubled = p.sized_notional(ratio=2.0)
        self.assertAlmostEqual(base, 10.0)
        self.assertAlmostEqual(doubled, 20.0)
        self.assertAlmostEqual(doubled / base, 2.0)

    def test_sized_notional_coord_trim_is_not_compounded_with_set_ratio(self):
        p = _pulse()
        p.coord = NS(size_mult=lambda n: 0.8)
        p.open = {"a": object(), "b": object()}
        with patch.object(pt, "TARGET_NOTIONAL", 10.0):
            value = p.sized_notional(ratio=2.0)
        self.assertAlmostEqual(value, 16.0)

    def test_sl_min_refresh_widens_too_tight_open_stop_and_wakes_controls(self):
        p = _pulse()
        pos = _long()
        p.open[p.position_key(pos)] = pos
        changed = p._refresh_open_risk_floors()
        self.assertGreaterEqual(changed, 1)
        self.assertGreaterEqual(pos.sl_pct, 0.002 - 1e-12)
        self.assertLessEqual(pos.sl, 99.80 + 1e-9)
        self.assertNotIn(p.position_key(pos), p.ctrl_skip)
        self.assertFalse(pos.ctrl_verified)
        self.assertEqual(pos.control_sl_bp, 15)

    def test_sl_min_refresh_keeps_legal_trail_beyond_floor(self):
        p = _pulse()
        p.exits = NS(enabled=True, opt_sl_min=0.001, opt_sl_max=0.009,
                     optimal_sl=lambda *a, **k: 99.40)
        pos = _long(sl_pct=0.006, sl=99.40, tp=101.0)
        p.open["k"] = pos
        p._refresh_open_risk_floors()
        self.assertAlmostEqual(pos.sl, 99.40, places=4)
        self.assertGreaterEqual(pos.sl_pct, 0.002)

    def test_raise_to_min_qty_does_not_inflate_a_lot_already_above_the_floor(self):
        p = _pulse()
        c = p.contracts["X-USDT"]
        c.min_usdt = 2.27
        c.min_qty = 0.01
        qty = p.raise_to_min_qty(c, 100.0, 0.5, "The minimum size per order is 2.27 USDT.")
        self.assertAlmostEqual(qty, 0.5)

    def test_raise_to_min_qty_lifts_a_lot_below_the_learned_usdt_floor(self):
        p = _pulse()
        c = p.contracts["X-USDT"]
        qty = p.raise_to_min_qty(c, 100.0, 0.01, "The minimum size per order is 5 USDT")
        self.assertGreaterEqual(qty * 100.0, 5.0)

    def test_ctrl_body_raises_quantity_to_venue_min(self):
        p = _pulse()
        p.cid = lambda *a, **k: "cid-u"
        pos = _long()
        pos.qty = 0.01
        body = p._ctrl_body(pos, "sl", 99.80)
        self.assertGreaterEqual(float(body["quantity"]), 0.1)
        self.assertGreaterEqual(float(body["quantity"]) * 100.0, 5.0)

    def test_batch_success_marks_complete_without_mutating_retry_intent(self):
        a = bingx_fast.FastBingX.__new__(bingx_fast.FastBingX)
        a.post = lambda path, body: {"code": 0, "data": {"orders": [
            {"code": 0, "orderId": "1", "clientOrderID": "a"},
            {"code": 0, "orderId": "2", "clientOrderID": "b"},
        ]}}
        orders = [{"clientOrderID": "a", "quantity": "0.10"}, {"clientOrderID": "b", "quantity": "0.20"}]
        result = a.batch_place(orders)
        self.assertTrue(result["complete"])
        self.assertEqual(result["pendingIndexes"], [])
        self.assertEqual(orders[0]["quantity"], "0.10")

    def test_batch_cooled_small_batch_is_not_rewritten_as_success(self):
        a = bingx_fast.FastBingX.__new__(bingx_fast.FastBingX)
        a.post = lambda path, body: {"code": 101209, "msg": "cooling", "error": True, "cooled": True}
        result = a.batch_place([{"clientOrderID": "a", "quantity": 1}, {"clientOrderID": "b", "quantity": 1}])
        self.assertTrue(result.get("cooled"))
        self.assertNotEqual(result.get("code"), 0)
        self.assertFalse(result.get("complete", False))

    def test_offline_symbol_from_body_parses_bingx_109418(self):
        msg = "MKR-USDT is offline currently,all validted symbols in api:/openApi/swap/v2/quote/contracts, please verify it"
        self.assertEqual(
            bingx_fast.offline_symbol_from_body({"code": 109418, "msg": msg}, {"symbol": "MKR-USDT"}),
            "MKR-USDT",
        )
        self.assertEqual(bingx_fast.offline_symbol_from_body({"code": 109418, "msg": msg}, {}), "MKR-USDT")
        self.assertEqual(bingx_fast.offline_symbol_from_body({"code": 0, "msg": "ok"}, {"symbol": "BTC-USDT"}), "")

    def test_mark_symbol_offline_drops_intern_and_keeps_opens(self):
        p = _pulse()
        p._offline_symbols = set()
        p.ignore_syms = {}
        p._lev_retry = {}
        p.open = {}
        with patch.object(pt, "SYMBOLS", ["BTC-USDT", "EOS-USDT", "ETH-USDT"]):
            p.mark_symbol_offline("EOS-USDT")
            self.assertIn("EOS-USDT", p._offline_symbols)
            self.assertNotIn("EOS-USDT", pt.SYMBOLS)
            self.assertIn("BTC-USDT", pt.SYMBOLS)

    def test_tradable_contract_names_exclude_offline(self):
        p = _pulse()
        p._offline_symbols = {"MKR-USDT"}
        p.contracts = {"BTC-USDT": object(), "MKR-USDT": object(), "ETH-USDT": object()}
        names = p._tradable_contract_names()
        self.assertEqual(set(names), {"BTC-USDT", "ETH-USDT"})

    def test_set_leverage_skips_offline_and_missing_contracts(self):
        p = _pulse()
        p._offline_symbols = {"EOS-USDT"}
        p._lev_retry = {}
        p.lev_map = {"BTC-USDT": 150}
        p.lev_max = {"BTC-USDT": 150}
        p.contracts = {"BTC-USDT": p.contracts["X-USDT"]}
        p.api = NS(path_cd={})
        p._load_lev_file = lambda: None
        p._drain_offline_hits = lambda: None
        called = []
        p.ensure_max_leverage = lambda s, force=False: called.append(s) or 150
        with patch.object(pt, "SYMBOLS", ["BTC-USDT", "EOS-USDT", "MKR-USDT"]), patch.object(pt, "LEVERAGE", 150):
            p.set_leverage()
        self.assertNotIn("EOS-USDT", called)
        self.assertNotIn("MKR-USDT", called)


if __name__ == "__main__":
    unittest.main()
