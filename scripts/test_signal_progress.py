"""Independent branch reconstruction and incremental Signals contracts."""
import copy
import json
import math
from pathlib import Path
import random
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server/pulse'))
from signal_windows import History,DEFAULT_WINDOWS,rules,policy_listing
from signal_progress import SignalProgress,distance_checks
from signal_replay import open_position,close_result

T=1791460800000
FEES={"maker":.0002,"taker":.0005,"slippagePerLeg":.0001,"entryLiquidity":"taker","exitLiquidity":"taker","source":"configured-scenario"}
P={"id":"test","kind":"normal","strategyRange":"signals","tp":.006,"sl":.005,"trailArm":0,"trailGap":0,"holdMinutes":20}

def cfg():
    return dict(base=copy.deepcopy(DEFAULT_WINDOWS),main=copy.deepcopy(DEFAULT_WINDOWS),realLastN=8,minPf=2.2,
                historyMinutes=720,maxContextDrawdown=.04,fees=copy.deepcopy(FEES))

def identity(symbol='A',side=1):
    return dict(symbol=symbol,source='ema-cross',sourceRange='short',tf=1,side=side,coordination='source')

def plain_pass(rows,t,rule,floor=2.2):
    vals=[v for clock,v in rows if t-720*60000<=clock<t]
    a,b,n,mode=rule.long,rule.short,rule.minimal,rule.polarity
    if len(vals)<a+n:return False
    def positive(v):
        g=sum(x for x in v if x>0);l=-sum(x for x in v if x<0)
        peak=total=dd=0
        for x in v:total+=x;peak=max(peak,total);dd=max(dd,peak-total)
        return g>0 and (not l or g/l>=floor) and dd<=.04
    minimal=vals[-n:]
    return (positive(minimal) if mode=='positive' else sum(minimal)<0) and positive(vals[-n-a:-n]) and positive(vals[-n-b:-n])

def scalar_path(bars,events,br,mr,side,p=P):
    """Deliberately slow normal-position oracle; no production gate/position helpers."""
    history={s:[] for s in ('base','main','real')};positions={s:None for s in history};candidates=[]
    real_n=cfg()['realLastN']
    for row in bars:
        t,o,h,l,c,_=row
        b=plain_pass(history['base'],t,br);m=plain_pass(history['main'],t,mr)
        vals=[v for end,v in history['real'] if t-720*60000<=end<t][-real_n:]
        g=sum(v for v in vals if v>0);loss=-sum(v for v in vals if v<0)
        total=peak=dd=0
        for value in vals:total+=value;peak=max(peak,total);dd=max(dd,peak-total)
        good_real=len(vals)==real_n and g>0 and (not loss or g/loss>=2.2) and dd<=.04
        if events.get(t)==side:
            if b and m and good_real:candidates.append(t)
            for stage,admit in [('base',True),('main',b),('real',b and m)]:
                if admit and positions[stage] is None:positions[stage]=(t,o*(1+side*.0001))
        for stage,pos in list(positions.items()):
            if pos is None:continue
            at,entry=pos;stop=entry*(1-side*p['sl']);target=entry*(1+side*p['tp']);exit_=None;end=t+60000
            if p['strategyRange']=='exit' and events.get(t)==-side and at<t:exit_=o;end=t
            elif side*(o-stop)<=0:exit_=o
            elif (l<=stop if side==1 else h>=stop):exit_=stop
            elif (h>=target if side==1 else l<=target):exit_=target
            elif t+60000-at>=p['holdMinutes']*60000:exit_=c
            if exit_ is not None:
                fill=exit_*(1-side*.0001);net=side*(fill-entry)/entry-.0005-fill/entry*.0005
                history[stage].append((end,net));positions[stage]=None
    return history,positions,candidates


