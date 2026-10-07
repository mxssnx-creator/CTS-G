"""Unit tests for the account math of scripts/sim_12h_account.py (synthetic tapes)."""
import os
import sys
import tempfile
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("CTS_DATA_DIR", tempfile.mkdtemp(prefix="cts-sim-test-"))

import sim_12h_account as sim  # noqa: E402

sim._engine_path()
from position_cost import last_n_cost_pf  # noqa: E402


class AccountMathTest(unittest.TestCase):
    def test_margin_fees_and_pnl(self):
        acct = sim.Account(10.0, 0.1)  # 0.1% round trip
        acct.open_lot(1, "AAA-USDT", 1, 0.001, 2000.0, 100)  # 2 USDT notional
        self.assertAlmostEqual(acct.used_margin, 0.02)
        self.assertAlmostEqual(acct.fees, 0.001)  # half of 0.1% of 2 USDT
        self.assertAlmostEqual(acct.cash, 9.999)
        # mark to market
        self.assertAlmostEqual(acct.unrealized({"AAA-USDT": 2020.0}), 0.02)
        self.assertAlmostEqual(acct.equity({"AAA-USDT": 2020.0}), 10.019)
        self.assertAlmostEqual(acct.worst_unrealized({"AAA-USDT": 2030.0}, {"AAA-USDT": 1990.0}), -0.01)
        lot = acct.close_lot(1, 2020.0)
        self.assertAlmostEqual(lot["gross"], 0.02)
        self.assertAlmostEqual(lot["net"], 0.02 - 0.002)
        self.assertAlmostEqual(acct.cash, 10.018)
        self.assertEqual(acct.used_margin, 0.0)
        self.assertEqual(acct.groups(), {})

    def test_short_and_dca_add(self):
        acct = sim.Account(10.0, 0.1)
        acct.open_lot(7, "BBB-USDT", -1, 1.0, 2.0, 50)
        acct.add_fill(7, 1.0, 2.2, 50)  # adverse add, average 2.1
        self.assertAlmostEqual(acct.lots[7]["entry"], 2.1)
        self.assertAlmostEqual(acct.used_margin, (2.0 + 2.2) / 50)
        self.assertAlmostEqual(acct.unrealized({"BBB-USDT": 2.0}), 0.2)
        lot = acct.close_lot(7, 2.0)
        self.assertAlmostEqual(lot["gross"], 0.2)
        self.assertAlmostEqual(lot["net"], 0.2 - 4.2 * 0.001)

    def test_liquidation_forfeits_maintenance(self):
        acct = sim.Account(1.0, 0.1)
        acct.open_lot(1, "AAA-USDT", 1, 0.1, 100.0, 100)   # 10 USDT notional, 0.1 margin
        acct.open_lot(2, "AAA-USDT", -1, 0.05, 100.0, 100)  # 5 USDT notional short
        mmr = {"AAA-USDT": 0.005}
        self.assertAlmostEqual(acct.maintenance(mmr), 0.075)
        closed = acct.liquidate({"AAA-USDT": 101.0}, {"AAA-USDT": 90.0}, mmr)
        self.assertEqual(len(closed), 2)
        self.assertAlmostEqual(closed[0]["exit"], 90.0)   # long at the low
        self.assertAlmostEqual(closed[1]["exit"], 101.0)  # short at the high
        self.assertEqual(acct.lots, {})
        self.assertEqual(acct.used_margin, 0.0)
        self.assertEqual(acct.cash, 0.0)  # 1 - 1.0 - 0.05 - fees - maintenance -> floored at zero
        acct2 = sim.Account(1.0, 0.1)
        acct2.open_lot(1, "AAA-USDT", 1, 0.1, 100.0, 100)
        acct2.liquidate({"AAA-USDT": 100.0}, {"AAA-USDT": 99.5}, mmr)
        self.assertAlmostEqual(acct2.cash, 1.0 - 0.05 - 0.01 - 0.05)  # loss, fees, forfeited maintenance

    def test_drawdown_percent_and_underwater(self):
        dd = sim.DrawdownTracker(10.0)
        dd.update(0, 10.5)
        d, w = dd.update(1, 9.45, 9.0)
        self.assertAlmostEqual(d, 10.0)  # (10.5 - 9.45) / 10.5
        self.assertAlmostEqual(w, 1.5 / 10.5 * 100)
        dd.update(2, 10.0)
        dd.update(3, 10.6)
        self.assertAlmostEqual(dd.max_dd_pct, 10.0)
        self.assertEqual(dd.max_under_min, 2)
        self.assertAlmostEqual(dd.peak, 10.6)

    def test_profit_factors_match_engine(self):
        moves = [0.004, -0.002, 0.0015, -0.003, 0.006, 0.0005]
        rows = [{"t": i, "pnl_pct": m} for i, m in enumerate(moves)]
        eng = last_n_cost_pf(rows, len(rows), 0.1, ordered=True, simple=True)
        self.assertAlmostEqual(sim.cost_pf_ratio(moves, 0.1), eng["ratio"])
        nets = [m - 0.001 for m in moves]
        self.assertAlmostEqual(round(sim.classic_pf(nets), 4), eng["classicPf"])
        self.assertEqual(sim.classic_pf([0.1]), 99.0)
        self.assertEqual(sim.classic_pf([]), 0.0)
        self.assertTrue(sim.clears(1.02, 1.02))
        self.assertFalse(sim.clears(1.0199, 1.02))

    def test_walk_forward_evidence_is_strictly_before_entry(self):
        rng = np.random.default_rng(3)
        moves = rng.normal(0.0012, 0.004, 40)
        exits = np.arange(100, 140)
        group = np.zeros(40, dtype=np.int64)
        ev = sim.Evidence(group, exits, moves, 0.1)
        idx, gs = ev.lookup(np.array([0, 0, 0, 1]), np.array([130, 131, 100, 130]))
        self.assertEqual(idx[0], 29)   # closes at bars 100..129 (30) are visible at bar 130
        self.assertEqual(idx[1], 30)
        self.assertEqual(idx[2], -1)   # nothing closed before bar 100
        self.assertEqual(idx[3], -1)   # unknown group
        r, ok = ev.ratio(idx, gs, 30)
        self.assertTrue(ok[0])
        self.assertFalse(ok[2])
        rows = [{"t": int(t), "pnl_pct": float(m)} for t, m in zip(exits[:30], moves[:30])]
        self.assertAlmostEqual(r[0], last_n_cost_pf(rows, 30, 0.1, ordered=True, simple=True)["ratio"])
        r5, ok5 = ev.ratio(idx, gs, 5)
        rows5 = [{"t": int(t), "pnl_pct": float(m)} for t, m in zip(exits[26:31], moves[26:31])]
        self.assertAlmostEqual(r5[1], last_n_cost_pf(rows5, 5, 0.1, ordered=True, simple=True)["ratio"])

    def test_vectorized_ddt_matches_engine(self):
        from set_engine import drawdown_time_by_symbol
        rng = np.random.default_rng(11)
        n = 400
        t = np.sort(rng.integers(0, 20000, n)).astype(float) * 60
        sym = rng.integers(0, 4, n)
        mv = rng.normal(0.0008, 0.004, n)
        rows = [{"t": float(a), "symbol": f"S{b}", "pnl_pct": float(c)} for a, b, c in zip(t, sym, mv)]
        eng = drawdown_time_by_symbol(rows, ordered=True)["maxS"]
        self.assertAlmostEqual(sim.ddt_max_s(t, sym, mv - 0.001), eng)
        one = drawdown_time_by_symbol(rows[:50], ordered=True)["maxS"]
        self.assertAlmostEqual(sim.ddt_max_s(t[:50], sym[:50], mv[:50] - 0.001), one)

    def test_control_orders_overall(self):
        c = sim.ControlOrders()
        g = ("AAA-USDT", 1)
        c.step({}, {g: 2}, {g})        # group occupied: SL+TP placed
        c.step({g: 2}, {g: 3}, {g})    # membership change: cancel-replace both legs
        c.step({g: 3}, {g: 3}, set())  # nothing changed
        c.step({g: 3}, {}, {g})        # emptied: cancel both
        self.assertEqual(c.as_dict(), {"controlPlace": 2, "controlCancelReplace": 2, "controlCancel": 2})


