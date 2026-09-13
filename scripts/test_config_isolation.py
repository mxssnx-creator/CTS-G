"""Per-configuration evidence, sequential gates and unchanged-input reuse."""
import pathlib
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server/pulse'))
from set_engine import SetBook, SetState
from position_cost import last_n_cost_pf
from set_overview import build_overview, qualified_strategy_results
from pulse_trader import Pulse


def state(index=0, **kw):
    return SetState(**dict(dict(id=f'config-{index}', pack='general', tf='1m',
        sl_ratio=.6, trail_key='', trail_arm=0, trail_give=0, tp_pct=.004, idx=index), **kw))


def tape(pf=1.2, n=75, **kw):
    gross = .001 * (1 + (pf - 1) / .1)
    return [dict(dict(t=1_800_000_000+i*60, symbol='X-USDT', side='LONG',
        pnl_pct=gross, hold_s=60, reason='tp'), **kw) for i in range(n)]


def book(*states):
    b = SetBook(); b.by_idx=list(states); b.sets={s.id:s for s in states}
    b.progress.ready=True
    return b


class ConfigIsolationTests(unittest.TestCase):
    def test_indication_aggregate_cannot_override_selected_config(self):
        p=Pulse.__new__(Pulse); p.px={'X-USDT':100};p.strat_ind=True
        p.execution_lane_key=lambda *a:'own';p.occupying=lambda *a,**k:False
        p.indications=NS(settings={'enabled':True},match=lambda *a:NS(direction='long',kind='trend'))
        p.sets=NS(enabled=True,use_historic_gate=True,progress=NS(ready=True),
                  indication_ok=lambda *a:False,execution_allowed=lambda *a:True)
        own=state(pack='indications')
        self.assertIsNone(p.entry_sense('X-USDT',1,'ind:trend',.8,'indications',own))
        p.sets.indication_ok=lambda *a:True
        p.sets.execution_allowed=lambda *a:False
        self.assertEqual(p.entry_sense('X-USDT',1,'ind:trend',.8,'indications',own),'set-gate')

    def test_dca_uses_own_evidence_without_global_deactivation(self):
        from dca_engine import DcaBook
        dca=DcaBook();dca.closes=tape(.8);dca.score()
        self.assertFalse(dca.active)
        self.assertTrue(dca.score(tape(1.2))['active'])
        self.assertFalse(dca.active)  # Own decision did not rewrite overall stats.
        dca.closes=tape(1.8);dca.score()
        self.assertTrue(dca.active)
        self.assertFalse(dca.score(tape(.8))['active'])

    def test_all_last_counts_and_ranges_have_independent_threshold_results(self):
        # 240 separately evaluated configurations; the weaker neighbor always
        # stays below Base despite the stronger neighbor and combined average.
        for n in range(5,76,5):
            for sl in (.2,.4,.6,.8):
                for trailing in (False,True):
                    with self.subTest(n=n,sl=sl,trailing=trailing):
                        good=state(0,sl_ratio=sl,kind='trail' if trailing else 'base',
                                   trail_key='.3:.1' if trailing else '',hist=tape(1.021))
                        bad=state(1,sl_ratio=sl,hist=tape(1.019))
                        b=book(good,bad); b.pf_n=b.min_samples=n
                        b.deact_n=5*(1+(n//5)%5); b.max_dd_s=(1+2*(n//5%5))*3600
                        for st in (good,bad):b._score_pair((st,None))
                        self.assertTrue(good.stage_ledger['real'])
                        self.assertFalse(bad.stage_ledger['base'])
                        self.assertEqual(bad.main_pf,0)
                        self.assertEqual(good.stage_ledger['baseN'],n)
                        self.assertEqual(b.qualified_stage_ids('base'),[good.id])
                        self.assertEqual(b.entry_sets('general','LONG'),[good])

    def test_below_base_skips_downstream_but_new_own_evidence_reactivates(self):
        st=state(hist=tape(1.02,n=30));b=book(st)
        with patch.object(b,'_window_cost_pf',wraps=b._window_cost_pf) as calc:
            m=b._score_metrics(st.hist)
            self.assertEqual(calc.call_count,1)
        self.assertEqual(m['evaluation_windows'],{})
        self.assertEqual((m['main_n'],m['real_n']),(0,0))
        b._score_pair((st,None)); b._score_pair((st,None))
        self.assertEqual((b.score_completed,b.score_reused),(1,1))
        st.hist=tape(1.2,n=30)
        b._score_pair((st,None))
        self.assertTrue(st.stage_ledger['real'])

    def test_stages_count_their_own_samples_and_have_unique_records(self):
        st=state(hist=tape(1.2,n=30));b=book(st);b._score_pair((st,None))
        records=[b.stage_record(st,name) for name in ('base','main','real')]
        self.assertEqual([r.sample_count for r in records],[30,5,3])
        self.assertEqual([r.required_samples for r in records],[30,5,3])
        self.assertEqual(len({r.dedupe_key for r in records}),3)
        self.assertTrue(all(r.confidence==1 and not r.insufficient_sample for r in records))
        flow=b.stage_flow()['stages']
        self.assertEqual([flow[n]['sampleCount'] for n in ('Base','Main','Real')],[30,5,3])

    def test_live_source_change_invalidates_equal_combined_tape(self):
        st=state(hist=tape(n=30));b=book(st)
        b._score_pair((st,None));st.live=[st.hist.pop()]
        b._score_pair((st,None))
        self.assertEqual(b.score_completed,2)
        self.assertEqual(st.live_eval['n'],1)

    def test_live_trailing_close_never_credits_normal_parent(self):
        parent=state(0,hist=tape());child=state(1,kind='trail',trail_key='0.3:0.1',parent_set_id=parent.id)
        b=book(parent,child);b._score_pair((parent,None))
        rec=dict(tape(n=1)[0],set_id=parent.id,trail_set_id=child.id,
                 client_id='own',close_fill_id='fill',strategy='core')
        b.on_live_close(rec);b.on_live_close(rec)
        self.assertEqual(len(parent.live),0)
        self.assertEqual(len(child.live),1)
        self.assertEqual(child.live[0]['set_id'],child.id)
        self.assertEqual(parent.base_pf,1.2)

    def test_additional_strategies_and_different_ranges_cannot_lift_normal_pf(self):
        st=state(hist=tape(1.01,n=30));b=book(st)
        st.live=tape(1.8,strategy='block')+tape(1.8,strategy='dca')
        st.live+=tape(1.8,tp_pct=.008)+tape(1.8,set_id='other-config')
        b._score_pair((st,None))
        self.assertFalse(st.stage_ledger['base'])
        self.assertEqual(st.live_eval['n'],0)
        self.assertEqual(len(st.live),300)  # Actual accounting is retained.

    def test_system_statistics_use_only_qualified_sets_and_keep_exchange_losses(self):
        good=state(0,hist=tape(1.2));bad=state(1,hist=tape(.8))
        bad.live=tape(.8,n=3,exchange_confirmed=True,strategy='core')
        b=book(good,bad)
        for st in (good,bad):b._score_pair((st,None))
        overview=build_overview(b)
        self.assertEqual({r['setId'] for r in overview['rows'] if r['scope']=='system'},{good.id})
        self.assertEqual({r['setId'] for r in overview['rows'] if r['scope']=='exchange'},{bad.id})
        self.assertEqual(overview['selection']['excludedSets'],1)
        self.assertEqual([r['id'] for r in b.snapshot()['rows']],[good.id])

    def test_strategy_stats_partition_ranges_and_reuse_unchanged_evidence(self):
        b=book()
        b.strategy_hist['block']=tape(1.2,set_id='base',tp_pct=.004,sl_ratio=.6)
        b.strategy_hist['block']+=tape(.8,set_id='base',tp_pct=.008,sl_ratio=.4)
        rows=qualified_strategy_results(b)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0][0]['tpPct'],.4)
        with patch.object(b,'_score_metrics',side_effect=AssertionError('unchanged evidence')):
            self.assertEqual(len(qualified_strategy_results(b)),1)

    def test_live_min_pf_cannot_borrow_other_sets_strategies_or_partial_counts(self):
        p=Pulse.__new__(Pulse)
        rows=[]
        for sid,strategy,pf in [('own','block',.8),('neighbor','block',1.8),('own','core',1.8)]:
            for i,row in enumerate(tape(pf,n=8)):
                rows.append(NS(**dict(row,set_id=sid,strategy=strategy,client_id=f'{sid}-{strategy}-{i}',
                    qty=1,entry=100,pnl=(pf-1),exchange_confirmed=True,partial=False)))
        first = vars(rows[0]).copy()
        rows[0] = NS(**dict(first, qty=.4, pnl=first['pnl']*.4, partial=True, close_fill_id='partial'))
        final = NS(**dict(first, qty=.6, pnl=first['pnl']*.6, t=first['t']+1, close_fill_id='final'))
        rows.extend([final, final])
        p.strategy_closes=lambda:rows
        own=p.config_strategy_closes('own','LONG',strategy='block')
        self.assertEqual(len(own),8)
        self.assertAlmostEqual(last_n_cost_pf(own,8,.1)['ratio'],.8)
        self.assertGreater(last_n_cost_pf(rows,8,.1)['ratio'],1.5)


if __name__=='__main__':unittest.main()
