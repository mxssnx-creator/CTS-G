#!/usr/bin/env python3
"""Research-only symbol/side walk-forward. Cache must be locally generated and trusted."""
import argparse, os, sys, json, pickle, bisect, time
from array import array
from pathlib import Path
ap=argparse.ArgumentParser(description=__doc__)
ap.add_argument('--data', required=True)
ap.add_argument('--cache', required=True, help='trusted local trade_sim cache from this exact grid')
ap.add_argument('--reference', required=True, help='hourly trade_sim JSON for this exact grid')
ap.add_argument('--out', required=True)
ap.add_argument('--min-context', type=int, default=12)
ap.add_argument('--max-modeled-stop-loss-pct', type=float, default=None,
                help='optional maximum initial stop distance plus modeled cost, in percent')
args=ap.parse_args()
if args.min_context < 1:
    ap.error('--min-context must be at least 1')
if args.max_modeled_stop_loss_pct is not None and not 0 < args.max_modeled_stop_loss_pct <= 100:
    ap.error('--max-modeled-stop-loss-pct must be greater than 0 and no more than 100')
reference=json.loads(Path(args.reference).read_text())
os.environ['STAGE_OVERLAY_PATCH']=json.dumps({'histLookbackBars':reference['gate']['lookback_bars'],
    'histLookbackAuto':False,'holdEnabled':False,'positionCostPct':reference['config']['cost_pct'],
    'extraIndicators':reference['config'].get('extra_indicators',[])})
import stage_eval as sg
import set_engine as se
names,bars,times=sg.load_bars(args.data,len(reference['symbols']))
with open(args.cache,'rb') as f: shadow=pickle.load(f)
book=sg.make_book()
if (names != reference['symbols'] or len(book.by_idx) != reference['sets']
        or any(len(shadow[s][3]) != len(book.by_idx) for s in names)):
    raise RuntimeError('reference data or grid differs')
expected={'honor_tp_trail':book.hist_honor_tp,'exit_sets':book.exit_sets,
          'time_bars':book.hist_time_bars,'scratch_bars':book.scratch_bars,
          'scratch_min_pct':book.scratch_min*100,'cost_pct':book.cost_pct}
if any(reference['config'].get(key)!=value for key,value in expected.items()):
    raise RuntimeError('reference exit or cost settings differ')
if (reference['gate']['window']!=book.gate_window or reference['gate']['short']!=book.base_short
        or reference['gate']['main_n']!=book.main_n):
    raise RuntimeError('reference stage settings differ')
book.gate_min=args.min_context
cfg=sg.lane_cfg(book)
cf=book.cost_pct/100.0
acc={s:se.BarIndex(bars[s]) for s in names}
index={s:{t:i for i,t in enumerate(times[s])} for s in names}
hours=[h['t'] for h in reference['hourly']]
if not hours or any(hours[i]-hours[i-1] != 3600 for i in range(1,len(hours))):
    raise ValueError('reference hours must be non-empty and contiguous')
end={s:bisect.bisect_left(times[s],hours[-1]+3600) for s in names}
busy={s:-1 for s in names}
contexts={}

class ContextTape(sg.Tapes):
    def __init__(self,s,side):
        self.cf=cf
        self.by_set={}
        ts=times[s]
        dirs=shadow[s][1]
        for st in book.by_idx:
            en,ex,pp=shadow[s][3][st.idx]
            rows=sorted((ts[j]+60,ts[a]+60,p) for a,j,p in zip(en,ex,pp) if dirs[st.pack][a]==side)
            self.by_set[st.idx]=(array('d',(r[0] for r in rows)),array('d',(r[1] for r in rows)),array('d',(r[2] for r in rows)))

