"""Causality and cash reconciliation for the public-data audit harness."""
from __future__ import annotations
import pathlib
import sys
import unittest
import tempfile
from unittest.mock import patch
from types import SimpleNamespace
import numpy as np
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_six_hour_audit as audit
from position_cost import net_return_pf
from pulse_trader import Contract


class SixHourAuditTests(unittest.TestCase):
    def test_completed_lane_checkpoint_resumes_without_another_price_replay(self):
        class States(list):
            open=[]
        book=audit.se.SetBook()
        book.load({"baseEvalPosCount":5,"mainEvalPosCount":5,"realEvalPosCount":5,"setMinSamples":5,
                   "positionCostPct":.18,"baseMinPf":1.1,"mainMinPf":1.1,"realMinPf":1.1})
        meta=next(row for row in audit.account_model.build_catalog(book) if row["pack"]=="general" and row["kind"]=="base")
        result=dict(symbol="TEST",startS=1_700_000_000,bars=[[100,100.5,99.5,100,1] for _ in range(720)],
                    coverage=[dict(requestedConfigs=1)],funding=[])
        catalog=[dict(meta,lane="pack:general",symbolScoped=True)]
        tapes={k:np.array(v) for k,v in dict(uid=[0]*6,side=[1]*6,entry=[0,2,4,6,8,11],
               exit=[1,3,5,7,9,12],evidenceRaw=[.004]*6,reason=["time"]*6).items()}
        evidence=audit.account_model.Evidence(np.ones(6,dtype=int),tapes["exit"],tapes["evidenceRaw"],.18)
        lane=[(1,.6,"general")]*720
        def adjustment(*args,**kwargs):
            parent=args[3][0]
            child=parent.catalog_copy();child.id=parent.id+":adjust:dca:1";child.parent_set_id=parent.id
            child.kind="dca";child.relative_count=1;child.axis_key="dca:1";child.volume_ratio=1
            row=audit.se.hist_fill(result["startS"]+12*60,"TEST",1,.001,60,"time")
            row.update(_entry=11,_parent_entry=10,_exit=12,_avg=100,_qty=1)
            child.hist=[row]
            return States([child])
        with tempfile.TemporaryDirectory() as directory:
            book._audit_lane_cache_dir=directory;book._audit_derived_signature="frozen-input-and-math"
            with patch.object(book,"prepare_replay_signals",return_value=({"general":lane},{},[])), \
                 patch("axis_grid.calculate_axes",return_value=States()), \
                 patch("indication_adjustment_grid.calculate_adjustments",side_effect=adjustment) as replay:
                first,first_counts=audit.derived_replay(result,book,catalog,tapes,evidence)
                self.assertEqual(replay.call_count,1)
            with patch.object(book,"prepare_replay_signals",return_value=({"general":lane},{},[])), \
                 patch("axis_grid.calculate_axes",side_effect=AssertionError("replayed completed lane")), \
                 patch("indication_adjustment_grid.calculate_adjustments",side_effect=AssertionError("replayed completed lane")):
                second,second_counts=audit.derived_replay(result,book,catalog,tapes,evidence)
            self.assertEqual(first_counts,second_counts)
            self.assertEqual(first["catalog"],second["catalog"])
            for key in first["core"]:
                np.testing.assert_array_equal(first["core"][key],second["core"][key])

    def test_lazy_axis_aliases_keep_exact_parent_ownership_and_multiplicity(self):
        from contracts import stable_key
        parents=("TEST|signals|one","TEST|signals|two")
        meta=dict(set_ids=None,parentSetIds=parents,aliasKind="axis",aliasSide="SHORT",symbol="TEST",
                  axisKey="prev:4",relativeCount=4,mult=2)
        actual=audit.materialize_aliases(meta)
        self.assertEqual(actual["set_ids"],["axis:"+stable_key(p,"TEST","SHORT","prev",4) for p in parents])
        self.assertEqual(actual["parentSetIds"],parents)
        self.assertIsNone(meta["set_ids"])

    def test_lazy_adjustment_aliases_keep_exact_step_identity(self):
        meta=dict(set_ids=None,parentSetIds=("TEST|lane|one","TEST|lane|two"),aliasKind="dca",relativeCount=2,mult=2)
        actual=audit.materialize_aliases(meta)
        self.assertEqual(actual["set_ids"],["TEST|lane|one:adjust:dca:2","TEST|lane|two:adjust:dca:2"])

    def test_exact_return_pf_below_floor_is_not_rounded_into_admission(self):
        # .18% cost plus .179928% net gives exact return PF1.09996.
        evidence = audit.account_model.Evidence(np.ones(5,dtype=int),np.arange(5),np.full(5,.00359928),.18)
        prior,first=evidence.lookup(np.array([1]),np.array([5]))
        flags=audit.account_model.stage_flags(evidence,prior,first,5,(5,5,5),{"base":1.1,"main":1.1,"real":1.1})
        self.assertAlmostEqual(flags["r30"][0],1.09996)
        self.assertFalse(flags["base_ok"][0])
        self.assertFalse(flags["real_ok"][0])

    def test_base_parent_selection_accepts_thirty_completed_of_fifty_window(self):
        book = SimpleNamespace(pf_n=50, eval_need=lambda:30, stage_min_pf={"base":1.1})
        evidence = audit.account_model.Evidence(np.ones(30,dtype=int),np.arange(30),np.full(30,.004),.18)
        self.assertEqual(audit.base_qualified_parent_uids(evidence,book,30).tolist(),[0])
        self.assertEqual(audit.base_qualified_parent_uids(evidence,book,29).tolist(),[])

    def test_base_parent_selection_never_borrows_other_scope_closes(self):
        book = SimpleNamespace(pf_n=50, eval_need=lambda:30, stage_min_pf={"base":1.1})
        evidence = audit.account_model.Evidence(np.repeat([1,3],29),np.tile(np.arange(29),2),np.full(58,.004),.18)
        self.assertEqual(audit.base_qualified_parent_uids(evidence,book,40).tolist(),[])

    def test_empty_evidence_has_no_positive_pf(self):
        self.assertIsNone(audit.canonical_stats([], .18)["pf"])

    def test_nonfinite_or_zero_weight_is_not_positive_evidence(self):
        metric = audit.canonical_stats([float("nan"),float("inf"),.004],.18,[1,1,0])
        self.assertEqual(metric["n"],0)
        self.assertIsNone(metric["pf"])
        self.assertEqual(metric["invalidObservations"],3)

    def test_weighted_aliases_preserve_numerical_return(self):
        compact = audit.canonical_stats([.004, -.001], .18, [3, 2])
        expanded = audit.canonical_stats([.004] * 3 + [-.001] * 2, .18)
        self.assertEqual(compact, expanded)

    def test_cash_net_pf_includes_funding_without_charging_cost_twice(self):
        rows = [dict(gross=1, net=.81, notional=100)]
        metric = audit.account_stats(rows, .18)
        self.assertAlmostEqual(metric["pf"], net_return_pf(.0081, .18), places=6)
        self.assertAlmostEqual(metric["netUsdt"], .81)

    def test_funding_changes_evidence_but_never_the_execution_price_move(self):
        bar = [100, 102, 98, 100, 100]
        result = dict(symbol="TEST-USDT", startS=1_800_000_000,
            bars=[bar[:] for _ in range(720)],
            funding=[dict(fundingTime=(1_800_000_000 + 362 * 60) * 1000,
                          fundingRate=".0001", markPrice="100")],
            catalog=[dict(localUid=0, symbolScoped=True, lane="signals|direct_tf:1m",
                          set_id="test", set_ids=["test"], mult=1, kind="base")],
            core={key: np.array(value) for key, value in dict(uid=[0], side=[1], entry=[360],
                                                             exit=[364], raw=[.01], reason=[0]).items()},
            reasons=["tp"])
        book = SimpleNamespace(cost_pct=.18, eval_need=lambda: 30,
            _stage_window_ns=lambda: (50, 30, 30), stage_min_pf={"base":1.1,"main":1.1,"real":1.1})
        _, tapes, candidates, _ = audit.build_causal_candidates([result], book)
        self.assertEqual(tapes["raw"][0], .01)
        self.assertAlmostEqual(tapes["evidenceRaw"][0], .0099)
        self.assertEqual(candidates["countBeforeEntry"][0], 0)
        self.assertFalse(candidates["base_ok"][0])

    def test_actual_account_cash_funding_and_fill_net_reconcile(self):
        bar = [100, 100.5, 99.95, 100, 100]
        source = dict(symbol="TEST-USDT", startS=1_800_000_000, bars=[bar[:] for _ in range(720)],
            funding=[dict(fundingTime=(1_800_000_000 + 362 * 60) * 1000,
                          fundingRate=".0001", markPrice="100")])
        source["bars"][364]=[100,101.5,99.95,101,100]
        catalog = [dict(kind="base", lane="signals|direct_tf:1m", set_ids=["test"],sl=.02,tp=.01,trail="",pack="indications")]
        candidates = {key:np.array(value) for key,value in dict(uid=[0], sym=[0], side=[1], entry=[360],
            exit=[364], raw=[.01], reason=["tp"], admitted=[True], r30=[1.2], countBeforeEntry=[30]).items()}
        overlay = {"orderSizing":"minQty", "marginCapPct":.1,
                   "positionCostPct":.18,
                   "axisPrevEnabled":False,"axisLastEnabled":False,"axisContEnabled":False,"axisPauseEnabled":False}
        book=audit.se.SetBook();book.load(overlay)
        result = audit.execute_account([source], catalog, candidates, book, overlay,
            {"TEST-USDT":Contract("TEST-USDT",.1,.1,1,1,1,100)}, 1000, gated=False)
        close = result["closedPositions"][0]
        self.assertAlmostEqual(close["fundingUsdt"], -.0001 * close["notional"])
        self.assertAlmostEqual(result["totals"]["balanceEnd"] - 1000, close["net"], places=8)
        self.assertAlmostEqual(result["totals"]["closed"]["pf"], 1.45, places=6)
        self.assertEqual(result["totals"]["orders"]["entry"], 1)
        self.assertEqual(result["totals"]["orders"]["close"], 1)


if __name__ == "__main__":
    unittest.main()
