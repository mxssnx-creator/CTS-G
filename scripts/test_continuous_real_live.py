"""Real admission, ongoing evidence, and large execution-book regressions."""
import sys
import pathlib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_all_valid_entries import AllValidEntries, pt
from set_engine import SetBook
from position_cost import shared_pf_settings


class ContinuousTests(AllValidEntries):
    # Reuse the account/exchange harness, without repeating its inherited tests.
    @staticmethod
    def tape(n=30):
        return [dict(t=1000+i*60, symbol='X-USDT', side='LONG',
                     pnl_pct=.004, qty=1., entry=100., pnl=.3, hold_s=60)
                for i in range(n)]

    def test_new_defaults_and_unified_pf(self):
        b=SetBook(); b.load({'minPf':1.15,'baseMinPf':1.3,'setMinPf':1.25},rebuild=False)
        self.assertEqual(b.lookback,2880)
        self.assertEqual(b.pf_n,30)
        self.assertEqual(b.eval_need(),30)
        self.assertEqual(set(b.stage_min_pf.values()),{1.15})
        self.assertEqual(b.min_pf,1.15)
        for n in range(5,76,5):
            b.load({'baseEvalPosCount':n,'setDeactN':5},rebuild=False)
            self.assertEqual(b.pf_n,n);self.assertEqual(b.deact_n,5)

    def test_first_live_close_preserves_base_evidence(self):
        b=self.book(1);b.pf_n=b.min_samples=30
        st=b.by_idx[0];st.hist=self.tape()
        b._score_one(st)
        self.assertTrue(st.by_side['LONG']['active'])
        st.live=[dict(self.tape(1)[0],t=4000)]
        b._score_one(st)
        self.assertEqual(st.by_side['LONG']['last15_n'],30)
        self.assertTrue(st.by_side['LONG']['active'])
        self.assertEqual([st],b.entry_sets('general','LONG'))

    def test_base_requires_30_and_strictly_above_105(self):
        b=self.book(1);b.pf_n=b.min_samples=30;st=b.by_idx[0]
        st.hist=self.tape(29);b._score_one(st)
        self.assertFalse(st.stage_ledger['base'])
        st.hist=self.tape();b._score_one(st)
        self.assertTrue(st.stage_ledger['real'])
        view=dict(st.by_side['LONG'],base_pf=1.05)
        self.assertFalse(b._real_metrics_ok(view))
        view.update(base_pf=1.3,main_pf=1.04)
        self.assertFalse(b._real_metrics_ok(view))

    def test_identical_evidence_reuses_score_changed_content_rescores(self):
        b=self.book(1);st=b.by_idx[0];st.hist=self.tape()
        b._score_pair((st,None));b._score_pair((st,None))
        self.assertEqual(b.score_completed,1);self.assertEqual(b.score_reused,1)
        st.hist[-1]=dict(st.hist[-1],pnl_pct=-.02,pnl=-2.1)
        b._score_pair((st,None))
        self.assertEqual(b.score_completed,2)
        b.min_pf=1.3;b._score_pair((st,None))
        self.assertEqual(b.score_completed,3)

    def test_valid_direction_counts_survive_opposite_losses(self):
        b=self.book(1);b.pf_n=b.min_samples=30;st=b.by_idx[0]
        st.hist=self.tape()+[dict(r,side='SHORT',pnl_pct=-.02,pnl=-2.1,t=r['t']+1) for r in self.tape()]
        b._score_pair((st,None))
        self.assertTrue(st.stage_ledger['real'])
        self.assertEqual(st.stage_ledger['evaluationDirection'],'LONG')
        self.assertEqual(b.qualified_stage_ids('real'),[st.id])
        self.assertEqual(b.entry_sets('general','LONG'),[st])
        self.assertEqual(b.entry_sets('general','SHORT'),[])

    def test_deactivation_uses_exact_window(self):
        b=self.book(1);b.deact_n=5;st=b.by_idx[0]
        st.live=[dict(row,pnl=-1.,pnl_pct=-.009) for row in self.tape(4)]
        self.assertTrue(b._live_entry_allowed(st,'LONG'))
        st.live.append(dict(st.live[-1],t=5000))
        self.assertFalse(b._live_entry_allowed(st,'LONG'))

    def test_700_qualified_sets_open_exactly_once_and_keep_controls(self):
        b=self.book(700);b.pf_n=b.min_samples=30
        for i,st in enumerate(b.by_idx):
            st.hist=self.tape()
            if i%2:
                st.kind='trail';st.trail_key='0.3:0.1';st.trail_arm=.3;st.trail_give=.1
            b._score_pair((st,None))
        self.assertEqual(len(b.entry_sets('general','LONG')),700)
        p=self.pulse(b);p.strat_trail=True
        with patch.object(pt,'SYMBOLS',['X-USDT']):
            for _ in range(150):
                p.maybe_entries()
                if len(p.open)==700:break
            self.assertEqual(len(p.open),700,p.last_error)
            for _ in range(5):p.maybe_entries()
        self.assertEqual(p._entry_queue["eligible"],700)
        self.assertEqual(p._entry_queue["opened"],700)
        self.assertEqual(p._entry_queue["remaining"],0)
        self.assertEqual(p._entry_queue["pending"],0)
        self.assertEqual(len(p.api.posts),700)
        self.assertEqual(len({q.set_id for q in p.open.values()}),700)
        self.assertEqual(len(p.api.orders),1400)
        for q in p.open.values():
            self.assertEqual(float(p.api.orders[q.sl_oid]['quantity']),q.qty)
            self.assertEqual(float(p.api.orders[q.tp_oid]['quantity']),q.qty)
        self.assertEqual(p.errors,0,p.last_error)


if __name__=='__main__':
    names=[name for name in ContinuousTests.__dict__ if name.startswith('test_')]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(ContinuousTests(n) for n in names))
    sys.exit(not result.wasSuccessful())
