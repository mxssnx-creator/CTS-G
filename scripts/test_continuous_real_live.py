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

    def test_base_requires_30_and_strictly_above_102(self):
        b=self.book(1);b.pf_n=b.min_samples=30;st=b.by_idx[0]
        st.hist=self.tape(29);b._score_one(st)
        self.assertFalse(st.stage_ledger['base'])
        st.hist=self.tape();b._score_one(st)
        self.assertTrue(st.stage_ledger['real'])
        view=dict(st.by_side['LONG'],base_pf=1.02)
        self.assertFalse(b._real_metrics_ok(view))
        view.update(base_pf=1.3,main_pf=1.01)
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

    def test_memory_trim_retains_last75_of_each_direction(self):
        from set_engine import trim_hist
        b=self.book(1);b.pf_n=b.min_samples=75;st=b.by_idx[0]
        short=[dict(r,side='SHORT') for r in self.tape(75)]
        long=[dict(r,t=r['t']+10000) for r in self.tape(200)]
        st.hist=trim_hist(short+long);st.live=list(short+long)
        b.trim_tapes(hist_cap=24,live_cap=16)
        self.assertEqual(sum(r['side']=='SHORT' for r in st.hist),75)
        self.assertGreaterEqual(sum(r['side']=='LONG' for r in st.hist),75)
        self.assertEqual(sum(r['side']=='SHORT' for r in st.live),75)
        b._score_one(st)
        self.assertTrue(st.by_side['LONG']['active'])
        self.assertTrue(st.by_side['SHORT']['active'])

    def test_middle_history_slices_publish_qualification(self):
        b=self.book(1);p=self.pulse(b);scores=[]
        p._hist_request_changed=lambda:False
        p._hist_replay_chunk_size=lambda n:1
        p._hist_peer_claim=lambda:True
        p._hist_peer_touch=lambda:None
        p._hist_peer_release=lambda:None
        p._hist_write_status=lambda *a,**k:None
        p.write_stats=lambda **k:None
        p.trim_caches=lambda **k:None
        p._hist_incremental_symbols=set()
        def publish(names, already, total, score=True, **kwargs):
            scores.append(score)
            if names==['BCH-USDT']:
                b.by_idx[0].hist=self.tape()
                if score:b._score_pair((b.by_idx[0],None))
            if names==['SOL-USDT']:
                if score:b._score_pair((b.by_idx[0],None))
                self.assertTrue(b.by_idx[0].stage_ledger['real'])
            return True
        p._replay_sets_isolated=publish
        self.assertTrue(p._hist_replay_chunked(['XRP-USDT','BCH-USDT','SOL-USDT'],False,3))
        self.assertEqual(scores,[True,False,True])

    def test_deactivation_uses_exact_window(self):
        b=self.book(1);b.deact_n=5;st=b.by_idx[0]
        st.live=[dict(row,pnl=-1.,pnl_pct=-.009) for row in self.tape(4)]
        self.assertTrue(b._live_entry_allowed(st,'LONG'))
        st.live.append(dict(st.live[-1],t=5000))
        self.assertFalse(b._live_entry_allowed(st,'LONG'))

    def test_700_qualified_sets_resume_after_rate_limit_open_once_and_keep_controls(self):
        b=self.book(700);b.pf_n=b.min_samples=30
        for i,st in enumerate(b.by_idx):
            st.hist=self.tape()
            if i%2:
                st.kind='trail';st.trail_key='0.3:0.1';st.trail_arm=.3;st.trail_give=.1
            b._score_pair((st,None))
        self.assertEqual(len(b.entry_sets('general','LONG')),700)
        p=self.pulse(b);p.strat_trail=True
        original_post = p.api.post
        ban = {'active':False, 'injected':False}
        def post(path, body):
            if len(p.api.posts) == 137 and not ban['injected']:
                ban.update(active=True, injected=True)
                return {'code':100410, 'msg':'Please try again later.'}
            return original_post(path, body)
        p.api.post = post
        p.api.order_retry_after = lambda: 480 if ban['active'] else 0
        with patch.object(pt,'SYMBOLS',['X-USDT']):
            for _ in range(150):
                p.maybe_entries()
                if ban['active']:
                    self.assertEqual(len(p.open),137)
                    self.assertEqual(p._entry_queue['remaining'],563)
                    for _ in range(8):p.maybe_entries()
                    self.assertEqual(len(p.api.posts),137)
                    self.assertFalse(p.pending_orders)
                    # Simulate expiry without waiting or sending network traffic.
                    ban['active']=False
                    p.cooldown.clear()
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
        self.assertTrue(ban['injected'])

    def test_simulated_overall_trading_progress(self):
        """Replay → score → Base/Main/Real → intern scan → entries → overall SL/TP."""
        from set_engine import synth_trend
        from combo_eval import evaluate_book
        import hist_test as ht

        book = SetBook()
        book.load({
            "histLookbackBars": 2880,
            "baseEvalPosCount": 30,
            "setMinPf": 1.1,
            "minPf": 1.1,
            "slToTpRatios": [0.6],
            "setMinStep": 8,
            "setStepMax": 8,
            "stratTrailing": False,
            "stratBlock": False,
            "stratDca": False,
            "histTestEnabled": False,
        })
        self.assertEqual(book.lookback, 2880)
        self.assertGreaterEqual(len(book.by_idx), 1)
        bars = synth_trend(220)
        for symbol in ("XRP-USDT", "BCH-USDT", "SOL-USDT"):
            book.ingest_bars(symbol, bars)
        book.replay_all(symbols=["XRP-USDT", "BCH-USDT", "SOL-USDT"], workers=1, merge=True, score=True)
        self.assertTrue(book.progress.ready)
        self.assertEqual(book.progress.phase, "ready")
        self.assertGreaterEqual(book.progress.pct, 99.0)
        qualified = [st for st in book.by_idx if (st.stage_ledger or {}).get("base")]
        self.assertTrue(qualified, "synth trend must admit at least one Base set")
        winner = qualified[0]
        led = winner.stage_ledger or {}
        self.assertGreaterEqual(int(led.get("baseN") or 0), 30)
        self.assertGreaterEqual(float(led.get("basePf") or 0), 1.1)
        # Main/Real use independent last-N windows — they must not inherit Base PF.
        if not led.get("main"):
            self.assertGreaterEqual(int(led.get("mainN") or 0), 1)
            self.assertLess(float(led.get("mainPf") or 0), 1.1)
        tape_book = self.book(1)
        tape_book.pf_n = tape_book.min_samples = 30
        st = tape_book.by_idx[0]
        st.hist = self.tape()
        tape_book._score_one(st)
        self.assertTrue(st.stage_ledger["base"])
        self.assertTrue(st.stage_ledger["main"])
        self.assertTrue(st.stage_ledger["real"])
        combo = evaluate_book(book, min_pf=1.1, cost_pct=0.1, pf_n=30)
        selected = ht.selected_coordinations({
            "successfulConfigs": combo.get("successful") or combo.get("successfulConfigs") or [],
            "combo": combo,
        })
        for row in selected:
            sid = str(row.get("id") or "")
            self.assertTrue(ht.is_coordination_set_id(sid) or ht.is_catalog_set_id(sid), sid)
            self.assertFalse(sid.startswith("block:"), sid)
            self.assertFalse(sid.startswith("dca:"), sid)
        intern = ht.intern_liquid_pool(["FLYBRAIN-USDT", "XRP-USDT"], None, cap=50)
        self.assertEqual(len(intern), 50)
        self.assertEqual(set(intern), set(ht.HIST_TEST_MAJORS))
        p = self.pulse(self.book(4))
        for st in p.sets.by_idx:
            st.hist = self.tape()
            p.sets._score_pair((st, None))
        p.overlay = {"histTestEnabled": True, "symbolCap": 50}
        p.symbol_cap = 50
        with patch.object(pt, "SYMBOLS", ["X-USDT"]):
            scan = p._intern_symbols()
            self.assertIn("X-USDT", scan)
            self.assertIn("BTC-USDT", scan)
            self.assertIn("BCH-USDT", scan)
            p.maybe_entries()
        self.assertGreaterEqual(len(p.open), 1, p.last_error)
        pos = next(iter(p.open.values()))
        self.assertTrue(pos.sl_oid and pos.tp_oid)
        self.assertEqual(pos.symbol, "X-USDT")
        flow = book.stage_flow()
        stages = flow.get("stages") or flow
        self.assertIn("Base", stages)
        self.assertGreaterEqual(int((stages.get("Base") or {}).get("qualified") or 0), 1)


if __name__=='__main__':
    names=[name for name in ContinuousTests.__dict__ if name.startswith('test_')]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(ContinuousTests(n) for n in names))
    sys.exit(not result.wasSuccessful())