class Windows(unittest.TestCase):
    def test_exact_cartesian_pairs_and_excluded_long5(self):
        policy=rules();self.assertEqual(len(policy),42)
        self.assertEqual({r.long for r in policy},{10,15,20})
        self.assertTrue(all(r.short<=.5*r.long for r in policy))
        self.assertEqual(len(policy_listing()['excludedPairs']),9)
        self.assertEqual({r['id'] for r in policy_listing()['rules']},{r.id for r in policy})
        self.assertEqual(len({r.id for r in policy}),42)

    def test_bad_ranges_ratio_and_duplicates_fail(self):
        for key,value in [('shortRatio',.51),('short',[4,4]),('minimal',[0]),('polarity',['inverse'])]:
            raw=copy.deepcopy(DEFAULT_WINDOWS);raw[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):rules(raw)

    def test_negative_minimal_never_changes_long_short_or_cumulative_losses(self):
        h=History(23)
        for i in range(20):h.record(i*60000,.01)
        h.record(20*60000,-.9)
        passing=h.evaluate(21*60000,720*60000,rules(),2.2,.04)
        rule=passing['l20-s10-m1-negative']
        self.assertEqual(rule['long']['loss'],0)
        self.assertEqual(rule['minimal']['loss'],.9)
        self.assertAlmostEqual(h.cumulative()['loss'],.9)
        self.assertLess(h.cumulative()['pf'],1)
        self.assertNotIn('l20-s10-m1-positive',passing)

    def test_strict_clock_expiry_and_full_windows(self):
        h=History(23)
        for i in range(11):h.record(i*60000,.01)
        self.assertEqual(h.evaluate(10*60000,720*60000,rules()),{})
        self.assertIn('l10-s4-m1-positive',h.evaluate(10*60000+1,720*60000,rules()))
        self.assertEqual(h.evaluate(900*60000,720*60000,rules()),{})
        with self.assertRaises(ValueError):h.record(10*60000,.02)

    def test_indexed_tail_matches_unbounded_scalar_all_rules(self):
        rng=random.Random(19);h=History(23);rows=[];policy=rules()
        for i in range(250):
            val=rng.choice([.01,.04,.02,-.003,-.001,0]);h.record(T+i*60000,val);rows.append((T+i*60000,val))
            for t in (T+i*60000,T+i*60000+1):
                got=h.evaluate(t,720*60000,policy)
                self.assertEqual(set(got),{r.id for r in policy if plain_pass(rows,t,r)})
        self.assertLessEqual(len(h.times),24)
        self.assertEqual(h.count,250)

    def test_empty_flat_and_no_loss_not_finite_infinity(self):
        h=History(23);self.assertIsNone(h.cumulative()['pf'])
        h.record(T,0);self.assertEqual(h.cumulative()['pf'],1)
        h.record(T+1,.1);self.assertIsNone(h.cumulative()['pf'])
        self.assertEqual(h.cumulative()['pfState'],'no-loss')


