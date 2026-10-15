"""Active forced and native-count gates use cost-relative returns once."""
import pathlib
import importlib.util
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from coord_engine import Coordinator, recent_closed_rows
import forced_configs as forced
from position_cost import last_n_cost_pf, net_return_pf


class RemainingGateTests(unittest.TestCase):
    def test_count_and_parent_gate_do_not_round_weak_pf_up_to_floor(self):
        book = self.coordinator()
        allow, _, metric = self.add(book,[.0009996]*3)
        self.assertFalse(allow)
        self.assertAlmostEqual(metric["countPf"],1.09996)
        rows = [dict(t=i,pnl_pct=.0019996) for i in range(8)]
        allow, _, metric = book.gate(rows,0)
        self.assertFalse(allow)
        self.assertAlmostEqual(metric["lastPf"],1.09996)

    def test_forced_training_exact_floor_rejects_rounding_candidate(self):
        row = dict(evidenceVersion=forced.FORCED_EVIDENCE_VERSION,source="historical-market",
                   symbol="XRP-USDT",indication="signals",direction="LONG",tpPct=.4,slPct=.1,
                   pf=1.2,holdoutPf=None,holdoutN=0,trainingCostPct=.15,
                   trainingResults=[.0014994]*5)
        self.assertAlmostEqual(forced.training_window(row)["trainPf"],1.09996)
        self.assertFalse(forced.valid_candidate(row,1.1))

    def test_unavailable_forced_holdout_ranks_after_equal_observed_config(self):
        common = dict(tradesPerHour=2.,slPct=.1,maxDrawdownR=1.,tpPct=.4,pf=1.2)
        missing = dict(common,id="a",holdoutPf=None)
        observed = dict(common,id="b",holdoutPf=.8)
        self.assertGreater(forced.rank_key(missing),forced.rank_key(observed))

    def test_coord_optimization_net_mean_is_roi_with_cash_mean_named_separately(self):
        book = self.coordinator()
        rows = [dict(t=i,pnl=3.,pnl_pct=.004,qty=10.,entry=100.) for i in range(8)]
        book.gate(rows,0)
        self.assertAlmostEqual(book.optimization_stats["netAvg"],.003)
        self.assertAlmostEqual(book.optimization_stats["cashNetAvg"],3.)

    def test_recent_closed_horizon_excludes_old_millisecond_row(self):
        now = 1_800_000_030
        old = dict(t=(now-20_000)*1000,pnl_pct=.05)
        latest = dict(t=now-10,pnl_pct=.002)
        self.assertEqual(recent_closed_rows([latest,old],now=now),[latest])

    def test_reuse_cannot_relabel_or_persist_legacy_classic_artifact(self):
        path = pathlib.Path(__file__).with_name("apply_forced_symbol_configs.py")
        spec = importlib.util.spec_from_file_location("apply_forced_for_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            artifact = pathlib.Path(folder) / "old.json"
            artifact.write_text(json.dumps(dict(version=3, rows=[dict(trainPf=99)])))
            with patch.object(module, "FORCED_PROJECT", str(artifact)), patch.object(module, "load_overlay") as load, patch.object(module, "persist_bundle") as persist:
                with self.assertRaises(ValueError):
                    module.persist_existing()
                load.assert_not_called()
                persist.assert_not_called()
            self.assertEqual(json.loads(artifact.read_text())["version"], 3)

    def coordinator(self):
        book = Coordinator()
        book.load({}, dict(positionCostPct=.1, minPf=1.1))
        book.axes["last"].enabled = True
        book.axes["last"].max_window = 3
        book.axes["pause"].enabled = False
        return book

    def add(self, book, tape):
        # The count's gate remains independent of the parent's known gate.
        with patch.object(book, "gate", return_value=(True, [], {})):
            return book.add_gate([], 0, count=3, count_tape=tape)

    def test_profitable_classic_ring_can_fail_return_pf_floor(self):
        allow, reasons, metrics = self.add(self.coordinator(), [.0001]*3)
        self.assertFalse(allow)
        self.assertEqual(metrics["countClassicPf"], 99)
        self.assertAlmostEqual(metrics["countPf"], 1.01)
        self.assertTrue(reasons)

    def test_scalar_net_ring_is_not_charged_twice(self):
        allow, _, metrics = self.add(self.coordinator(), [.001]*3)
        self.assertTrue(allow)
        self.assertAlmostEqual(metrics["countPf"], 1.1)

    def test_measured_row_cost_is_preserved_in_count_gate(self):
        rows = [dict(pnl_pct=.0002, position_cost_pct=.01),
                dict(pnl_pct=.002, position_cost_pct=.1),
                dict(pnl_pct=.002, position_cost_pct=0, cost_source="live-exchange")]
        allow, _, metrics = self.add(self.coordinator(), rows)
        self.assertTrue(allow)
        self.assertEqual(metrics["countPf"], last_n_cost_pf(rows, 3, .1)["ratio"])

    def test_unavailable_count_data_blocks_and_empty_headline_is_unavailable(self):
        allow, _, metrics = self.add(self.coordinator(), [None, float("nan")])
        self.assertFalse(allow)
        self.assertIsNone(metrics["countPf"])
        self.assertEqual(metrics["countN"], 0)
        self.assertEqual(metrics["countUnavailableReturns"], 2)

    def test_explicit_free_cost_and_zero_pf_are_not_replaced_by_defaults(self):
        book = self.coordinator()
        book.load({}, dict(positionCostPct=0, minPf=1.1))
        book.axes["last"].enabled = True
        book.axes["last"].max_window = 3
        self.assertEqual(book.position_cost_pct, 0)
        allow, _, metric = self.add(book, [-.01]*3)
        self.assertFalse(allow)
        self.assertAlmostEqual(metric["countPf"], 0)

    def test_forced_training_rejects_many_tiny_wins_despite_classic_99(self):
        row = dict(trainingResults=[.00001]*30, trainingCostPct=.15, slPct=.1)
        metric = forced.training_window(row)
        self.assertEqual(metric["trainClassicPf"], 99)
        self.assertAlmostEqual(metric["trainPf"], net_return_pf(.00001, .15))
        self.assertLess(metric["trainPf"], forced.MIN_PF)

    def test_forced_cost_net_training_and_free_reference(self):
        row = dict(trainingResults=[.0015]*5, trainingCostPct=.15, slPct=.1)
        self.assertAlmostEqual(forced.training_window(row)["trainPf"], 1.1)
        row["trainingCostPct"] = 0
        self.assertAlmostEqual(forced.training_window(row)["trainPf"], 1.15)
        row["trainingCostPct"] = float("inf")
        self.assertIsNone(forced.training_window(row)["trainPf"])

    def test_empty_forced_replay_never_has_positive_pf(self):
        result = forced._replay([[100.,100.,100.,100.,1.]]*20, [(0,0.)]*20,
                                1, .4, .1, 0, 100000., .15, 0, 1.05, 6)
        self.assertIsNone(result["pf"])
        self.assertIsNone(result["trainPf"])
        self.assertIsNone(result["holdoutPf"])
        self.assertFalse(result["eligible"])
        self.assertEqual(result["evidenceVersion"], 4)


if __name__ == "__main__":
    unittest.main()
