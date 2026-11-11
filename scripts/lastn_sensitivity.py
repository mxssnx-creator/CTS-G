#!/usr/bin/env python3
"""Native frozen-market sensitivity; no venue writes and no automatic preset promotion.

Every trial starts with fresh stage state. Only Last-N settings differ; no saved
execution ledger is filtered after the fact. Negative recovery is the existing
same-direction G lane, not an inverse-profit estimate.
"""
import argparse, copy, hashlib, json, os, sys, time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE));sys.path.insert(0,str(HERE.parent/'server/pulse'))
import stage_eval as sg
import replay_cache
from coord_engine import Coordinator
from micro_short_study import metric
from micro_feasibility_study import no_network, drawdown

CONTEXT=None
TRIPLES=[(1,1,1),(3,1,1),(5,3,1),(5,3,3),(10,5,3),(10,5,5),(20,10,8),(30,15,12),(50,25,15),(80,40,20)]
MODES=[('positive',False,[],None),('negative-1-3',True,[1,3],None),('negative-5-10',True,[5,10],None),
       ('veto-1',False,[],{'windows':[1],'mode':'any','maxPf':1}),
       ('veto-3-5',False,[],{'windows':[3,5],'mode':'all','maxPf':1})]

def trial(spec):
    name,ov,names,bars,times,shadow,hours,out=CONTEXT
    bn,mn,rn,mode,negative,neg,loss=spec
    begun=time.perf_counter();b=sg.make_book()
    b.by_idx=[s for s in b.by_idx if (b.pack_ranges.get(s.pack) or {}).get('lanePolicy',{}).get('role')=='independent']
    b.sets={s.id:s for s in b.by_idx};b.packs=sorted({s.pack for s in b.by_idx})
    # Base tapes are immutable; each trial owns a cold book and its own processing policy.
    for pack in b.packs:
        p=b.pack_ranges[pack]['lanePolicy']
        p['lastN']={'baseLong':bn,'baseShort':0,'mainLong':mn,'mainShort':[1],
                    'realN':rn,'realNegN':rn,'negWindows':neg or [1]}
        p['negativeFilter']=loss
    b.neg_on=negative;b.gate_min=1;b.short_on=False
    tapes=sg.Tapes(b,names,times,shadow)
    parity,bad=sg.self_check(b,tapes,hours,samples=4)
    if bad:raise AssertionError((spec,bad))
    co=Coordinator();co.load({},sg.replay_overlay())
    rows=sg.execute(b,names,bars,times,shadow,tapes,hours,refresh_s=0,include_open=True,axis_coord=co)
    settled=sorted([r for r in rows if 'net' in r],key=lambda r:(r['t'],r['symbol']))
    held=[r for r in rows if r.get('open_at_end')]
    trades=[{k:r[k] for k in ['t','t_in','symbol','set','pack','kind','lane','reason','net']}|
      {'side':r['tr']['dir'],'entry':r['tr']['entry_px'],'exit':r['tr']['exit_px'],
       'slRatio':b.sets[r['set']].sl_ratio,'step':b.sets[r['set']].step} for r in settled]
    stages=[]
    for at in hours:
        bp=mp=rp=0;gp=gl=0.
        for st in b.by_idx:
            if sg.gate_base(b,tapes,st,at)[0]:
                bp+=1;e=sg.base_eval(b,tapes,st,at);gp+=e['gp'];gl+=e['gl']
                ch,net=sg.chain_at(b,tapes,st,at)
                v=sg.se.stage_verdicts([net[k] for k in ch['main']],[net[k] for k in ch['real']],
                    [net[k] for k in ch['realneg']],ch['short_n'],b.stage_params(st.pack,st))
                mp+=bool(v['main']);rp+=bool(v['pos'] or v['negl'])
        stages.append({'t':at,'evaluated':len(b.by_idx),'basePassed':bp,'validatedPercent':100*bp/len(b.by_idx),
          'selectedWindowPf':gp/gl if gl else None,'noLoss':bool(gp and not gl),'mainPassed':mp,'executable':rp,
          'settled':metric([r['net'] for r in trades if at<r['t']<=at+3600])})
    halves=[metric([r['net'] for r in trades if (r['t']<=hours[12])==first]) for first in [True,False]]
    result={'id':f'{name}-b{bn}-m{mn}-r{rn}-{mode}','range':name,'baseN':bn,'mainN':mn,'realN':rn,'mode':mode,
      'negativeWindows':neg,'negativeFilter':loss,'sets':len(b.by_idx),'closed':metric([r['net'] for r in trades]),
      'halves':halves,'held':len(held),'marked':metric([r['net'] for r in trades]+[r['mark_net'] for r in held]),
      'unitDrawdownPercent':drawdown([r['net'] for r in trades]),'stages':stages,'trades':trades,
      'parity':{'checks':parity,'mismatches':bad},'seconds':time.perf_counter()-begun}
    result['passesScreen']=all(h['n']>=10 and (h['noLoss'] or (h['pf'] is not None and h['pf']>1)) for h in halves)
    Path(out,result['id']+'.json').write_text(json.dumps(result,indent=2,allow_nan=False))
    return {k:v for k,v in result.items() if k not in ('trades','stages')}

def main():
    global CONTEXT
    ap=argparse.ArgumentParser();ap.add_argument('--market',required=True);ap.add_argument('--source-protocol',required=True)
    ap.add_argument('--out',required=True);ap.add_argument('--jobs',type=int,default=3);args=ap.parse_args()
    sys.addaudithook(no_network);out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    original=json.loads(Path(args.source_protocol).read_text())
    names,bars,times=sg.load_bars(args.market,12);start=times[names[0]][0]
    assert all(times[n]==times[names[0]] and len(times[n])==4320 for n in names)
    hours=list(range(start+86400,start+172800,3600))
    protocol={'symbols':names,'prehistory':[start,hours[0]],'trade':[hours[0],hours[-1]+3600],
      'triples':TRIPLES,'negativeCases':MODES,'selection':'Both 12h halves PF>1 and >=10 settled parents each; retrospective screen only',
      'sourceHash':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'costs':original['fees'],
      'priorDataOverlap':True,'nativePhysicalConstraint':'One physical position per symbol, unchanged',
      'strictFullWindows':True,'mainShortSelection':False,'baseSecondaryWindow':0,
      'negativeMeaning':'G existing same-direction recovery lane, independently positive Real-negative validation; recent-loss veto cases separate'}
    (out/'protocol.json').write_text(json.dumps(protocol,indent=2));results=[]
    for plan in original['plans']:
        ov=plan['overlay'];os.environ['STAGE_OVERLAY_PATCH']=json.dumps(ov)
        fp=replay_cache.fingerprint(args.market,names,sg.replay_overlay(),sg.PULSE,sg.__file__)
        cache=str(out/(plan['range']+'.pkl'))
        print('BUILD',plan['range'],flush=True)
        shadow=replay_cache.load(cache,fp) if Path(cache).exists() else sg.build(names,bars,times,args.jobs)
        if not Path(cache).exists():replay_cache.save(cache,shadow,fp)
        CONTEXT=(plan['range'],ov,names,bars,times,shadow,hours,str(out))
        specs=[(bn,mn,rn,*m) for bn,mn,rn in TRIPLES for m in MODES]
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            for result in pool.map(trial,specs):
                results.append(result);(out/'summary.json').write_text(json.dumps(results,indent=2,allow_nan=False))
                print(json.dumps(result),flush=True)
    print('COMPLETE',len(results),'eligible',sum(r['passesScreen'] for r in results),flush=True)
if __name__=='__main__':main()
