#!/usr/bin/env python3
"""Bounded, causal Micro/Short comparison through the production SetLane and stage rules.

No exchange writes. Freeze every candidate before scoring. Select on the first
24h after 24h prehistory, then evaluate only the frozen finalists on the last 24h.
Raw lane tapes may be shared only among pure admission-policy variants; prices,
signals, geometry, cost, holds and pauses are held identical within that group.
"""
from __future__ import annotations
import argparse, copy, itertools, json, math, os, resource, sys, time
from dataclasses import asdict
from pathlib import Path

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))

def metric(values):
    if not all(math.isfinite(x) for x in values):raise ValueError('unknown return in measured cohort')
    gp=sum(max(0,x) for x in values);gl=sum(max(0,-x) for x in values)
    return {'n':len(values),'pf':gp/gl if gl else (1.0 if values and not gp else None),'noLoss':bool(gp and not gl),
            'gain':gp,'loss':gl,'netPct':100*sum(values),'wins':sum(x>0 for x in values)}

def candidate_rank(row):
    def pf(m):return math.inf if m['noLoss'] else (m['pf'] if m['pf'] is not None else 0)
    enough=all(x['n']>=10 for x in row['halves'])
    score=min(pf(x) for x in row['halves'])
    return (enough and score>=1 and pf(row['marked'])>=1,enough,score,row['closed']['netPct'],row['closed']['n'])

def source_configs(range_name,lo,hi):
    names=['ao_lr3_edge-turn_standalone_t1','ao_lr6_failed-break_standalone_t1',
           'ao_lr6_breakout_trend_t1','ao_lr3_edge-turn_standalone_t5',
           'ao_lr6_failed-break_standalone_t5','ao_lr6_breakout_trend_t5',
           'breakout_retest','failed_breakout','mean_stretch_revert','pullback_trend']
    return [{'label':f'{range_name} independent source study','names':names,'relations':[''],
             'stepMin':lo,'stepMax':hi,'slRatios':[i/4 for i in range(1,11)],'types':['base','exit','trail'],
             'trailStepMin':lo,'trailStepMax':hi,'major':range_name,'minor':'causal-20261109'}]

