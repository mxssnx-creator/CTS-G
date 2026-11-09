#!/usr/bin/env python3
"""Frozen native Micro/Minimal/Short × Last-N replay. Offline, retrospective evidence."""
import argparse, copy, hashlib, json, os, sys, time
from pathlib import Path
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE));sys.path.insert(0,str(HERE.parent/'server/pulse'))
import stage_eval as sg
from lane_policy import DEFAULT_LAST_N_LANES
from coord_engine import Coordinator
from micro_short_study import metric
from micro_feasibility_study import no_network, drawdown
import replay_cache

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--market',required=True);ap.add_argument('--out',required=True);ap.add_argument('--jobs',type=int,default=3);args=ap.parse_args()
 sys.addaudithook(no_network)
 out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
 names,bars,times=sg.load_bars(args.market,12)
 assert len(names)==12 and all(len(times[n])==4320 for n in names)
 assert all(times[n]==times[names[0]] for n in names)
 start=times[names[0]][0];hours=list(range(start+86400,start+172800,3600))
 base=sg.replay_overlay();base.update(stratGeneral=False,stratIndications=False,stratShortRange=False,extraIndicators=[],
   directionMode='split',holdAuto=False,stepHoldOn=[True]*12,stepHoldMin=[15]*12,
   minPfStepAuto=False,histLookbackBars=1440,histLookbackAuto=False,mainNegEnabled=False,
   stageFullWindow=True,setGateMinTrades=10,groupGateEnabled=False,blockEnabled=False,dcaEnabled=False,
   makerFeeRate=.0002,takerFeeRate=.0005,slippageFeeRatio=.1,minSlDistancePct=.1)
 base.pop('positionCostPct',None)
 plan=[]
 for name,lo,hi in [('micro',2,3),('minimal',4,6),('short',7,9)]:
  ov=copy.deepcopy(base)
  ov.update(setMinStep=lo,setStepMax=hi,tpRanges={k:k==name for k in ['micro','minimal','short','normal','long','wide']})
  ov['indicationConfigs']=[{'label':name+' independent lanes','names':['momchg_n5_t1','mpull_n5_t1','wickrev_n5_t1','sweep_n5_t1'],
    'relations':[''],'stepMin':lo,'stepMax':hi,'slRatios':[.25,.5],'types':['base'],
    'symbols':names,'major':name,'minor':'independent','lastNLanes':DEFAULT_LAST_N_LANES}]
  plan.append({'range':name,'overlay':ov})
 protocol={'symbols':names,'prehistory':[start,start+86400],'trade':[hours[0],hours[-1]+3600],
  'fees':'Public quote maker 0.02%, taker 0.05%, fee-relative slippage 10%; funding excluded',
  'selection':'Require PF>1 in BOTH 12-hour halves and >=10 closes per half; no automatic economic promotion',
  'priorDataOverlap':True,'plans':plan,'sourceHash':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
  'execution':'Native lane/entry/Axis rules; one physical position per symbol; independent qualification does not multiply venue slots'}
 (out/'protocol.json').write_text(json.dumps(protocol,indent=2));results=[]
 for g in plan:
  os.environ['STAGE_OVERLAY_PATCH']=json.dumps(g['overlay']);book=sg.make_book();assert not book.config_report['dropped'],book.config_report['dropped']
  print('BUILD',g['range'],len(book.by_idx),flush=True)
  fp=replay_cache.fingerprint(args.market,names,sg.replay_overlay(),sg.PULSE,sg.__file__)
  cp=str(out/(g['range']+'.pkl'))
  shadow=replay_cache.load(cp,fp) if Path(cp).exists() else sg.build(names,bars,times,args.jobs)
  if not Path(cp).exists():replay_cache.save(cp,shadow,fp)
  for role in ['major','minor','independent','parallel']:
   begun=time.perf_counter();b=sg.make_book()
   b.by_idx=[s for s in b.by_idx if (b.pack_ranges.get(s.pack) or {}).get("lanePolicy")]
   if role!='parallel':
    b.by_idx=[s for s in b.by_idx if b.pack_ranges[s.pack]['lanePolicy']['role']==role]
   b.sets={s.id:s for s in b.by_idx};b.packs=sorted({s.pack for s in b.by_idx})
   tapes=sg.Tapes(b,names,times,shadow)
   n,bad=sg.self_check(b,tapes,hours,samples=12);assert bad==0,(g['range'],role,bad)
   co=Coordinator();co.load({},sg.replay_overlay())
   raw=sg.execute(b,names,bars,times,shadow,tapes,hours,refresh_s=0,include_open=True,axis_coord=co)
   settled=sorted([r for r in raw if 'net' in r],key=lambda r:(r['t'],r['symbol']))
   held=[r for r in raw if r.get('open_at_end')]
   trades=[{k:r[k] for k in ['t','t_in','symbol','set','pack','kind','lane','reason','net']}|{'side':r['tr']['dir'],'entry':r['tr']['entry_px'],'exit':r['tr']['exit_px'],
      'slRatio':b.sets[r['set']].sl_ratio,'step':b.sets[r['set']].step,'effectiveSl':b.lane_fracs(b.sets[r['set']])[0],'effectiveTp':b.lane_fracs(b.sets[r['set']])[1]} for r in settled]
   snapshots=[]
   for at in hours:
    gp=gl=0.;passed=active=0
    for st in b.by_idx:
     ok,_,_,_=sg.gate_base(b,tapes,st,at)
     if ok:
      e=sg.base_eval(b,tapes,st,at);ns=[v-e['cf'] for v in e['w']];gp+=sum(max(0,v) for v in ns);gl+=sum(max(0,-v) for v in ns);passed+=1
     active+=bool(sg.gate_metrics(b,tapes,st,at)[0])
    snapshots.append({'t':at,'evaluated':len(b.by_idx),'basePassed':passed,'validatedPercent':100*passed/len(b.by_idx),'postBasePf':gp/gl if gl else None,
      'noLoss':bool(gp and not gl),'executable':active,'settled':metric([r['net'] for r in trades if at<r['t']<=at+3600])})
   halves=[metric([r['net'] for r in trades if (r['t']<=hours[12])==first]) for first in [True,False]]
   result={'id':g['range']+'-'+role,'range':g['range'],'role':role,'sets':len(b.by_idx),'closed':metric([r['net'] for r in trades]),
    'marked':metric([r['net'] for r in trades]+[r['mark_net'] for r in held]),'held':len(held),'halves':halves,'trades':trades,
    'unitDrawdownPercent':drawdown([r['net'] for r in trades]),'stageSnapshots':snapshots,'parity':{'checks':n,'mismatches':bad},'seconds':time.perf_counter()-begun}
   (out/(result['id']+'.json')).write_text(json.dumps(result,indent=2));results.append({k:v for k,v in result.items() if k not in ['trades','stageSnapshots']})
   (out/'summary.json').write_text(json.dumps(results,indent=2));print(json.dumps(results[-1]),flush=True)
if __name__=='__main__':main()
