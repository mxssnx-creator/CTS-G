#!/usr/bin/env python3
"""Native Axis admission and independent Block/DCA counterfactuals on 50 symbols.

Each overlay is compared against its own identical parents at equal original
base notional. No strategy's parent is counted twice in a combined total.
All declared settings are diagnostics; none is promoted from this one window.
"""
import argparse
import bisect
import collections
import json
import os
from pathlib import Path
import pickle
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path[:0]=[str(HERE),str(HERE.parent/'server/pulse')]


def main():
    ap=argparse.ArgumentParser()
    for name in ['data','cache','parents','patch-file','out']:ap.add_argument('--'+name,required=True)
    ap.add_argument('--compare-axis',action='store_true')
    args=ap.parse_args()
    os.environ['STAGE_OVERLAY_PATCH']=Path(args.patch_file).read_text()
    import stage_eval as sg
    import replay_cache
    import set_engine as se
    import block_rounds as br
    from block_coordination_study import simulate as block_sim
    from coord_engine import Coordinator
    from stack_eval import summarize,dca_rungs,admit_dca_candidates,add_notional_weight
    t0=time.monotonic();names,bars,times=sg.load_bars(args.data,50);book=sg.make_book();overlay=sg.replay_overlay()
    manifest=replay_cache.fingerprint(args.data,names,overlay,sg.PULSE,sg.__file__)
    saved=pickle.load(open(args.parents,'rb'))
    if saved['fingerprint']!=manifest or saved['axis']!='overlay':raise ValueError('parents differ from current verified replay')
    hours=saved['hours'];parents=[r for r in saved['rows']if 'net'in r]
    shadow=replay_cache.load(args.cache,manifest);tapes=sg.Tapes(book,names,times,shadow)
    by_id={st.id:st for st in book.by_idx};cf=book.cost_pct/100
    def metric(values,adds=0):
        events=[(r['t'],v,1.0)for r,v in zip(parents,values)]
        return summarize(events,len(parents),adds,hours)
    units=[r['net']for r in parents]
    out={'scope':'Equal original base notional; overlays on identical native admitted parents; OHLC approximation, no venue rounding',
         'baseline':metric(units),'axis':{'current':metric(units)},'block':{},'dca':{},'rounds':{},'per_indication':{}}
    def record(label,values,adds_by_parent=None):
        groups={}
        for i,(r,value)in enumerate(zip(parents,values)):
            groups.setdefault((r['pack'],r['kind']),[]).append((r['t'],value,1.0))
        for pack in book.packs:
            for kind in ['base','exit','trail']:
                events=groups.get((pack,kind),[])
                adds=sum((adds_by_parent or {}).get(i,0)for i,r in enumerate(parents)if r['pack']==pack and r['kind']==kind)
                stats=summarize(events,len(events),adds,hours);stats.pop('hourly',None)
                out['per_indication'].setdefault(pack,[]).append({'processing':label,'kind':kind,**stats})
    record('Axis current · parents',units)
    coord=Coordinator();coord.load({},overlay)
    if args.compare_axis:
        cache=Path(args.parents+'.axis-off.pkl')
        if cache.exists():
            rows=pickle.loads(cache.read_bytes())
            if rows['fingerprint']!=manifest:raise ValueError('Axis control cache differs')
            rows=rows['rows']
        else:
            rows=sg.execute(book,names,bars,times,shadow,tapes,hours,refresh_s=0,include_open=True)
            cache.write_bytes(pickle.dumps(dict(fingerprint=manifest,rows=rows)))
        settled=[r for r in rows if 'net'in r]
        out['baseline']=summarize([(r['t'],r['net'],1)for r in settled],len(settled),0,hours)
        print('Axis control',out['baseline']['pf'],len(settled),flush=True)
    auto=sg.AutoFloors(book,tapes);window_parents=[];allowed_cache={}
    def eligible(st,lane,now):
        from stack_eval import qualified_parent_lanes
        key=(st.idx,lane,now)
        if key not in allowed_cache:
            auto.apply(book,now)
            allowed_cache[key]=lane in qualified_parent_lanes(book,tapes,st,now)and sg.axis_judge(book,tapes,coord,st,now,lane)[0]
        return allowed_cache[key]
    for r in parents:
        st=by_id[r['set']];tr=r['tr'];symbol=r['symbol'];side=int(tr['dir']);entry=tr['entry_px'];extremes=[];best=0
        for i in range(tr['entry_i']+1,tr['exit_i']):
            px=bars[symbol][i][1 if side>0 else 2];move=(px/entry-1)*side/cf
            if move>best+1e-12:
                best=move;at=float(times[symbol][i]);wn=book.win(st)
                extremes.append((at,move,eligible(st,r['lane'],at),book.floor_for(wn.real_neg_n if r['lane']=='neg'else wn.real_n,st.pack,st)))
        honor=se.lane_honors_tp(st.kind)
        window_parents.append(dict(t_in=r['t_in'],t=r['t'],net=r['net'],symbol=symbol,set=st.id,pack=st.pack,
            kind=st.kind,lane=r['lane'],side=side,step=st.step,range='trail'if st.kind=='trail'else se.tp_range_of(st.step),
            entry=entry,exit=tr['exit_px'],reason=r['reason'],honor_tp=book.hist_honor_tp if honor is None else honor,
            tp_costs=r['fracs'][1]/cf,sl_costs=r['fracs'][0]/cf,extremes=extremes))
    window=dict(meta=dict(cost_frac=cf),parents=window_parents)
    block_cfg={k:v for k,v in overlay.items()if k.startswith('block')}
    arms=[('shipped',block_cfg)]+[(f'{rel} ratio {ratio}',{**block_cfg,'blockRelations':[rel],'blockVolumeRatio':ratio,'blockCounts':[]})
        for rel in ['set','symbol','step']for ratio in [.1,.2,.5]]
    for label,cfg in arms:
        captured=[];result=block_sim(window,cfg,capture=captured)
        values=units.copy();counts={}
        for r in captured:values[r['parent']]=r['with'];counts[r['parent']]=r['adds']
        out['block'][label]={**metric(values,sum(counts.values())),'adds_pf':result['addsPf'],'adds_net_pct':result['addsNet']}
        record('Block · '+label,values,counts)
    print('Block comparisons',len(arms),flush=True)
    round_arms=[('shipped',br.config_of(overlay.get('blockRounds')))]
    for n in range(1,7):
        round_arms.append((f'indication count {n} ratio 0.2',br.config_of(dict(enabled=True,relations=['indication'],maxMult=3,
            counts={str(n):dict(on=True,ratio=.2,step=True,maxLevel=2,pause=1)}))))
    for label,cfg in round_arms:
        rb=br.RoundsBook(cfg=cfg);mult={};events=[]
        for i,r in enumerate(window_parents):events.extend([(r['t_in'],0,i),(r['t'],1,i)])
        for at,kind,i in sorted(events):
            r=window_parents[i]
            if kind==0:
                keys=br.keys_of(r['symbol'],r['side'],r['pack'],r['kind'],r['step'],r['lane'],r['set'])
                mult[i]=rb.ask(keys)[0];rb.opened(str(i),keys,mult[i])
            else:rb.closed(str(i),r['net'])
        values=[r['net']*mult[i]for i,r in enumerate(parents)]
        out['rounds'][label]={**metric(values),'raised':sum(v > 1 for v in mult.values()),'maxMult':max(mult.values(),default=1)}
        record('Block rounds · '+label,values)
    for label,cfg in [('auto 1 rung',dict(n=1,f0=.4,fs=.05,m0=.5,g=1,max_expo=2)),
                     ('2 rungs',dict(n=2,f0=.25,fs=.25,m0=.25,g=1,max_expo=2)),
                     ('6 rungs',dict(n=6,f0=.1,fs=.1,m0=.15,g=1,max_expo=2))]:
        candidates=[]
        for i,r in enumerate(parents):
            tr=r['tr'];symbol=r['symbol'];side=int(tr['dir']);entry=float(tr['entry_px']);sl=r['fracs'][0]
            def hit(lo,hi,level,direction):
                return next((j for j in range(lo,hi)if (bars[symbol][j][1]>=level if direction>0 else bars[symbol][j][2]<=level)),-1)
            fills=dca_rungs(side,entry,tr['exit_px'],sl,tr['entry_i'],tr['exit_i']+1 if r['net'] < 0 else tr['exit_i'],cfg,cf,hit)
            for j,k,m,net in fills:
                distance=min(max((cfg['f0']+k*cfg['fs'])*sl,cf),.9*sl);level=entry*(1-side*distance)
                candidates.append(dict(now=float(times[symbol][j]),close=r['t'],net=net,parent_key=i,symbol=symbol,
                    rung=k,parent=r,weight=add_notional_weight(entry,level,m)))
        admitted=admit_dca_candidates(candidates,book.min_pf,stop_parent=True)
        values=units.copy();counts=collections.Counter()
        for c in admitted:values[c['parent_key']]+=c['net']*c['weight'];counts[c['parent_key']]+=1
        out['dca'][label]=metric(values,len(admitted));record('DCA · '+label,values,counts)
    out['seconds']=time.monotonic()-t0
    Path(args.out).write_text(json.dumps(out,indent=1,allow_nan=False))
    print(json.dumps({key:{k:v['pf']for k,v in out[key].items()}for key in ['axis','block','rounds','dca']}),flush=True)


if __name__=='__main__':main()
