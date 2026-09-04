#!/usr/bin/env python3
"""Deterministic no-network regression tests for forced/VST evidence gates."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../server/pulse")))
import forced_configs as forced
from hist_calc import resolve_symbols, cli_options
from position_cost import completed_roundtrips
from set_engine import SetBook, IND_KINDS, trades_per_hour
from pulse_trader import Pulse


class ForcedTests(unittest.TestCase):
    def row(self, **overrides):
        return {"id": "forced:XRP:signals:LONG:test", "symbol": "XRP-USDT", "indication": "signals",
                "direction": "LONG", "tpPct": .4, "slPct": .1, "pf": 1.2, "trainPf": 1.3,
                "holdoutPf": 1.1, "trainN": 20, "holdoutN": 10, "maxDrawdownR": 2,
                "tradesPerHour": 4, "source": "historical-market", "eligible": True, **overrides}

    def replay(self, bars, signals=None, **kwargs):
        return forced._replay(bars, signals or [(1, .9)] * len(bars), 1, .4, .1, 0,
                              100000, kwargs.get("cost", .15), 8, 1.02, 6)

    def test_exact_grid(self):
        self.assertEqual(len(forced.TP_GRID) * len(forced.SL_GRID), 81)
        self.assertEqual(forced.TP_GRID, tuple(round(.4 + i * .05, 2) for i in range(9)))
        self.assertEqual(forced.SL_GRID, tuple(round(.1 + i * .05, 2) for i in range(9)))

    def test_forced_symbols_are_mandatory_not_duplicate(self):
        self.assertEqual(resolve_symbols({"symbols": ["XRPUSDT"], "allSymbols": False}), list(forced.FORCED_SYMBOLS))

    def test_forced_cli(self):
        self.assertTrue(cli_options(["--forced-only", "--hours", "24"])["forcedOnly"])

    def test_recent_pf_and_drawdown_are_explicit_diagnostics(self):
        result = self.replay([[100, 101, 99, 100, 1]] * 120)
        self.assertEqual(result["recentPf"]["last8"]["classicPf"], 0)
        self.assertTrue(result["recentPf"]["last8"]["available"])
        self.assertFalse(result["recentPf"]["last75"]["available"])
        self.assertEqual(result["ddEpisodes"], 1)
        self.assertGreater(result["avgDdS"], 0)
        self.assertEqual(result["avgDdS"], result["maxDdS"])

    def test_strict_pf_and_input_validation(self):
        self.assertTrue(forced.valid_candidate(self.row()))
        for field in ("pf", "trainPf", "holdoutPf"):
            self.assertFalse(forced.valid_candidate(self.row(**{field: 1.02})))
            self.assertFalse(forced.valid_candidate(self.row(**{field: float("nan")})))
        for changes in ({"source": "synth"}, {"trainN": 7}, {"holdoutN": 7}, {"slPct": .12}, {"maxDrawdownR": 7}):
            self.assertFalse(forced.valid_candidate(self.row(**changes)))

    def test_top_five_per_symbol_and_kind_throughput_first(self):
        rows = [self.row(id=f"{sym}:{kind}:{i}", symbol=sym, indication=kind, tradesPerHour=i)
                for sym in forced.FORCED_SYMBOLS for kind in IND_KINDS for i in range(8)]
        selected = forced.select_best(rows)
        self.assertEqual(len(selected), 120)
        self.assertTrue(all(row["tradesPerHour"] >= 3 for row in selected))
        self.assertEqual(forced.select_best([self.row(eligible=False)]), [])

    def test_positive_baseline_and_costs(self):
        bars = [[100, 100.6, 99.99, 100, 1]] * 120
        result = self.replay(bars)
        self.assertTrue(result["eligible"])
        self.assertAlmostEqual(result["netAvgPct"], .25)
        self.assertLessEqual(result["tradesPerHour"], 30)
        self.assertFalse(self.replay(bars, cost=.5)["eligible"])

    def test_stop_first_when_both_touched(self):
        result = self.replay([[100, 101, 99, 100, 1]] * 120)
        self.assertEqual(result["pf"], 0)
        self.assertAlmostEqual(result["netAvgPct"], -.25)

    def test_stop_gap_not_optimistic(self):
        bars = [[100, 100, 100, 100, 1], [99, 100, 98, 100, 1]] * 60
        self.assertAlmostEqual(self.replay(bars)["netAvgPct"], -1.15)

    def test_no_entry_candle_lookahead(self):
        bars = [[100, 102, 99, 100, 1]] + [[100, 100, 100, 100, 1]] * 5
        self.assertEqual(self.replay(bars, [(1, .9)] + [(0, 0)] * 5)["n"], 0)

    def test_holdout_loss_vetoes_training_profit(self):
        result = self.replay([[100, 100.6, 99.99, 100, 1]] * 84 + [[100, 100, 99, 100, 1]] * 36)
        self.assertGreater(result["trainPf"], 1.02)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["holdoutPf"], 0)

    def test_complete_coverage_without_signals(self):
        with patch.object(forced, "indication_kind_votes", return_value=[]):
            result = forced.evaluate_symbol("BCH-USDT", [[100, 100, 100, 100, 1]] * 20, {}, 10000)
        self.assertEqual(result["completed"], 1296)
        self.assertEqual(result["best"], [])
        self.assertEqual(len({r["id"] for r in result["rows"]}), 1296)
        self.assertTrue(all(result["settings"][f"type{k}"] for k in ("Signals", "State", "Direction", "Move", "Active", "Common", "Trend", "Break")))

    def test_roundtrip_partial_dedupe_and_missing_history(self):
        base = {"client_id": "test", "symbol": "XRP-USDT", "side": "LONG", "conn": "bingx-x02",
                "entry": 100, "pnl_pct": .004, "position_cost_pct": .15, "exchange_confirmed": True,
                "roundtrip_qty": 3, "hold_s": 60}
        first = dict(base, t=10, qty=1, pnl=.25, partial=True, close_fill_id="a")
        last = dict(base, t=20, qty=2, pnl=.5, partial=False, close_fill_id="b")
        self.assertEqual(completed_roundtrips([first]), [])
        self.assertEqual(completed_roundtrips([last]), [])
        result = completed_roundtrips([first, last, first])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["qty"], 3)
        self.assertAlmostEqual(result[0]["pnl"], .75)
        self.assertEqual(completed_roundtrips([dict(last, exchange_confirmed=False)]), [])

    def test_vst_only_and_own_candidate_gate(self):
        p = object.__new__(Pulse)
        p.api = SimpleNamespace(base="https://open-api-vst.bingx.com")
        p.sets = SimpleNamespace(live_test_mode=True)
        p.open, p.closed, p.control_orders, p.position_cost_pct = {}, [], True, .15
        p._forced_data = lambda: {"rows": [self.row()]}
        with patch("pulse_trader.CONN_SHORT", "bingx-x02"):
            self.assertTrue(p._forced_entry_allowed(self.row(), "XRP-USDT", "LONG", .9))
            p.api.base = "https://open-api.bingx.com"
            self.assertFalse(p._forced_entry_allowed(self.row(), "XRP-USDT", "LONG", .9))
            p.api.base = "https://open-api-vst.bingx.com.evil.invalid"
            self.assertFalse(p._forced_entry_allowed(self.row(), "XRP-USDT", "LONG", .9))
        with patch("pulse_trader.CONN_SHORT", "bingx-x01"):
            p.api.base = "https://open-api-vst.bingx.com"
            self.assertFalse(p._forced_entry_allowed(self.row(), "XRP-USDT", "LONG", .9))

    def test_adaptation_cannot_pool_unrelated_configs(self):
        book = SetBook()
        book.load({"setMinStep": 1, "setStepMax": 3, "stratTrailing": False,
                   "stratIndications": False, "slToTpRatios": [.6], "setMinSamples": 8})
        rows = [dict(t=i, set_id=f"set-{i}:st3", step=3, symbol="XRP-USDT", side="LONG",
                     pnl_pct=.009, pnl=1, exchange_confirmed=True) for i in range(8)]
        book.adapt_from_live(rows)
        self.assertEqual(book.min_step, 1)
        for row in rows:
            row["set_id"] = "same:st3"
        book.adapt_from_live(rows)
        self.assertEqual(book.min_step, 3)

    def test_throughput_does_not_count_duplicate_partials(self):
        self.assertEqual(trades_per_hour([{"t": 0, "client_id": "one"}, {"t": 3600, "client_id": "one"}]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
