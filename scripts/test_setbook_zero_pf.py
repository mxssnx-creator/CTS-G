"""A real zero Return PF stays negative evidence through reports and gates."""
import pathlib
import sys
import unittest

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/"server/pulse"))
from set_engine import SetBook, hist_fill, last_n_chrono, hist_row_key, trades_per_hour


class SetBookZeroPfTests(unittest.TestCase):
    def precision_book(self):
        b = SetBook()
        b.load(dict(positionCostPct=.1, baseEvalPosCount=5,
            mainEvalPosCount=5, realEvalPosCount=5, setMinSamples=5,
            setMinStep=7, setStepMax=7, setMinPf=1.1,
            stratTrailing=False, stratGeneral=False, slToTpRatios=[.6],
            microEnabled=False))
        return b

    def test_fast_history_does_not_round_pf_up_to_gate_floor(self):
        b = self.precision_book()
        gross = [.0019996] * 5
        metric = b._fast_historic_pf_from_gross(gross, 5, .1, .001)
        self.assertAlmostEqual(metric["ratio"], 1.09996, places=12)
        st = b.by_idx[0]
        st.hist = [hist_fill(1000+i*60, "X", 1, move, 60, "tp")
                   for i, move in enumerate(gross)]
        b._score_pair((st, None))
        self.assertFalse(st.stage_ledger["base"])
        self.assertLess(st.base_pf, 1.1)
        self.assertFalse(b.execution_allowed(st, "indications", "LONG"))

    def test_stored_stage_record_keeps_sub_floor_precision(self):
        b = self.precision_book()
        st = b.by_idx[0]
        st.hist = [hist_fill(1000+i*60, "X", 1, .001999996, 60, "tp")
                   for i in range(5)]
        b._score_pair((st, None))
        self.assertAlmostEqual(st.base_pf, 1.0999996, places=12)
        self.assertFalse(st.stage_ledger["base"])
        self.assertEqual(st.stage_ledger["records"]["Base"]["pf"], st.base_pf)
        self.assertEqual(b.stage_record(st, "Base").net_pf, st.base_pf)

    def test_mixed_seconds_milliseconds_keep_the_actual_recent_window(self):
        b = self.precision_book()
        start = 1_760_000_000
        old = [hist_fill((start-600+i*60)*1000, "X", 1, -.009, 60, "sl")
               for i in range(2)]
        new = [hist_fill(start+i*60, "X", 1, .004, 60, "tp")
               for i in range(5)]
        rows = [*old, *new]
        self.assertEqual(last_n_chrono(rows, 5), new)
        ok, _, windows = b._live_windows_ok(rows, minimum_pf=1.1)
        self.assertTrue(ok)
        self.assertAlmostEqual(windows["overall"]["pf"], 1.3, places=12)
        same = dict(new[0], t=start*1000)
        self.assertEqual(hist_row_key(new[0]), hist_row_key(same))
        self.assertAlmostEqual(trades_per_hour([new[0], dict(new[-1], t=(start+240)*1000)]), 30.)

    def test_zero_pf_is_preserved_in_live_windows_optimization_and_submission(self):
        b=SetBook()
        b.load(dict(positionCostPct=.1,baseEvalPosCount=5,mainEvalPosCount=5,
            realEvalPosCount=5,setMinSamples=5,setMinStep=7,setStepMax=7,
            stratTrailing=False,stratGeneral=False,slToTpRatios=[.6],microEnabled=False))
        b.progress.ready=True
        st=b.by_idx[0]
        # Nine fee units of gross loss plus one fee unit produces PF exactly0.
        rows=[dict(t=1000+i*60,symbol="X",side="LONG",pnl_pct=-.009,
            qty=1.,entry=100.,pnl=-1.,position_cost_pct=.1,
            exchange_confirmed=True,partial=False,ours=True,strategy="core",
            set_id=st.id,set_tp_pct=st.tp_pct,step=st.step,hold_s=60,
            client_id=f"own-{i}",close_fill_id=f"fill-{i}",reason="sl") for i in range(6)]
        ok,reason,windows=b._live_windows_ok(rows)
        self.assertFalse(ok)
        self.assertAlmostEqual(windows["overall"]["pf"],0., places=12)
        b.adapt_from_live(rows)
        self.assertAlmostEqual(b.optimization_stats["pf"],0., places=12)
        st.hist=[hist_fill(1000+i*60,"X",1,-.009,60,"sl") for i in range(6)]
        st.live=rows
        b._score_pair((st,None))
        self.assertAlmostEqual(st.last15_ratio,0., places=12)
        self.assertEqual(st.live_eval["optimizationPf"],0.)
        self.assertFalse(st.stage_ledger["base"])
        self.assertFalse(b.execution_allowed(st,"indications","LONG"))


if __name__ == "__main__":unittest.main()
