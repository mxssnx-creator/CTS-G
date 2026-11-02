#!/usr/bin/env python3
"""Check research scoped entries, ranking and bar-modeled fills from a trusted local cache."""
import argparse,os,sys,json,pickle,bisect
from array import array
from pathlib import Path
ap=argparse.ArgumentParser(description=__doc__)
ap.add_argument('--data', required=True)
ap.add_argument('--cache', required=True, help='trusted cache from the exact grid')
ap.add_argument('--result', required=True)
args=ap.parse_args()
x=json.loads(Path(args.result).read_text())
os.environ['STAGE_OVERLAY_PATCH']=json.dumps({'histLookbackBars':x['config']['history_bars'],
    'histLookbackAuto':False,'holdEnabled':False,'positionCostPct':x['config']['cost_pct'],
    'extraIndicators':x['config'].get('extra_indicators',[])})
import stage_eval as sg
names,bars,times=sg.load_bars(args.data,len(x['symbols']))
with open(args.cache,'rb') as f: shadow=pickle.load(f)
b=sg.make_book();b.gate_min=x['config']['min_same_context_closes']
if (names != x['symbols'] or len(b.by_idx) != x['sets']
        or any(len(shadow[s][3]) != len(b.by_idx) for s in names)):
    raise ValueError('data, cache or Set grid differs')
by_id={st.id:st for st in b.by_idx};cf=b.cost_pct/100; cache={}; ranks={}; bad=[];unchecked=[];min_n=None
class Scope(sg.Tapes):
    def __init__(self,s,side):
        self.cf=cf;self.by_set={};ts=times[s];dirs=shadow[s][1]
        for st in b.by_idx:
            en,ex,pp=shadow[s][3][st.idx]
            rows=sorted((ts[j]+60,ts[a]+60,p) for a,j,p in zip(en,ex,pp) if dirs[st.pack][a]==side)
            self.by_set[st.idx]=(array('d',(z[0] for z in rows)),array('d',(z[1] for z in rows)),array('d',(z[2] for z in rows)))
for r in x['rows']:
    s=r['symbol'];side=r['side'];st=by_id[r['set']];ts=times[s];series=bars[s]
    if not x['hours'][0] <= r['t_in'] < x['hours'][-1]+3600:
        bad.append({'row':r,'error':'entry outside report window'})
    i=bisect.bisect_left(ts,r['t_in']-60)
    if i>=len(ts) or ts[i]+60 != r['t_in'] or shadow[s][1][st.pack][i]!=side: bad.append({'row':r,'error':'entry signal mismatch'});continue
    eligible=[(shadow[s][2][p][i],p) for p in b.packs if shadow[s][1][p][i] and shadow[s][2][p][i]>=b.entry_conf]
    if not eligible or st.pack != max(eligible,key=lambda z:z[0])[1]: bad.append({'row':r,'error':'wrong signal pack'})
    if (s,side) not in cache:cache[(s,side)]=Scope(s,side)
    anchor=((int(r['t_in'])-60)//3600)*3600+60
    tape=cache[(s,side)]
    ok,rank,n,pf=sg.gate_metrics(b,tape,st,anchor);min_n=n if min_n is None else min(min_n,n)
    if not ok or n<b.gate_min or (ok if isinstance(ok,str) else 'pos')!=r['lane']: bad.append({'row':r,'error':'scope gate or lane mismatch','n':n})
    limit=x['config'].get('max_modeled_stop_loss_pct')
    if limit is not None and b.lane_fracs(st)[0]*100+b.cost_pct>limit+1e-9:
        bad.append({'row':r,'error':'modeled stop plus cost exceeds filter'})
    rk=(s,side,st.pack,anchor)
    if rk not in ranks:
        choices={('pos','fixed'):[],('pos','trail'):[],('neg','fixed'):[],('neg','trail'):[]}
        for candidate in b.by_idx:
            if candidate.pack != st.pack:continue
            if limit is not None and b.lane_fracs(candidate)[0]*100+b.cost_pct>limit+1e-9:continue
            valid,key,_,_=sg.gate_metrics(b,tape,candidate,anchor)
            if valid:
                lane=valid if isinstance(valid,str) else 'pos'
                choices[(lane,'trail' if candidate.kind=='trail' else 'fixed')].append((key,candidate.id))
        picked=None
        for lane in ('pos','neg'):
            for kind in ('fixed','trail'):
                group=choices[(lane,kind)]
                if group:
                    # Python's stable sort retains grid order when rank keys tie.
                    picked=sorted(group,key=lambda item:item[0],reverse=True)[0][1]
                    break
            if picked:break
        ranks[rk]=picked
    if ranks[rk] != st.id:bad.append({'row':r,'error':'scoped ranking mismatch','expected':ranks[rk]})
    if r['open']:continue
    if st.kind not in ('base','exit','trail'):unchecked.append({'symbol':s,'set':st.id});continue
    e=series[i][3];slf=max((1+b.sl_min_steps)*cf,st.tp_pct*st.sl_ratio);tpf=max(2*cf,st.tp_pct)
    sl=e*(1-side*slf);tp=e*(1+side*tpf);res=None;peak=e;trail=None
    end=bisect.bisect_left(ts,x['hours'][-1]+3600)
    for j in range(i+1,end):
        high,low,close=series[j][1:4]
        stop=(max(sl,trail) if side>0 else min(sl,trail)) if trail is not None else sl
        if (low<=stop if side>0 else high>=stop):res=(j,'sl',stop)
        elif (st.kind=='base' or (st.kind=='trail' and b.hist_honor_tp)) and (high>=tp if side>0 else low<=tp):res=(j,'tp',tp)
        if res is None and st.kind=='trail':
            peak=max(peak,high) if side>0 else min(peak,low)
            if (peak-e)/e*side>=st.trail_arm/100:
                level=peak*(1-side*st.trail_give/100)
                trail=level if trail is None else (max(trail,level) if side>0 else min(trail,level))
            if trail is not None and (close<=trail if side>0 else close>=trail):res=(j,'sl',trail)
        if res is None and j-i>=b.hist_time_bars:res=(j,'time',close)
        if res is None and j-i>=b.scratch_bars and (close-e)/e*side>=b.scratch_min:res=(j,'scratch+',close)
        if res:break
    if not res or ts[res[0]]+60!=r['t_out'] or res[1]!=r['reason'] or abs((res[2]-e)/e*side-cf-r['net'])>1e-10:
        bad.append({'row':r,'error':'fixed-exit fill mismatch','result':res})
print(json.dumps({'records':len(x['rows']),'closed':x['closed_count'],'min_scoped_gate_n':min_n,'mismatches':bad,'unaudited':unchecked,'note':'Checks signals, hourly context gate and ranking, and independent bar-by-bar modeled exits. It does not validate live fills or intrabar ordering.'},indent=2))
sys.exit(bool(bad or unchecked))
