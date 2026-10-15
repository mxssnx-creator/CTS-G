"""Risk-variant selection and headlines use measured return/cost evidence."""
import pathlib
import sys
import unittest

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/"server/pulse"))
from risk_variants import VariantBook
from position_cost import last_n_cost_pf


class RiskVariantReturnTests(unittest.TestCase):
    def test_headline_is_return_pf_and_classic_cash_is_explicit_diagnostic(self):
        b=VariantBook(); b.load({"positionCostPct":.1})
        rows=[dict(t=1000,pnl=1000.,pnl_pct=.004,hold_s=60),
              dict(t=1060,pnl=-1.,pnl_pct=-.006,hold_s=60)]
        score=b._score_rows(rows)
        expected=last_n_cost_pf(rows,len(rows),.1)
        self.assertEqual(score.pf,expected["ratio"])
        self.assertEqual(score.ratio,score.pf)
        self.assertLess(score.pf,1.)
        self.assertEqual(score.classic_pf,1000.)
        self.assertEqual(score.expectancy,expected["netAvg"])
        self.assertEqual(score.cash_expectancy,499.5)

    def test_live_close_preserves_actual_per_row_cost_and_measured_free_fill(self):
        b=VariantBook(); b.load({"positionCostPct":.18,"slToTpAuto":False,"trailAuto":False})
        rows=[dict(t=1000+i,pnl=.1,pnl_pct=.004,position_cost_pct=cost,hold_s=60,
                   sl_ratio=.6,trail_key="0.3:0.1",qty=1.,entry=100.)
              for i,cost in enumerate([0.,.01,.4])]
        for row in rows:b.on_close(row)
        stored=b.sl_rows["0.6"]
        self.assertEqual([r["position_cost_pct"] for r in stored],[0.,.01,.4])
        expected=last_n_cost_pf(rows,len(rows),.18)
        score=b._score_rows(stored)
        self.assertEqual(score.pf,expected["ratio"])
        self.assertEqual(score.n,expected["count"])

    def test_zero_pf_is_not_replaced_by_one_and_empty_is_unavailable(self):
        b=VariantBook(); b.load({"positionCostPct":.15})
        zero=b._score_rows([dict(pnl_pct=-.0135,pnl=-10.)])
        self.assertEqual((zero.pf,zero.ratio),(0.,0.))
        self.assertEqual(zero.n,1)
        empty=b._score_rows([])
        self.assertEqual((empty.pf,empty.ratio,empty.n),(0.,0.,0))
        snap=b.snapshot()["sl"][0] if "sl" in b.snapshot() else b.snapshot()["slScores"][0]
        self.assertFalse(snap["available"])

    def test_missing_roi_is_reconstructed_from_executed_notional(self):
        b=VariantBook(); b.load({"positionCostPct":.1,"slToTpAuto":False,"trailAuto":False})
        row=dict(t=1000,pnl=.4,qty=1.,entry=100.,position_cost_pct=.2,sl_ratio=.6)
        b.on_close(row)
        stored=b.sl_rows["0.6"][0]
        self.assertNotIn("pnl_pct",stored)
        self.assertEqual(b._score_rows([stored]).pf,last_n_cost_pf([row],1,.1)["ratio"])
        b.on_close(dict(pnl=50.,sl_ratio=.8))
        self.assertEqual(b._score_rows(b.sl_rows["0.8"]).n,0)

    def test_zero_configured_cost_and_partial_or_unconfirmed_events(self):
        b=VariantBook(); b.load({"positionCostPct":0.,"slToTpAuto":False,"trailAuto":False})
        self.assertEqual(b.cost_pct,0.)
        for patch in [dict(partial=True),dict(exchange_confirmed=False),dict(ours=False)]:
            b.on_close(dict(pnl_pct=.004,sl_ratio=.6,**patch))
        self.assertEqual(b.sl_rows,{})

    def test_normal_close_does_not_credit_the_selected_trailing_configuration(self):
        b=VariantBook(); b.load({"slToTpAuto":False,"trailAuto":False})
        b.on_close(dict(pnl_pct=.004,pnl=.4,sl_ratio=.6,trail_key=""))
        self.assertEqual(len(b.sl_rows["0.6"]),1)
        self.assertEqual(b.trail_rows,{})
        self.assertEqual(b.trail_closes,0)

    def test_missing_or_invalid_stop_ratio_never_credits_the_current_selected_ratio(self):
        b=VariantBook(); b.load({"slToTpAuto":False,"trailAuto":False})
        for patch in [{},dict(sl_ratio=0.),dict(sl_ratio=float("nan")),dict(sl_ratio=-1.),dict(sl_ratio=4.)]:
            b.on_close(dict(pnl_pct=.004,pnl=.4,**patch))
        self.assertEqual(b.sl_rows,{})
        self.assertEqual(b.sl_closes,0)
        b.on_close(dict(pnl_pct=.004,pnl=.4,trail_key="0.3:0.1"))
        self.assertEqual(b.sl_rows,{})
        self.assertEqual(len(b.trail_rows["0.3:0.1"]),1)


if __name__ == "__main__":unittest.main()
