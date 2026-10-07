"""Minimum-lot sizing and automatic leverage for the allowed SL distance."""
import os
import pathlib
import sys
import tempfile
import unittest
import unittest.mock
from types import SimpleNamespace as NS

os.environ.setdefault("CTS_DATA_DIR", tempfile.mkdtemp(prefix="cts-minlot-"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
import pulse_trader as pt  # noqa: E402
from position_cost import LIQ_SL_SHARE  # noqa: E402


def contract(sym="X-USDT", min_qty=0.001, min_usdt=5.0, step=0.001, max_lev=150):
    return pt.Contract(symbol=sym, min_qty=min_qty, min_usdt=min_usdt, step=step, qprec=3, pprec=2, max_lev=max_lev)


class Api:
    def __init__(self):
        self.posts = []
        self.path_cd = {}

    def post(self, path, body):
        self.posts.append((path, dict(body)))
        return {"code": 0, "data": {}}


def trader(**over):
    p = object.__new__(pt.Pulse)
    p.available = 1000.0
    p.equity = 1000.0
    p.margin_cap_pct = 0.0
    p.sl_max = 0.03
    p.htf_sl_max = 0.0
    p.order_sizing = "minQty"
    p.sl_auto_leverage = True
    p.volume_factor = 1.0
    p.vol1h = {}
    p.lev_map = {}
    p.lev_max = {}
    p.contracts = {}
    p.api = Api()
    p._lev_retry = {}
    p.use_max_leverage = True
    p.symbol_has_foreign_exposure = lambda s: False
    p.fetch_symbol_leverage = lambda s: (150, 150, 150)
    p._drain_offline_hits = lambda: None
    p._persist_lev = lambda: None
    p.ok = lambda r: isinstance(r, dict) and r.get("code") == 0
    p.coord = NS(size_mult=lambda n: 1.0)
    p.open = {}
    for k, v in over.items():
        setattr(p, k, v)
    return p


class MinLotSizingTests(unittest.TestCase):
    def test_min_qty_mode_orders_the_venue_minimum(self):
        p = trader()
        c = contract()
        # min USDT 5 at px 100 -> 0.05; min qty 0.001 -> the larger, on the step.
        self.assertAlmostEqual(p.size_qty(c, 100.0, ratio=3.0), 0.05)
        c2 = contract(min_qty=0.2, min_usdt=5.0)
        self.assertAlmostEqual(p.size_qty(c2, 100.0), 0.2)

    def test_min_qty_rounds_up_to_the_lot_step(self):
        p = trader()
        c = contract(min_qty=0.0, min_usdt=5.0, step=0.01)
        q = p.size_qty(c, 33.0)  # 5/33 = 0.1515 -> 0.16
        self.assertAlmostEqual(q, 0.16)
        self.assertGreaterEqual(q * 33.0, 5.0)

    def test_factor_mode_keeps_the_volume_factor_target(self):
        p = trader(order_sizing="factor", volume_factor=10.0)
        c = contract()
        want = max(0.05, round(pt.TARGET_NOTIONAL * 10.0 / 100.0, 3))
        self.assertAlmostEqual(p.size_qty(c, 100.0), want, places=3)
        p.order_sizing = "minQty"
        self.assertAlmostEqual(p.size_qty(c, 100.0), 0.05)

    def test_no_room_no_order(self):
        p = trader(available=0.0)
        self.assertEqual(p.size_qty(contract(), 100.0), 0.0)


class AutoLeverageTests(unittest.TestCase):
    def test_leverage_follows_the_widest_allowed_sl(self):
        p = trader()
        self.assertEqual(p.leverage_target(150), int(LIQ_SL_SHARE / 0.03))
        self.assertEqual(p.leverage_target(20), 20, "never above the venue max")
        p.htf_sl_max = 0.15
        self.assertEqual(p.leverage_target(150), int(LIQ_SL_SHARE / 0.15))
        p.sl_auto_leverage = False
        self.assertEqual(p.leverage_target(150), 150)

    def test_every_allowed_sl_fires_before_liquidation(self):
        p = trader()
        c = contract()
        p.lev_max["X-USDT"] = 150
        lev = p.leverage_for(c)
        self.assertLessEqual(p.sl_max, LIQ_SL_SHARE / lev + 1e-12)

    def test_allowed_sl_uses_the_applied_leverage_until_lowered(self):
        p = trader()
        c = contract()
        p.lev_max["X-USDT"] = 150
        p.lev_map["X-USDT"] = 150  # still at the old max on the venue
        self.assertAlmostEqual(p.sl_allowed_max(c), LIQ_SL_SHARE / 150)
        p.lev_map["X-USDT"] = p.leverage_target(150)
        self.assertAlmostEqual(p.sl_allowed_max(c), 0.03)

    def test_venue_leverage_is_set_to_the_target_on_both_sides(self):
        p = trader()
        c = contract()
        p.contracts["X-USDT"] = c
        p.lev_max["X-USDT"] = 150
        p.lev_map["X-USDT"] = 150
        got = p.ensure_max_leverage("X-USDT")
        want = p.leverage_target(150)
        self.assertEqual(got, want)
        sides = {b["side"]: b["leverage"] for path, b in p.api.posts if path.endswith("/leverage")}
        self.assertEqual(sides, {"LONG": want, "SHORT": want})
        # Already at target: no further POST.
        p.api.posts.clear()
        p.ensure_max_leverage("X-USDT")
        self.assertEqual([x for x in p.api.posts if x[0].endswith("/leverage")], [])


class MaxLeverageDefaultTests(unittest.TestCase):
    def test_default_runs_every_pair_at_its_venue_max(self):
        p = trader()
        p.sl_auto_leverage = False
        self.assertEqual(p.leverage_target(150), 150)
        self.assertEqual(p.leverage_target(20), 20)

    def test_lane_sl_caps_do_not_follow_leverage_in_max_mode(self):
        p = trader()
        p.sl_auto_leverage = False
        p.htf_sl_max = 0.15
        c = contract()
        p.lev_map["X-USDT"] = 150
        self.assertAlmostEqual(p.sl_allowed_max(c), 0.03, msg="1m lanes keep slMaxPct")
        self.assertAlmostEqual(p.sl_allowed_max(c, "htf"), 0.15, msg="1h lane keeps its preset cap")

    def test_htf_cap_never_widens_the_1m_lanes(self):
        p = trader()
        p.htf_sl_max = 0.15
        self.assertAlmostEqual(p.sl_cap_pct("1m"), 0.03)
        self.assertAlmostEqual(p.sl_cap_pct("htf"), 0.15)

    def test_refused_leverage_change_backs_off_and_keeps_venue_truth(self):
        p = trader()
        p.sl_auto_leverage = True
        c = contract()
        p.contracts["X-USDT"] = c
        p.lev_max["X-USDT"] = 150
        p.lev_map["X-USDT"] = 150
        p.fetch_symbol_leverage = lambda s: (0, 0, 0)
        p.api.post = lambda path, body: {"code": 101400, "msg": "insufficient margin"}
        p.ensure_max_leverage("X-USDT")
        self.assertEqual(p.lev_map["X-USDT"], 150, "lev_map stays the applied leverage")
        self.assertGreater(p._lev_retry.get("X-USDT", 0), 0, "backed off")

    def test_seed_from_contracts_never_overwrites_applied_leverage(self):
        p = trader()
        c = contract(max_lev=125)
        p.contracts = {"X-USDT": c}
        p.lev_map["X-USDT"] = 20
        p.seed_lev_from_contracts()
        self.assertEqual(p.lev_map["X-USDT"], 20)
        self.assertEqual(p.lev_max["X-USDT"], 125)


class RemainderTests(unittest.TestCase):
    def pos(self, qty, **kw):
        base = dict(symbol="X-USDT", side="LONG", qty=qty, entry=100.0, opened_at=0.0, sl=99.0, tp=101.0, peak=100.0)
        base.update(kw)
        return pt.Position(**base)

    def test_sole_lot_remainder_below_minimum_uses_the_whole_position_form(self):
        p = trader()
        p.px = {"X-USDT": 100.0}
        p.contracts = {"X-USDT": contract(min_qty=0.05, min_usdt=0.0)}
        a = self.pos(0.02)
        p.open = {"a": a}
        q, whole = p.control_qty(a)
        self.assertTrue(whole)
        self.assertAlmostEqual(q, 0.05, msg="legal quantity next to closePosition")

    def test_remainder_is_never_raised_over_a_sibling(self):
        p = trader()
        p.px = {"X-USDT": 100.0}
        p.contracts = {"X-USDT": contract(min_qty=0.05, min_usdt=0.0)}
        a, b = self.pos(0.02), self.pos(0.05)
        p.open = {"a": a, "b": b}
        with unittest.mock.patch.object(pt, "log"):
            q, whole = p.control_qty(a)
        self.assertFalse(whole)
        self.assertAlmostEqual(q, 0.02, msg="not raised to 0.05: that would close the sibling's lot")

    def test_full_lot_keeps_the_quantity_form(self):
        p = trader()
        p.px = {"X-USDT": 100.0}
        p.contracts = {"X-USDT": contract(min_qty=0.05, min_usdt=0.0)}
        a = self.pos(0.05)
        p.open = {"a": a}
        self.assertEqual(p.control_qty(a), (0.05, False))


class ProfileTests(unittest.TestCase):
    def test_both_desks_ship_min_lot_and_auto_leverage(self):
        import json
        import connection_profile as cp
        prof = cp.processing_profile()
        self.assertEqual(prof["orderSizing"], "minQty")
        self.assertIs(prof["slAutoLeverage"], False, "always max leverage by default")
        root = pathlib.Path(__file__).resolve().parents[1] / "server/pulse"
        for lane in ("bingx-x01", "bingx-x02"):
            ov = json.loads((root / f"overlay-{lane}.json").read_text())
            self.assertEqual(ov["orderSizing"], "minQty")
            self.assertIs(ov["slAutoLeverage"], False)


if __name__ == "__main__":
    unittest.main()
