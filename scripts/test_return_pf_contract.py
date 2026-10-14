"""Return PF, fee accounting and hourly admission share one cost-net tape."""
import pathlib
import sys
import unittest
from collections import UserDict

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ao_sim as ao
import position_cost as pc
import stage_ledger as sl
import stats_report as report
import pf_audit


class ReturnCostContract(unittest.TestCase):
    def test_hard_boundary_is_strict_even_at_the_requested_floor(self):
        self.assertFalse(pc.clears_pf(1.02, 1.02))
        self.assertTrue(pc.clears_pf(1.03, 1.02))
        self.assertTrue(pc.clears_pf(1.15, 1.15))

    def test_small_measured_cost_uses_percent_in_net_and_pf(self):
        row = dict(t=1, qty=1, entry=100, pnl_pct=.0002, fee_total=.01, cost_source="live-exchange")
        metric = pc.last_n_cost_pf([row], 1)
        self.assertAlmostEqual(pc.row_position_cost_pct(row), .01)
        self.assertAlmostEqual(pc.row_net_pnl(row), .0001)
        self.assertAlmostEqual(metric["ratio"], 1.10)
        self.assertAlmostEqual(metric["netPct"], .01)
        self.assertAlmostEqual(metric["classicPf"], 99)

    def test_measured_free_fill_preserves_net_with_finite_reference(self):
        row = dict(t=1, qty=1, entry=100, pnl_pct=.0005, position_cost_pct=0, cost_source="live-exchange")
        metric = pc.last_n_cost_pf([row], 1)
        self.assertAlmostEqual(metric["ratio"], 1.05)
        self.assertEqual(metric["pfReferenceCostPct"], .10)
        self.assertEqual(metric["zeroCostSamples"], 1)
        self.assertEqual(metric["costPct"], 0)
        self.assertEqual(metric["netPct"], .05)
        simple = pc.last_n_cost_pf([dict(t=1, pnl_pct=.0005)], 1, 0)
        self.assertEqual(simple["ratio"], metric["ratio"])
        self.assertEqual(simple["netPct"], metric["netPct"])
        configured = pc.last_n_cost_pf([dict(row, pnl_pct=.002)], 1, .20)
        self.assertEqual(configured["ratio"], 1.10)
        self.assertEqual(configured["pfReferenceCostPct"], .20)
        self.assertEqual(pc.net_return_pf(.002, 0, .20), 1.10)

    def test_mixed_fees_report_actual_average_return(self):
        rows = [dict(t=1, qty=1, entry=100, pnl_pct=.002, position_cost_pct=.1, cost_source="live-exchange"),
                dict(t=2, qty=1, entry=100, pnl_pct=.006, position_cost_pct=.2, cost_source="live-exchange")]
        metric = pc.last_n_cost_pf(rows, 2)
        self.assertEqual(metric["ratio"], 1.15)
        self.assertEqual(metric["netPct"], .25)
        self.assertEqual(metric["grossPct"], .4)
        self.assertEqual(metric["netAvg"], .0025)
        evidence = pc.cost_aware_metrics(rows)
        self.assertEqual(evidence["netReturnPf"], 1.15)
        self.assertEqual(evidence["grossReturnPf"], 1.25)

    def test_zero_total_placeholder_cannot_erase_entry_exit_fees(self):
        row = dict(qty=1, entry=100, pnl_pct=.002, fee_total=0, entry_fee=.05, exit_fee=.05)
        self.assertAlmostEqual(pc.row_position_cost_pct(row), .1)
        self.assertAlmostEqual(pc.row_net_pnl(row), .001)

    def test_mapping_legacy_net_result_and_timestamp_are_supported(self):
        rows = [UserDict(t=3, notional=100, pnl=.1, position_cost_pct=.1),
                UserDict(t=1, notional=100, pnl=-.5, position_cost_pct=.1)]
        metric = pc.last_n_cost_pf(rows, 1)
        self.assertEqual(metric["count"], 1)
        self.assertEqual(metric["ratio"], 1.1)
        self.assertAlmostEqual(pc.row_net_pnl(rows[0]), .001)

    def test_independent_raw_audit_keeps_small_and_zero_cost_units(self):
        for cost, gross, expected in ((.01, .0002, 1.1), (0, .0005, 1.05)):
            row = dict(t=1, pnl_pct=gross, position_cost_pct=cost)
            self.assertEqual(pf_audit.raw_cost_pf([row], 1)[0], expected)
            self.assertEqual(pf_audit.hand_pf([row], 1)["handRatio"], expected)

    def test_unknown_notional_is_not_a_synthetic_cost_only_loss(self):
        metric = pc.last_n_cost_pf([dict(t=1, pnl=1)], 1)
        self.assertEqual(metric["count"], 0)
        self.assertEqual(metric["ratio"], 1)
        self.assertEqual(metric["unavailableReturnSamples"], 1)
        invalid = pc.last_n_cost_pf([dict(t=1, pnl_pct=float("nan")), dict(t=2, pnl_pct=float("inf"))], 2)
        self.assertEqual(invalid["count"], 0)
        self.assertEqual(invalid["unavailableReturnSamples"], 2)
        evidence = pc.cost_aware_metrics([dict(t=1, pnl=1)])
        self.assertEqual(evidence["sampleCount"], 0)
        self.assertEqual(evidence["unavailableReturnSamples"], 1)