def plans(base, micro_families=False):
    result=[]
    for name,(lo,hi) in {'micro':(2,3),'short':(7,12)}.items():
        for hold in [0,15,60]:
            for sources in ['core','extended']:
                o=copy.deepcopy(base)
                ranges={k:k==name for k in ['micro','minimal','short','normal','long','wide']}
                o.update(tpRanges=ranges,trailRanges=ranges,setMinStep=lo,setStepMax=hi,
                         shortRangeStepMin=lo,shortRangeStepMax=hi,holdAuto=False,
                         stepHoldOn=[bool(hold)]*12,stepHoldMin=[hold or 15]*12,
                         setSlRatios=[i/4 for i in range(1,11)],indicationConfigs=source_configs(name,lo,hi) if sources=='extended' else [],
                         minPfStepAuto=False,histLookbackBars=1440,histLookbackAuto=False)
                profiles=[]
                for windows,negative in itertools.product(['factory','medium','fast'],[True,False]):
                    patch={'mainNegEnabled':negative}
                    if windows!='factory':
                        bl,ml,rn=(20,10,8) if windows=='medium' else (10,5,5)
                        patch['lastNByRange']={name:{'baseLong':bl,'baseShort':bl//2,'mainLong':ml,
                            'mainShort':[max(1,ml//2)],'realN':rn,'realNegN':rn,'negWindows':[5,10]}}
                    profiles.append({'id':f'{name}-hold{hold}-{sources}-{windows}-neg{int(negative)}','patch':patch})
                result.append({'id':f'{name}-hold{hold}-{sources}','range':name,'overlay':o,'profiles':profiles})
    if not micro_families:
        return result
    followup=[]
    for name in ['micro','short']:
        core=next(g for g in result if g['id']==f'{name}-hold0-core')
        followup.append(core)
        group=copy.deepcopy(core);group['id']=f'{name}-hold0-microfamily'
        configs=source_configs(name,2 if name=='micro' else 7,3 if name=='micro' else 12)
        configs[0]['label']=f'{name} Micro family follow-up'
        configs[0]['names']=[f'{f}_n5_t1' for f in ['sweep','wickrev','burstfade','exhaust','volclimax','burstgo','momchg','vwapx','mpull']]
        related=copy.deepcopy(configs[0]);related.update(label=f'{name} Micro trend/break follow-up',
            names=['momchg_n5_t1','mpull_n5_t1'],relations=['T5','B5'])
        group['overlay']['indicationConfigs']=configs+[related]
        for profile in group['profiles']:profile['id']=profile['id'].replace('-core-','-microfamily-')
        followup.append(group)
    return followup

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--data',required=True);ap.add_argument('--base',required=True)
    ap.add_argument('--out',required=True);ap.add_argument('--jobs',type=int,default=3);ap.add_argument('--group')
    ap.add_argument('--finalists',action='store_true')
    mode=ap.add_mutually_exclusive_group()
    mode.add_argument('--micro-families',action='store_true')
    mode.add_argument('--include-micro-families',action='store_true')
    args=ap.parse_args()
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    data=Path(args.data);base=json.loads(Path(args.base).read_text());groups=plans(base,args.micro_families)
    if args.include_micro_families:groups += [g for g in plans(base,True) if g['id'].endswith('-microfamily')]
    import stage_eval as sg, replay_cache
    from coord_engine import Coordinator
    names,bars,times=sg.load_bars(str(data),12)
    assert len(names)==12 and all(len(times[n])==4320 for n in names),'exactly 12 symbols and 72h required'
    assert all(times[n]==times[names[0]] for n in names),'aligned symbols required'
    assert all(b-a==60 for a,b in zip(times[names[0]],times[names[0]][1:])), 'contiguous minute data required'
    start=times[names[0]][0];split=start+48*3600;end=start+72*3600
    hours=list(range(start+24*3600,split,3600))
    protocol={'createdAt':time.time(),'symbols':names,'start':start,'development':[start+24*3600,split],
              'confirmation':[split,end],'selection':'highest minimum half-window closed PF, >=10 closes per development half, marked PF >=1, then net and count; otherwise diagnostic finalist only',
              'feeBasis':'configured conservative fallback fee model, not account-measured fills','groups':groups}
    p=out/'protocol.json'
    if p.exists():
        old=json.loads(p.read_text());assert all(old[k]==protocol[k] for k in protocol if k!='createdAt'),'protocol changed'
    else:p.write_text(json.dumps(protocol,indent=2))
    def run(o,shadow,run_hours,ident):
        os.environ['STAGE_OVERLAY_PATCH']=json.dumps(o)
        book=sg.make_book();tapes=sg.Tapes(book,names,times,shadow)
        got,bad=sg.self_check(book,tapes,run_hours,samples=20);assert bad==0,(ident,got,bad)
        coord=Coordinator();coord.load({},sg.replay_overlay())
        t0=time.perf_counter();rows=sg.execute(book,names,bars,times,shadow,tapes,run_hours,
                                             refresh_s=0,include_open=True,axis_coord=coord)
        closed=[r for r in rows if 'net' in r];held=[r for r in rows if r.get('open_at_end')]
        midpoint=run_hours[len(run_hours)//2]
        halves=[metric([r['net'] for r in closed if (r['t']<=midpoint)==first]) for first in [True,False]]
        snapshots=[]
        for at in [run_hours[0],run_hours[len(run_hours)//2],run_hours[-1]]:
            valid=[];ex=0;gps=gls=0
            for st in book.by_idx:
                ok,_,n,pf=sg.gate_base(book,tapes,st,float(at))
                if ok:
                    e=sg.base_eval(book,tapes,st,float(at));nets=[x-e['cf'] for x in e['w']]
                    gps+=sum(max(0,x) for x in nets);gls+=sum(max(0,-x) for x in nets);valid.append(st.id)
                ex+=bool(sg.gate_metrics(book,tapes,st,float(at))[0])
            snapshots.append({'t':at,'evaluated':len(book.by_idx),'basePassed':len(valid),'executable':ex,
                              'selectedPf':gps/gls if gls else None,'noLoss':bool(gps and not gls)})
        compact=[{k:r[k] for k in ['t','t_in','symbol','set','pack','kind','lane','reason','net']}|
                 {'side':r['tr']['dir'],'entry':r['tr']['entry_px'],'exit':r['tr']['exit_px']} for r in closed]
        rr={'id':ident,'closed':metric([r['net'] for r in closed]),'marked':metric([r['net'] for r in closed]+[r['mark_net'] for r in held]),
            'held':len(held),'halves':halves,'stageSnapshots':snapshots,'trades':compact,'open':held,'seconds':time.perf_counter()-t0,
            'engineParity':{'samples':got,'mismatches':bad},'sets':len(book.by_idx),
            'configurationCoverage':book.config_report,
            'effectiveWindows':asdict(book.win(book.by_idx[0])) if book.by_idx else None}
        (out/(ident+'.json')).write_text(json.dumps(rr,indent=2));print(json.dumps({k:rr[k] for k in ['id','closed','held','seconds']}),flush=True)
        return rr
    selected=[]
    if args.finalists:
        # Freeze the complete development choice BEFORE reading a confirmation outcome.
        decisions=[]
        for range_name in ['micro','short']:
            candidates=[]
            for g in groups:
                if g['range']!=range_name:continue
                for profile in g['profiles']:
                    r=json.loads((out/(profile['id']+'.json')).read_text())
                    candidates.append((candidate_rank(r),g,profile))
            rank,g,profile=max(candidates,key=lambda x:x[0]);decisions.append({'range':range_name,'id':profile['id'],
                'eligible':rank[0],'rank':[x if not isinstance(x,float) or math.isfinite(x) else 'no-loss' for x in rank]})
            selected.append((g,profile))
            baseline=next(x for x in groups if x['range']==range_name and x['id']==f'{range_name}-hold0-core')
            if profile['id']!=baseline['profiles'][0]['id']:selected.append((baseline,baseline['profiles'][0]))
        decision_path=out/'frozen-finalists.json'
        if decision_path.exists():assert json.loads(decision_path.read_text())==decisions
        else:decision_path.write_text(json.dumps(decisions,indent=2))
    for group in groups:
        profiles=[p for g,p in selected if g['id']==group['id']] if args.finalists else group['profiles']
        if args.group and group['id']!=args.group:continue
        if not profiles:continue
        os.environ['STAGE_OVERLAY_PATCH']=json.dumps(group['overlay'])
        book=sg.make_book();overlay=sg.replay_overlay()
        assert not book.config_report['dropped'],book.config_report['dropped']
        manifest=replay_cache.fingerprint(str(data),names,overlay,sg.PULSE,sg.__file__)
        cache=str(out/(group['id']+'.pkl'))
        shadow=replay_cache.load(cache,manifest) if Path(cache).exists() else sg.build(names,bars,times,max(1,args.jobs))
        if not Path(cache).exists():replay_cache.save(cache,shadow,manifest)
        topology=[(st.id,st.tp_pct,st.hold_bars) for st in book.by_idx]
        for profile in profiles:
            ident=profile['id']+('-confirmation' if args.finalists else '')
            if (out/(ident+'.json')).exists():continue
            o={**group['overlay'],**profile['patch']};os.environ['STAGE_OVERLAY_PATCH']=json.dumps(o)
            testbook=sg.make_book();assert topology==[(st.id,st.tp_pct,st.hold_bars) for st in testbook.by_idx]
            assert set(profile['patch']).issubset({'mainNegEnabled','lastNByRange'})
            run(o,shadow,list(range(split,end,3600)) if args.finalists else hours,ident)
        del shadow
    print(json.dumps({'rssMiB':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,'completed':True}),flush=True)

if __name__=='__main__':main()