class SyntheticTapeSimulationTest(unittest.TestCase):
    def test_orders_and_equity_on_synthetic_tape(self):
        import pulse_trader as pt
        sym = "AAA-USDT"
        contracts = {sym: pt.Contract(sym, 0.001, 0.001, 3, 2, 2.0, 100)}
        n = 120
        close = [2000.0] * n
        high = [2001.0] * n
        low = [1999.0] * n
        # entry at bar 61 close (2000), exits at bar 63 at +1% (TP) and bar 70 at -0.5% (SL)
        bars = {"close": [close], "high": [high], "low": [low], "ind_mask": [np.zeros(n, dtype=np.uint16)]}
        catalog = [dict(uid=0, pack="general", kind="base", mult=2), dict(uid=1, pack="general", kind="trail", mult=1)]
        cands = dict(sym=np.array([0, 0], np.int16), uid=np.array([0, 1], np.int32), side=np.array([1, -1], np.int8),
                     entry=np.array([61, 61], np.int32), exit=np.array([63, 70], np.int32),
                     raw=np.array([0.01, -0.005]), reason=np.array([1, 0], np.uint8), r30=np.array([1.05, np.nan]),
                     admitted=np.array([True, False]),
                     axes={a: np.array([True, False]) for a in sim.AXIS_WINDOWS})

        class Book:
            cost_pct = 0.1

        ov = {"volumeFactor": 1.0, "targetNotional": 2.15, "posCountsVolumeRatio": 0.05,
              "orderSizing": "factor", "slAutoLeverage": False}
        res = sim.simulate("t", cands, [], None, catalog, [sym], bars, 60, 120, 0, Book(), sim.Sizer(contracts, ov, None),
                           10.0, True, True)
        tot = res["totals"]
        # admitted Set (multiplicity 2) -> two independent lots; the second Set is not admitted
        self.assertEqual(tot["orders"]["entry"], 2)
        self.assertEqual(tot["orders"]["closeFills"], {"tp": 2})
        self.assertEqual(tot["orders"]["controlPlace"], 2)
        self.assertEqual(tot["orders"]["controlCancel"], 2)
        self.assertEqual(tot["orders"]["controlCancelReplace"], 0)
        self.assertEqual(tot["positions"]["lotsOpened"], 2)
        self.assertEqual(tot["positions"]["groupsOpened"], 1)
        lot_qty = 0.002  # 2.15 USDT target (x size_mult 1.0 / 0.95) / 2000, rounded up to the 0.001 step
        notional = lot_qty * 2000.0
        expect = 10.0 + 2 * (notional * 0.01 - notional * 0.001)
        self.assertAlmostEqual(tot["equityEnd"], expect, places=9)
        self.assertAlmostEqual(tot["marginMax"], 2 * notional / 100, places=9)
        self.assertEqual(tot["closed"]["wins"], 2)
        self.assertAlmostEqual(tot["byStrategy"]["axis:last"]["n"], 2)
        # unfiltered admits the second Set too
        res_u = sim.simulate("u", cands, [], None, catalog, [sym], bars, 60, 120, 0, Book(),
                             sim.Sizer(contracts, ov, None), 10.0, False, False)
        self.assertEqual(res_u["totals"]["orders"]["entry"], 3)
        self.assertEqual(res_u["totals"]["orders"]["closeFills"], {"tp": 2, "sl": 1})
        # LONG and SHORT groups each get their own protection pair
        self.assertEqual(res_u["totals"]["orders"]["controlPlace"], 4)
        self.assertEqual(res_u["totals"]["orders"]["controlCancel"], 4)
        self.assertEqual(res_u["totals"]["orders"]["controlCancelReplace"], 0)

    def test_margin_skip(self):
        import pulse_trader as pt
        sym = "AAA-USDT"
        contracts = {sym: pt.Contract(sym, 0.001, 0.001, 3, 2, 2.0, 100)}
        n = 120
        bars = {"close": [[2000.0] * n], "high": [[2000.0] * n], "low": [[2000.0] * n], "ind_mask": [np.zeros(n, dtype=np.uint16)]}
        catalog = [dict(uid=0, pack="general", kind="base", mult=500)]
        cands = dict(sym=np.array([0], np.int16), uid=np.array([0], np.int32), side=np.array([1], np.int8),
                     entry=np.array([61], np.int32), exit=np.array([-1], np.int32), raw=np.array([0.0]),
                     reason=np.array([255], np.uint8), r30=np.array([1.1]), admitted=np.array([True]),
                     axes={a: np.array([False]) for a in sim.AXIS_WINDOWS})

        class Book:
            cost_pct = 0.1

        res = sim.simulate("m", cands, [], None, catalog, [sym], bars, 60, 120, 0, Book(),
                           sim.Sizer(contracts, {"volumeFactor": 1.0, "orderSizing": "factor", "slAutoLeverage": False}, None), 1.0, True, True)
        tot = res["totals"]
        # 1 USDT equity at 100x: each 4 USDT lot needs 0.04 margin (+0.002 fee); the book fills until free margin is gone
        self.assertGreater(tot["positions"]["lotsOpened"], 10)
        self.assertLess(tot["positions"]["lotsOpened"], 500)
        self.assertEqual(tot["positions"]["lotsOpened"] + tot["skipped"]["noFreeMargin"] + tot["skipped"]["belowMinOrQty"], 500)
        self.assertLessEqual(tot["marginMax"], 1.0)