class ReportContract(unittest.TestCase):
    def test_report_preserves_fee_scope_and_small_cost(self):
        row = dict(t=1, qty=1, entry=100, pnl_pct=.001, fee=.05, fee_scope="total", cost_source="live-exchange")
        raw = report.enrich(report._row(row), .1)
        self.assertEqual(raw["costPct"], .05)
        self.assertEqual(raw["netPnl"], .05)
        self.assertEqual(report.pf_window([raw], None, .1)["pf"], 1.1)

    def test_report_keeps_a_genuine_zero_return_pf(self):
        row = report.enrich(report._row(dict(t=1, qty=1, entry=100, pnl_pct=-.009)), .1)
        metric = report.pf_window([row], None, .1)
        self.assertEqual(metric["pf"], 0)
        self.assertEqual(metric["pfAfterCost"], 0)

    def test_unknown_cash_return_is_accounted_but_not_pf_evidence(self):
        known = dict(t=1, qty=1, entry=100, pnl_pct=.01, pack="general")
        unknown = [dict(t=i+2, pnl=1, pack="general") for i in range(7)]
        metric = report.build(dict(closed=[known]+unknown), cost_pct=.1)["byStrategy"]["general"]
        self.assertEqual(metric["n"], 8)
        self.assertEqual(metric["returnSampleCount"], 1)
        self.assertEqual(metric["unavailableReturnSamples"], 7)
        self.assertFalse(metric["validated"])

    def test_report_uses_configured_floor_and_no_empty_positive_evidence(self):
        rows = [dict(t=i, qty=1, entry=100, pnl_pct=.0013) for i in range(30)]
        positive = report.build(dict(closed=rows, pfCost=dict(n=30, minPf=1.02)), cost_pct=.1)
        self.assertEqual(positive["costAccounting"]["minPf"], 1.02)
        self.assertTrue(positive["costAccounting"]["pass"])
        self.assertFalse(report.build(dict(pfCost=dict(n=30, minPf=1.02)))["costAccounting"]["pass"])

    def test_gross_and_net_ddt_read_their_own_return(self):
        rows = [dict(t=1000+i*60, symbol="A", qty=1, entry=100, pnl=.05, pnl_pct=.0005) for i in range(3)]
        blob = report.build(dict(closed=rows), cost_pct=.1)
        self.assertEqual(blob["drawdownTime"]["gross"]["episodes"], 0)
        self.assertGreater(blob["drawdownTime"]["afterCost"]["episodes"], 0)

    def test_replay_selection_cannot_keep_live_amounts_or_pf_aliases(self):
        row = report.enrich(report._row(dict(t=1, qty=1, entry=100, pnl_pct=.01, ind_kind="move")), .1)
        gate = dict(move=dict(n=40, pf=1.03, ok=True, validated=True))
        blob = report.merge_kind_stats([row], .1, gate=gate)["move"]
        self.assertEqual(blob["pfAfterCost"], 1.03)
        self.assertEqual(blob["costRatio"], 1.03)
        for key in ("netAfterCost", "wins", "losses", "classicPf", "netAvg"):
            self.assertIsNone(blob[key])

    def test_open_lanes_and_exit_candidates_are_not_closed_strategy_samples(self):
        result = report.merge_strategy_stats([], .1,
                    dca=dict(lanes=[{}, {}, {}], last15Ratio=0, last15N=0),
                    exits=dict(lanes=[dict(n=30, last15Ratio=1.2, selected=True),
                                      dict(n=50, last15Ratio=1.3)]),
                    sets_rows=[dict(pack="general", last15Ratio=1.3)])
        for key in ("dca", "exits", "general"):
            self.assertEqual(result[key]["n"], 0)
            self.assertEqual(result[key]["pf"], 1)
            self.assertEqual(result[key]["pfAfterCost"], 1)
        self.assertEqual(result["dca"]["calculationPreview"]["pf"], 0)
        self.assertEqual(result["exits"]["calculationPreview"]["n"], 30)

    def test_explicit_child_strategy_survives_report_normalization(self):
        rows = [dict(t=1, qty=1, entry=100, pnl_pct=.01, reason="tp", pack="indications",
                     strategy="block", ind_kind="signals"),
                dict(t=2, qty=1, entry=100, pnl_pct=.01, reason="tp", pack="indications",
                     strategy="dca", ind_kind="signals")]
        result = report.build(dict(closed=rows), cost_pct=.1)
        self.assertEqual(result["byStrategy"]["block"]["n"], 1)
        self.assertEqual(result["byStrategy"]["dca"]["n"], 1)
        self.assertEqual(result["byIndication"]["signals"]["n"], 0)