class Progress(unittest.TestCase):
    def test_default_keeps_full_windows_and_adds_main_eight(self):
        preset=json.loads((ROOT/'presets/signals-independent-windows.json').read_text())['progress']
        book=SignalProgress(preset)
        self.assertEqual(preset['realLastN'],'coordinated')
        self.assertEqual(preset['historyMinutes'],100*60)
        self.assertEqual(len(book.base_rules),42)
        self.assertEqual(len(book.main_rules),48)
        self.assertEqual(book.capacity,23)

    def test_dynamic_real_uses_each_lanes_own_minimal_count(self):
        raw=cfg();raw['realLastN']='lane-minimal';book=SignalProgress(raw)
        node=book.node();node.history.record(T,.01)
        for n in (1,2,3):
            ok,m=book.real_pass(node,T+1,f'l10-s4-m{n}-positive',None)
            self.assertEqual(ok,n==1);self.assertEqual(m['requiredN'],n)
        node.history.record(T+60000,-.001)
        ok,m=book.real_pass(node,T+60001,'l10-s4-m1-positive',None)
        self.assertFalse(ok)

    def test_coordinated_real_uses_complete_main_proof_not_duplicate_gate(self):
        raw=cfg();raw['realLastN']='coordinated';book=SignalProgress(raw)
        rid=book.register(identity(),P);root=book.roots[rid]
        rule='l10-s4-m1-positive'
        root.main[rule]=book.node();root.real[rule,rule]=book.node()
        for node in (root.base,root.main[rule]):
            for i in range(11):node.history.record(T+i*60000,.01)
        root.real[rule,rule].history.record(T,-.02)
        now=T+12*60000
        summary=book.summary(now)
        self.assertEqual(summary['stages']['real']['passed'],1)
        self.assertEqual(summary['stages']['real']['gain'],0)
        self.assertEqual(summary['stages']['real']['loss'],.02)
        self.assertEqual(summary['stages']['real']['pf'],0)
        out=book.step('A',[now,100,101,99.9,100.7,1],{book.event_key(identity()):1})
        selected=[c for c in out if c['baseRule']==rule and c['mainRule']==rule]
        self.assertEqual(len(selected),1)
        self.assertEqual(selected[0]['real']['validationStage'],'main')
        self.assertFalse(book.real_pass(book.node(),now,rule,None)[0])

    def test_real_modes_do_not_change_shadow_history_or_restart(self):
        from importlib.util import spec_from_file_location,module_from_spec
        spec=spec_from_file_location('oracle',ROOT/'scripts/signal_window_oracle.py')
        oracle=module_from_spec(spec);spec.loader.exec_module(oracle)
        bars=self.bars(120);events={r[0]:1 for r in bars}
        for mode in ('coordinated','lane-minimal',1,8):
            raw=cfg();raw['realLastN']=mode;book=SignalProgress(raw);rid=book.register(identity(),P)
            out=self.run_book(book,bars[:61],events)
            book=SignalProgress.restore(book.checkpoint())
            out+=self.run_book(book,bars[61:],events)
            for br,mr in [('l10-s4-m1-positive','l20-s10-m3-positive'),('l20-s10-m3-positive','l10-s4-m2-positive')]:
                rules_by_id={r.id:(r.long,r.short,r.minimal,r.polarity) for r in rules()}
                hist,_,_,times=oracle.reconstruct(bars,events,rules_by_id[br],rules_by_id[mr],1,P,raw)
                self.assertEqual([c['decisionT'] for c in out if c['baseRule']==br and c['mainRule']==mr],times)
                node=book.roots[rid].real.get((br,mr))
                self.assertEqual(node.history.count if node else 0,len(hist['real']))
        for mode in (0,True,'automatic',[],None):
            raw=cfg();raw['realLastN']=mode
            with self.assertRaises(ValueError):SignalProgress(raw)

    def test_added_main_eight_paths_match_scalar_for_every_base_rule(self):
        from importlib.util import spec_from_file_location,module_from_spec
        spec=spec_from_file_location('oracle',ROOT/'scripts/signal_window_oracle.py')
        oracle=module_from_spec(spec);spec.loader.exec_module(oracle)
        raw=json.loads((ROOT/'presets/signals-independent-windows.json').read_text())['progress']
        book=SignalProgress(raw);rid=book.register(identity(),P)
        bars=self.bars(120);events={r[0]:1 for r in bars}
        for i in (23,37,39,41,69,70,103):bars[i][2:5]=[100.1,99,99.4]
        out=self.run_book(book,bars,events)
        root=book.roots[rid]
        for br in book.base_rules:
            for mr in (r for r in book.main_rules if r.long==8):
                hist,_,opened,times=oracle.reconstruct(bars,events,(br.long,br.short,br.minimal,br.polarity),
                    (mr.long,mr.short,mr.minimal,mr.polarity),1,P,raw)
                for stage,node in [('main',root.main.get(br.id)),('real',root.real.get((br.id,mr.id)))]:
                    self.assertEqual(node.history.count if node else 0,len(hist[stage]))
                    self.assertAlmostEqual(node.history.cumulative()['net'] if node else 0,sum(v for _,v in hist[stage]),places=11)
                    self.assertEqual(node.opened if node else 0,opened[stage])
                self.assertEqual([c['decisionT'] for c in out if c['baseRule']==br.id and c['mainRule']==mr.id],times)

    def test_additional_low_distance_profiles_keep_both_ranges_and_cost_margin(self):
        preset=json.loads((ROOT/'presets/signals-low-distance-candidates.json').read_text())
        self.assertEqual(preset['progress']['maxContextDrawdown'],.01)
        book=SignalProgress(preset['progress'])
        for p in preset['protections']:
            book.register(identity(),p)
            self.assertIn(p['strategyRange'],('signals','exit'))
            self.assertLessEqual(p['sl'],.0045)
            self.assertGreater(distance_checks(p,FEES)['roundTripBreakEven'],.0012)
        self.assertEqual(len(book.roots),8)

    def book(self,extra=False,p=P):
        book=SignalProgress(cfg());rid=book.register(identity(),p)
        if extra:
            book.register(identity('B'),p);book.register(identity(side=-1),p)
            other=identity();other['source']='macd-cross';book.register(other,p)
        return book,rid

    def run_book(self,book,bars,events):
        key=book.event_key(identity());out=[]
        for bar in bars:
            es={key:events[bar[0]]} if bar[0] in events else {}
            out.extend(book.step('A',bar,es))
        return out

    def bars(self,n=400):
        return [[T+i*60000,100,101,99.9,100.7,1] for i in range(n)]

    def test_every_branch_is_lazy_and_exact_identity_survives(self):
        book,rid=self.book();self.assertEqual(book.roots[rid].main,{})
        bars=self.bars();events={r[0]:1 for r in bars}
        candidates=self.run_book(book,bars,events)
        root=book.roots[rid]
        self.assertEqual(len(root.main),21);self.assertEqual(len(root.real),21*21)
        self.assertGreater(len(candidates),0)
        self.assertTrue(all(c['identity']==identity() and c['protection']['strategyRange']=='signals' for c in candidates))
        summary=book.summary(T+400*60000)
        self.assertEqual(summary['possibleExecutionLanes'],1764)
        self.assertEqual(summary['stages']['real']['population'],1764)

    def test_independent_reconstruction_all_base_main_pairs(self):
        bars=self.bars(150);events={r[0]:1 for r in bars}
        for i in (23,37,39,41,69,70,103):bars[i][2:5]=[100.1,99,99.4]
        book,rid=self.book();candidates=self.run_book(book,bars,events);root=book.roots[rid]
        candidate_times={}
        for c in candidates:candidate_times.setdefault((c['baseRule'],c['mainRule']),[]).append(c['decisionT'])
        # All 1,764 paths, including those never materialized, are compared.
        for br in rules():
            for mr in rules():
                closed,positions,times=scalar_path(bars,events,br,mr,1)
                for stage,node in [('base',root.base),('main',root.main.get(br.id)),('real',root.real.get((br.id,mr.id)))]:
                    actual=node.history.cumulative() if node else {'n':0,'net':0}
                    self.assertEqual(actual['n'],len(closed[stage]),(br.id,mr.id,stage))
                    self.assertAlmostEqual(actual['net'],sum(v for _,v in closed[stage]),places=11)
                    self.assertEqual(bool(node and node.position),positions[stage] is not None)
                self.assertEqual(candidate_times.get((br.id,mr.id),[]),times)

    def test_other_sources_symbols_sides_do_not_change_existing_history(self):
        a,ra=self.book();b,rb=self.book(extra=True);bars=self.bars(85);events={r[0]:1 for r in bars}
        self.run_book(a,bars,events);self.run_book(b,bars,events)
        self.assertEqual(a.checkpoint()['roots'][0],b.checkpoint()['roots'][0])
        self.assertTrue(all(r.base.history.count==0 for k,r in b.roots.items() if k!=rb))

    def test_incremental_restart_and_duplicate_delivery(self):
        a,_=self.book();b,_=self.book();bars=self.bars(100);events={r[0]:1 for r in bars}
        expected=self.run_book(a,bars,events)
        before=self.run_book(b,bars[:51],events);b=SignalProgress.restore(json.loads(json.dumps(b.checkpoint())))
        after=self.run_book(b,bars[51:],events)
        self.assertEqual(expected,before+after);self.assertEqual(a.checkpoint(),b.checkpoint())
        self.assertEqual(b.step('A',bars[-1],{b.event_key(identity()):1}),[])
        bad=bars[-1].copy();bad[-1]=2
        with self.assertRaises(ValueError):b.step('A',bad,{b.event_key(identity()):1})

    def test_gaps_and_forged_source_events_fail(self):
        b,_=self.book();bars=self.bars(3)
        b.step('A',bars[0],{})
        with self.assertRaises(ValueError):b.step('A',bars[2],{})
        with self.assertRaises(ValueError):b.step('A',bars[1],{('A','other','short',1,'source'):1})

    def test_common_floor_and_no_ordinary_strategy_ranges(self):
        raw=cfg();raw['minPf']=2.19
        with self.assertRaises(ValueError):SignalProgress(raw)
        for name in ('micro','minimal','short','general','wide','long'):
            with self.subTest(name=name),self.assertRaises(ValueError):self.book(p={**P,'strategyRange':name})

    def test_exit_strategy_cannot_close_signals_sibling(self):
        b,rid=self.book(p={**P,'tp':.03,'sl':.03})
        eid=b.register(identity(),{**P,'tp':.03,'sl':.03,'strategyRange':'exit'})
        bars=[[T+i*60000,100,100.1,99.9,100,1] for i in range(3)]
        self.run_book(b,bars,{T:1,T+60000:-1})
        self.assertEqual(b.roots[eid].base.history.count,1)
        self.assertEqual(b.roots[rid].base.history.count,0)
        self.assertIsNotNone(b.roots[rid].base.position)

    def test_cost_aware_minimum_distances_and_five_percent_only(self):
        minimum=distance_checks(P,FEES)['roundTripBreakEven']
        self.assertGreater(minimum,.0012)
        for side in (-1,1):
            pos=open_position(T,100,side,P,FEES)
            close=close_result(pos,100*(1+side*minimum),T+60000,'test',FEES)
            self.assertGreaterEqual(close['unitNet'],-1e-12)
        for p in ({**P,'sl':.0012},{**P,'kind':'trailing-only','tp':0,'trailArm':.006,'trailGap':.005}):
            with self.assertRaises(ValueError):distance_checks(p,FEES)
        wide={**P,'kind':'trailing-only','tp':0,'trailArm':.055,'trailGap':.05}
        distance_checks(wide,FEES)
        with self.assertRaises(ValueError):distance_checks({**wide,'tp':.01},FEES)


if __name__=='__main__':unittest.main()