class LoadSymbolTest(unittest.TestCase):
    def test_time_base_is_first_row_not_start_when_warmup_precedes_window(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            rows = [[(1_000_000 + i * 60) * 1000, [1, 2, 0.5, 1.5, 3]] for i in range(5)]
            json.dump(dict(symbol="AAA-USDT", start=(1_000_000 + 120) * 1000, warmup=2, rows=rows),
                      open(os.path.join(d, "AAA-USDT.json"), "w"))
            bars, start_s = sim.load_symbol(d, "AAA-USDT")
            self.assertEqual(start_s, 1_000_000)
            self.assertEqual(len(bars), 5)


class DdtFastTest(unittest.TestCase):
    def test_matches_the_engine_on_random_mixed_symbol_tapes(self):
        import random
        from position_cost import POSITION_COST_PCT_DEFAULT, cost_as_frac
        from set_engine import drawdown_time_by_symbol
        rng = random.Random(7)
        cf = cost_as_frac(POSITION_COST_PCT_DEFAULT)
        for trial in range(300):
            n = rng.randint(0, 96)
            nsym = rng.randint(1, 4)
            t = sorted(rng.choice([0.0] + [60.0 * rng.randint(1, 4000) for _ in range(5)]) if rng.random() < 0.03
                       else 60.0 * rng.randint(1, 4000) for _ in range(n))
            sy = [rng.randrange(nsym) for _ in range(n)]
            mv = [rng.gauss(0.0005, 0.004) for _ in range(n)]
            rows = [{"t": a, "symbol": "S%d" % b, "pnl_pct": c} for a, b, c in zip(t, sy, mv)]
            want = float(drawdown_time_by_symbol(rows, ordered=True)["maxS"]) if rows else 0.0
            got = sim.ddt_max_s_fast(np.array(t), np.array(sy), np.array(mv), cf)
            self.assertEqual(got, want, (trial, n, nsym))


class ChartTest(unittest.TestCase):
    labels = ["05:00", "06:00", "07:00"]

    def test_bars_signed_and_stacked(self):
        svg = sim.svg_bars("pnl", self.labels, [("pnl", [-2.0, 0.0, 3.0])])
        self.assertEqual(svg.count("<rect"), 2)  # the zero hour draws no bar
        st = sim.svg_bars("w/l", self.labels, [("w", [1.0, 2.0, 0.0]), ("l", [3.0, 0.0, 1.0])], stacked=True)
        self.assertEqual(st.count("<rect"), 4)
        self.assertIn("legend", st)

    def test_lines_skip_missing_and_nan(self):
        svg = sim.svg_lines("pf", self.labels, [("a", [1.5, None, float("nan")])], ref=1.0)
        self.assertEqual(svg.count("<circle"), 1)
        self.assertNotIn("nan", svg.lower().replace("<title>", ""))
        self.assertNotIn("<polyline", svg)  # one point cannot draw a line

    def test_degenerate_flat_series(self):
        svg = sim.svg_lines("flat", self.labels, [("e", [0.0, 0.0, 0.0])])
        self.assertIn("<polyline", svg)
        self.assertEqual(sim.svg_bars("empty", [], [("x", [])]).count("<rect"), 0)

    def test_run_charts_from_hourly_rows(self):
        h = dict(startUtc="05:00", equityEnd=9.0, pnl=-1.0, ddMaxPct=10.0, ddIntrabarMaxPct=12.0, marginMaxPct=40.0,
                 closed=dict(wins=1, losses=2), byStrategy={"general/normal": dict(n=3, pfNormal=0.8), "block": dict(n=0)},
                 orders=dict(entry=3, blockAdd=0, dcaEntry=0, dcaAdd=0, closeFills={"sl": 2, "tp": 1},
                             controlPlace=2, controlCancelReplace=0, controlCancel=2),
                 positions=dict(lotsOpened=3, groupsOpened=1),
                 skipped=dict(noFreeMargin=0, belowMinOrQty=0, liveNegativeDeact=0, addOnNoParent=0))
        res = dict(hourly=[h, dict(h, startUtc="06:00")], equityCurve=[[0, 10.0, 9.9, 0.1, 1, 1], [5, 9.0, 8.5, 0.2, 2, 1]])
        out = sim.run_charts(res, 0, 0)
        self.assertEqual(out.count("<svg"), 11)



class AxisChildrenTest(unittest.TestCase):
    """Sim axis children follow coord_engine.axis_variants rules per Set x side."""

    def ev(self, moves):
        n = len(moves)
        return sim.Evidence(np.zeros(n, dtype=np.int64), np.arange(n), np.array(moves, float), 0.1)

    def test_children_cover_every_count_of_the_specs(self):
        kids = sim.axis_children({})
        self.assertEqual(len(kids), 29)
        self.assertEqual([c for a, c in kids if a == "prev"], list(range(4, 13)))
        off = sim.axis_children({"axisPauseEnabled": False, "axisLastMaxWindow": 2})
        self.assertNotIn("pause", {a for a, _ in off})
        self.assertEqual([c for a, c in off if a == "last"], [1, 2])

    def test_partial_window_needs_min_three_and_pause_streak(self):
        win, loss = 0.003, -0.003
        ev = self.ev([win] * 6 + [loss] * 4)
        idx, gs = ev.lookup(np.array([0]), np.array([10]))
        r, ok = ev.partial_ratio(idx, gs, 8, 3)
        self.assertTrue(ok[0])
        want = last_n_cost_pf([{"t": i, "pnl_pct": m} for i, m in enumerate([win] * 6 + [loss] * 4)], 8, 0.1)["ratio"]
        self.assertAlmostEqual(float(r[0]), round(want, 4), places=3)
        self.assertTrue(bool(ev.all_losses(idx, gs, 4)[0]))
        self.assertFalse(bool(ev.all_losses(idx, gs, 5)[0]))
        short = self.ev([win, win])
        i2, g2 = short.lookup(np.array([0]), np.array([2]))
        self.assertFalse(bool(short.partial_ratio(i2, g2, 8, 3)[1][0]))
        self.assertTrue(bool(short.partial_ratio(i2, g2, 2, 2)[1][0]))


class BaseWindowParityTest(unittest.TestCase):
    """The sim Base check equals SetBook: PF over the last min(n, window) closes, n >= need."""

    def test_base_passes_with_need_closes_before_the_window_is_full(self):
        ev = sim.Evidence(np.zeros(35, dtype=np.int64), np.arange(35), np.full(35, 0.003), 0.1)
        idx, gs = ev.lookup(np.array([0]), np.array([35]))
        full_r, full_ok = ev.ratio(idx, gs, 50)
        part_r, part_ok = ev.partial_ratio(idx, gs, 50, 30)
        self.assertFalse(bool(full_ok[0]))   # the old full-window rule rejected n=35
        self.assertTrue(bool(part_ok[0]))    # engine: n=35 >= need 30, PF over 35 closes
        want = last_n_cost_pf([{"t": i, "pnl_pct": 0.003} for i in range(35)], 50, 0.1)
        self.assertEqual(want["count"], 35)
        self.assertAlmostEqual(float(part_r[0]), round(want["ratio"], 4), places=3)
        few_r, few_ok = ev.partial_ratio(*ev.lookup(np.array([0]), np.array([20])), 50, 30)
        self.assertFalse(bool(few_ok[0]))    # below need


class StageParityWithEngineTest(unittest.TestCase):
    """sim.stage_flags == SetBook side gates on identical tapes (Base 50 / Main 30 / Real 30, PF 1.10)."""

    def test_stage_flags_match_setbook_on_random_tapes(self):
        sim._engine_path()
        from connection_profile import processing_profile
        from set_engine import SetBook
        book = SetBook()
        book.load({**processing_profile(), "stratTrailing": False, "slToTpRatios": [0.6], "setMinStep": 8, "setStepMax": 8})
        st = next(x for x in book.by_idx if x.kind == "base")
        rng = np.random.default_rng(7)
        checked = 0
        for n in (20, 29, 30, 35, 49, 50, 55, 80):
            for p_win in (0.35, 0.5, 0.62, 0.75):
                wins = rng.random(n) < p_win
                moves = np.where(wins, 0.004, -0.003)
                st.hist = [{"t": 1_000_000 + i * 60, "pnl_pct": float(m), "symbol": "T", "side": "LONG",
                            "hold_s": 60, "reason": "tp" if m > 0 else "sl"} for i, m in enumerate(moves)]
                st.live = []
                book._score_one(st)
                view = st.by_side["LONG"]
                ev = sim.Evidence(np.zeros(n, dtype=np.int64), np.arange(n), moves.astype(float), float(book.cost_pct))
                idx, gs = ev.lookup(np.array([0]), np.array([n]))
                fl = sim.stage_flags(ev, idx, gs, int(book.eval_need()), book._stage_window_ns(),
                                     {k: float(v) for k, v in book.stage_min_pf.items()})
                self.assertEqual(bool(fl["base_ok"][0]), bool(book._base_metrics_ok(view)), (n, p_win, view.get("base_pf")))
                self.assertEqual(bool(fl["real_ok"][0]), bool(book._real_metrics_ok(view)), (n, p_win, view.get("main_pf"), view.get("real_pf")))
                checked += 1
        self.assertEqual(checked, 32)

if __name__ == "__main__":
    unittest.main()


class DeployedSettingsPrecedenceTest(unittest.TestCase):
    def test_overlay_wins_over_the_profile_like_the_live_engine(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ov.json")
            with open(path, "w") as fh:
                json.dump({"setHistTimeBars": 120, "slMinPct": 0.4}, fh)
            ov = sim.deployed_settings(path)
            self.assertEqual((ov["setHistTimeBars"], ov["slMinPct"]), (120, 0.4))
            self.assertIn("baseEvalPosCount", ov, "profile fills keys the overlay leaves out")
            sim.PROFILE_WINS = True
            try:
                self.assertEqual(sim.deployed_settings(path)["setHistTimeBars"], 30)
            finally:
                sim.PROFILE_WINS = False