def ranked(s,side,pack,now):
    key=(s,side)
    if key not in contexts: contexts[key]=ContextTape(*key)
    tape=contexts[key]
    out={('pos','fixed'):[],('pos','trail'):[],('neg','fixed'):[],('neg','trail'):[]}
    for st in book.by_idx:
        if st.pack != pack: continue
        if (args.max_modeled_stop_loss_pct is not None
                and book.lane_fracs(st)[0] * 100 + book.cost_pct > args.max_modeled_stop_loss_pct + 1e-9):
            continue
        ok,rank,n,pf=sg.gate_metrics(book,tape,st,now)
        if ok:
            lane=ok if isinstance(ok,str) else 'pos'
            out[(lane,'trail' if st.kind=='trail' else 'fixed')].append((rank,st))
    for rows in out.values(): rows.sort(key=lambda x:x[0],reverse=True)
    return out

rows=[]; stats={'rankings':0,'rankings_with_eligible':0}
for h in hours:
    cache={}
    for minute in range(60):
        t=int(h)+minute*60
        # A signal closing exactly at the report boundary has no forward
        # observation in this window; it is not an in-window entry.
        if t+60 >= hours[-1]+3600:
            continue
        for s in names:
            i=index[s].get(t)
            if i is None or i<=busy[s]: continue
            warm,dirs,confs,_=shadow[s]
            if i<warm: continue
            best=None
            for p in book.packs:
                d,c=dirs[p][i],confs[p][i]
                if d and c>=cfg['entry_conf'] and (best is None or c>best[1]): best=(p,c,d)
            if best is None: continue
            pack,conf,side=best
            key=(s,side,pack)
            if key not in cache:
                cache[key]=ranked(s,side,pack,h+60)
                stats['rankings']+=1
                stats['rankings_with_eligible']+=int(any(cache[key].values()))
            choice=None; lane=''
            for ln in ('pos','neg'):
                for kind in ('fixed','trail'):
                    choices=cache[key][(ln,kind)]
                    if choices: choice=choices[0][1];lane=ln;break
                if choice: break
            if choice is None: continue
            machine=se.SetLane(choice,s)
            dd=[0]*len(bars[s]);cc=[0.0]*len(bars[s]);dd[i]=side;cc[i]=conf
            trades=machine.scan(i,end[s],bars[s],dd,cc,True,cfg,[i],acc[s])
            if not trades:
                if machine.open is not None:
                    busy[s]=end[s]-1
                    mark=float(bars[s][end[s]-1][3]); ent=machine.open['entry']
                    rows.append({'open':True,'symbol':s,'side':side,'set':choice.id,'lane':lane,'t_in':t+60,'net':(mark/ent-1)*side-cf})
                continue
            tr=trades[0];busy[s]=tr['exit_i']
            rows.append({'open':False,'symbol':s,'side':side,'set':choice.id,'lane':lane,'t_in':t+60,'t_out':times[s][tr['exit_i']]+60,'reason':tr['reason'],'net':tr['pnl_pct']-cf})
    print('hour',time.strftime('%Y-%m-%d %H:%M',time.gmtime(h)),'rankings',stats['rankings'],'eligible',stats['rankings_with_eligible'],'positions',len(rows),flush=True)
closes=[r for r in rows if not r['open']];opens=[r for r in rows if r['open']]
def pf(seq):
    g=sum(r['net'] for r in seq if r['net']>0); l=-sum(r['net'] for r in seq if r['net']<0)
    return g/l if l else None
out={'symbols':names,'sets':len(book.by_idx),'hours':hours,'config':{'cost_pct':book.cost_pct,'history_bars':book.lookback,'min_same_context_closes':book.gate_min,'gate_refresh_seconds':3600,'max_modeled_stop_loss_pct':args.max_modeled_stop_loss_pct,'extra_indicators':reference['config'].get('extra_indicators',[])},'stats':stats,'closed_count':len(closes),'closed_pf':pf(closes),'open_count':len(opens),'closed_plus_marked_pf':pf(rows),'rows':rows,'note':'Research model only: hourly gate per Set, symbol and side with a minimum number of same-context paper closes. The optional stop filter is a model constraint, not a guarantee against gaps, venue behavior, leverage, added positions or account drawdown. Modeled fills and one historical window do not establish profitability.'}
Path(args.out).write_text(json.dumps(out,indent=2)+'\n')
print(json.dumps({k:v for k,v in out.items() if k!='rows'},indent=2),flush=True)