class HourlyContract(unittest.TestCase):
    @staticmethod
    def tape(values, cost=.2):
        times = np.arange(1, len(values)+1) * sl.H_MS
        return sl.Tape(times-sl.H_MS, times, values, [1]*len(values), cost)

    def test_tiny_loss_free_returns_do_not_pass_pf_floor(self):
        config = sl.StageConfig(long_h=100, min_trades=4, min_pf=1.1)
        result = sl.base_eval(self.tape([.0001]*5), np.array([10*sl.H_MS]), config)
        self.assertAlmostEqual(result["pf"][0], 1.005)
        self.assertEqual(result["classicPf"][0], 4)
        self.assertFalse(result["ok"][0])
        self.assertTrue(sl.base_eval(self.tape([.002]*5), np.array([10*sl.H_MS]), config)["ok"][0])
        boundary = sl.StageConfig(long_h=100, min_trades=4, min_pf=1.02)
        self.assertFalse(sl.base_eval(self.tape([.0004]*5), np.array([10*sl.H_MS]), boundary)["ok"][0])

    def test_hourly_cost_rows_and_side_tapes_match_canonical_pf(self):
        tape = self.tape([.001, .004], [.1, .2])
        for candidate in (tape, tape.side_tape(1)):
            metric = sl.base_eval(candidate, np.array([3*sl.H_MS]), sl.StageConfig())
            self.assertAlmostEqual(metric["pf"][0], 1.15)
        self.assertAlmostEqual(sl.summarize([.001, .004], [.1, .2])["pf"], 1.15)

    def test_invalid_closes_are_not_samples_and_cannot_poison_future_windows(self):
        tape = self.tape([float("nan"), .002, .002, float("inf"), .002])
        metric = sl.base_eval(tape, np.array([10*sl.H_MS]), sl.StageConfig(min_trades=4))
        self.assertEqual(metric["n"][0], 3)
        self.assertFalse(metric["ok"][0])
        self.assertAlmostEqual(metric["pf"][0], 1.1)
        empty = sl.summarize([float("nan"), float("inf")])
        self.assertEqual(empty["n"], 0)
        self.assertFalse(empty["sampleAvailable"])

    def test_guard_cannot_resume_on_classic_no_loss_pf(self):
        guard = sl.lane_guard([], shadow_r=np.full(3, .0001), n=3, live_orders=False)
        self.assertFalse(guard["liveOrders"])
        self.assertAlmostEqual(guard["pf"], 1.005)

    def test_ao_uses_the_simulated_cost_and_keeps_classic_diagnostic(self):
        trade = ao.Trade("cfg", "A", 1, 0, 1, 100, 100.2, .001, "tp", 1, 0, 0, cost=.001)
        metric = ao.stats_of([trade])
        self.assertAlmostEqual(metric["pf"], 1.1)
        self.assertEqual(metric["classicPf"], ao.PF_NO_LOSS)
        self.assertFalse(ao.stats_of([])["sampleAvailable"])


if __name__ == "__main__":
    unittest.main()
