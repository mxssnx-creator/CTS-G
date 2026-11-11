"""Independent count and provenance oracles for the complete replay report."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'server/pulse'), str(ROOT / 'scripts'), str(ROOT / 'scripts/regression')]
from common import overlay
from set_engine import SetBook
import stage_eval as sg
import replay_cache
from fee_policy import cost_policy, paper_cost_pct


class AuditReporting(unittest.TestCase):
    def test_block_comparisons_change_real_count_ratios_and_cover_both_stack_types(self):
        from complete_strategy_audit import block_arms
        from block_rules import clean_cfg
        arms=dict(block_arms({}))
        self.assertEqual(len(arms),25)
        for relation in ['set','symbol','indication','step']:
            for ratio in [.1,.2,.5]:
                additive=clean_cfg(arms[f'{relation} ratio {ratio}'])
                shared=clean_cfg(arms[f'{relation} shared total {ratio}'])
                self.assertEqual(additive.relations,(relation,))
                self.assertEqual([c['ratio']for c in additive.counts],[ratio]*3)
                self.assertEqual(additive.stack_type,'additive')
                self.assertEqual(shared.stack_type,'shared')
                self.assertEqual(shared.shared_total,ratio)
    def test_optional_numpy_fallback_keeps_first_fill_and_excludes_exit_boundary(self):
        from stack_eval import plain_first_hit
        high,low=[100,101,103,104],[99,98,97,96]
        self.assertEqual(plain_first_hit(high,low,1,3,103,1),2)
        self.assertEqual(plain_first_hit(high,low,1,2,103,1),-1)
        self.assertEqual(plain_first_hit(high,low,1,4,97,-1),2)
        self.assertEqual(plain_first_hit(high,low,3,3,97,-1),-1)
    def test_bounded_future_entry_merge_matches_full_sort_at_ties_and_cutoffs(self):
        from trade_sim import FutureEntryTapes
        names=['B','A'];times={s:[i*60 for i in range(80)]for s in names}
        shadow={s:(0,{}, {},{0:([1,3,5,20,40],[2,4,7,22,42],[.01+j*.001+i for i in range(5)])})for j,s in enumerate(names)}
        bounded=FutureEntryTapes(names,times,shadow,.00176)
        rows=sorted((times[s][ent]+60,s,i,shadow[s][3][0][2][i])for s in names for i,ent in enumerate(shadow[s][3][0][0]))
        for now in [0,120,121,240,300,1260,3000]:
            for count in [0,1,3,8,50]:
                expected=[row[3]-.00176 for row in rows if row[0]>=now][:count]
                self.assertEqual(bounded.window(0,now,count),expected)
        self.assertFalse(hasattr(bounded,'cache'),'does not retain a complete future tape per Set')
    def test_strategy_hours_match_parent_settlement_at_every_boundary(self):
        from stack_eval import summarize, pf
        self.assertEqual(pf([0.0,0.0]),1.0)
        self.assertIsNone(pf([]))
        from trade_sim import settlement_hours
        clocks=[3600.0,3660.0,7200.0,10800.0]
        vals=[.01,-.02,.03,-.04];hours=[0.0,3600.0,7200.0]
        events=list(zip(clocks,vals,[1.0]*4))
        strategy=summarize(events,4,0,hours)
        parent=settlement_hours([dict(t=t,t_in=0,net=v)for t,v in zip(clocks,vals)],[],hours)
        self.assertEqual([r['net_pct']for r in strategy['hourly']],[r['net_pct']for r in parent])
        self.assertEqual([r['fills']for r in strategy['hourly']],[r['orders']for r in parent])
        self.assertAlmostEqual(sum(r['net_pct']for r in strategy['hourly']),strategy['net_pct'])
        # Equal-clock results keep source order, not their numerical value.
        self.assertEqual(summarize([(1,.02,1),(1,-.01,1)],2,0,[0])['max_dd_pct'],1.0)
    def test_block_add_cannot_use_newly_settled_same_clock_observations(self):
        from block_coordination_study import simulate
        rows=[dict(t_in=0,t=100,net=.03,symbol='S'+str(i),set='same',pack='p',kind='base',lane='pos',side=1,step=3,range='micro',entry=100,exit=103.176,reason='tp',honor_tp=True,tp_costs=10,sl_costs=10,extremes=[(10,3,True,1.01)])for i in range(8)]
        rows.append({**rows[0],'symbol':'TARGET','t_in':100,'t':200,'extremes':[(100,3,True,1.01)]})
        cfg=dict(blockEnabled=True,blockRelations=['step'],blockMinSamples=8,
                 blockVolumeRatio=.2,blockGuard=False,blockTargetShare=0,blockKinds=['base'])
        self.assertEqual(simulate(dict(meta=dict(cost_frac=.00176),parents=rows),cfg)['adds'],0)
    def test_shadow_migration_accepts_only_the_known_scoring_edit(self):
        from migrate_clock_order_cache import prove_edit
        before='class SetBook:\n def _streams(self, evidence, upper):\n  records = sorted((t, e, value) for t, e, value in zip(*evidence) if t <= upper)\n  return records\n'
        after=before.replace('sorted((t, e, value) for t, e, value in zip(*evidence) if t <= upper)',
            'sorted(((t, e, value) for t, e, value in zip(*evidence) if t <= upper), key=lambda row: row[0])')
        prove_edit(before,after)
        with self.assertRaises(ValueError):prove_edit(before,after.replace('return records','return []'))
    def test_main_short_preserves_tape_order_at_tied_closes(self):
        from unittest.mock import patch
        import set_engine as se
        book=SetBook();book.load(overlay(mainGateEnabled=True,mainShortEnabled=True))
        st=book.by_idx[0]
        st.proc_replayed=True;st.proc_total=3
        st.proc=[(1000,.02),(1000,-.02),(1100,.01)]
        st.exe=[];st.neg=[];st.main_short_win=5
        st.main_evidence=([1000,1000,1100],[900,800,950],[.02,-.02,.01])
        seen=[];original=se._main_windows
        def observe(ts,nets,entries,params):
            seen.append((list(ts),list(nets),list(entries)))
            return original(ts,nets,entries,params)
        with patch.object(se,'_main_windows',side_effect=observe):
            book._streams(st,now=1200,before=True)
        self.assertEqual(seen[0],([1000,1000,1100],[.02,-.02,.01],[900,800,950]))
    def test_add_return_matches_independent_quantity_cash_math(self):
        from stack_eval import add_notional_weight
        for side, add, close in [(1,90,110),(-1,110,90)]:
            parent_entry, multiple, cost = 100, .5, .00176
            unit = (close/add-1)*side-cost
            contribution = unit*add_notional_weight(parent_entry,add,multiple)
            cash = multiple*(close-add)*side-multiple*add*cost
            self.assertAlmostEqual(contribution,cash/parent_entry,places=14)
    def test_optimized_aggregation_and_atr_equal_plain_arithmetic(self):
        import indication_extra as ie
        from common import synth_bars
        rows = synth_bars(91, 900)
        for n in [1, 7, 14, 15, 16, 60, 300, 899]:
            for period in [1, 3, 14, 21]:
                used = rows[:n]
                plain = ie._true_ranges(used)[-period:]
                self.assertEqual(ie._atr(used,period),sum(plain)/len(plain))
            for tf in [1,5,15,30]:
                self.assertEqual(ie._agg(rows[:n],tf),ie._aggregate_rows(rows[:n],tf))
        before = ie._agg(rows,15)
        changed = [list(row)for row in rows];changed[450][1]*=2
        self.assertNotEqual(ie._agg(changed,15),before,'interior candle correction invalidates memo')
        self.assertLessEqual(len(ie._aggregate_local.cache),16)

    def test_rounds_cannot_learn_a_close_at_the_entry_clock(self):
        from block_rounds_study import simulate
        parents=[dict(t_in=0,t=60,net=-1,symbol='A',side=1,pack='p',kind='base',step=3,lane='pos',set='s'),
                 dict(t_in=60,t=120,net=1,symbol='B',side=1,pack='p',kind='base',step=3,lane='pos',set='s')]
        cfg=dict(relation='overall',n=1,ratio=1,step=True,max_level=2,pause=0,cap=3)
        got=simulate(dict(name='same-clock',parents=parents),cfg,{})
        self.assertEqual(got['raised'],0)
        self.assertEqual(got['netWith'],0)

    def test_full_samples_are_independent_of_profit(self):
        book = SetBook()
        book.load(overlay(setGateWindow=8, setGateShortN=0, stagesEnabled=False,
                          mainGateEnabled=False, mainNegEnabled=False, realGateEnabled=False))
        st = book.by_idx[0]
        tapes = sg.Tapes.__new__(sg.Tapes)
        tapes.cf = book.cost_pct / 100
        for count, net, full, passed in [(8, -.02, True, False), (3, .02, False, True), (0, .02, False, False)]:
            clocks = [1000 + 60 * i for i in range(count)]
            tapes.by_set = {st.idx: (clocks, [t-60 for t in clocks], [net+tapes.cf]*count)}
            readiness = {}
            windows = sg.window_figures(book, tapes, st, 2000, samples=readiness)
            self.assertEqual(readiness['baseLong'], dict(available=count, requested=8, full=full))
            self.assertEqual(windows['baseLong'][0], passed)

    def test_fee_percentage_units_have_no_fixed_cost_floor(self):
        policy = cost_policy()
        self.assertAlmostEqual(policy['slippageFeeRatio'], .1)
        self.assertAlmostEqual(paper_cost_pct(policy), .176)
        self.assertAlmostEqual(paper_cost_pct(policy, .0005, .0008), .143)
        self.assertAlmostEqual(paper_cost_pct(policy, .0005, .0005), .11)
        self.assertEqual(paper_cost_pct(cost_policy(dict(makerFeeRate=0,takerFeeRate=0))), 0)

    def test_transitive_source_changes_invalidate_replay_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); pulse = root/'server/pulse'; pulse.mkdir(parents=True)
            assets = root/'shared/cts-indications'; assets.mkdir(parents=True)
            (pulse/'engine.py').write_text('engine')
            source = assets/'formula.ts'; source.write_text('before')
            (root/'A-USDT.json').write_text('[]'); sim = root/'sim.py'; sim.write_text('sim')
            before = replay_cache.fingerprint(root, ['A-USDT'], {}, pulse, sim)
            source.write_text('after')
            after = replay_cache.fingerprint(root, ['A-USDT'], {}, pulse, sim)
            self.assertNotEqual(before, after)

    def test_stack_and_parent_replay_use_one_fee_resolved_cache(self):
        # A tiny real run proves both scripts resolve fees identically. Before
        # the fix stack_eval fingerprinted stale positionCostPct in the overlay.
        from common import synth_bars, T0
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [[int((T0+i*60)*1000), *r] for i,r in enumerate(synth_bars(7, 300))]
            (root/'A-USDT.json').write_text(json.dumps(rows))
            patch = dict(histLookbackBars=120, histLookbackAuto=False, holdEnabled=False,
                         setMinStep=2,setStepMax=2,setSlRatios=[1],stratTrailing=False,
                         stratShortRange=False,exitIgnoreTp=False)
            cache = str(root/'shadow.pkl')
            base = [sys.executable, '-B', str(ROOT/'scripts/trade_sim.py'), '--data',tmp,
                    '--cache',cache,'--symbols','1','--jobs','1','--last-hours','1','--patch',json.dumps(patch)]
            first = subprocess.run(base,capture_output=True,text=True,timeout=90)
            self.assertEqual(first.returncode,0,first.stderr[-1000:])
            second = subprocess.run([sys.executable,'-S','-B',str(ROOT/'scripts/stack_eval.py'),
                '--data',tmp,'--cache',cache,'--symbols','1','--jobs','1','--last-hours','1',
                '--part','none','--patch',json.dumps(patch),'--out',str(root/'stack.json')],
                capture_output=True,text=True,timeout=90)
            self.assertEqual(second.returncode,0,second.stderr[-1000:])


if __name__ == '__main__':
    unittest.main(verbosity=2)
