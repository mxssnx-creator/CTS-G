import sys,json,glob,os,math,argparse
from concurrent.futures import ProcessPoolExecutor
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'server', 'pulse'))
import indication_extra as ix
from set_engine import hit_exit
NAMES=[f'ao_{fam}_n{n}_t1' for fam in ('laguerre','ultimate','cfltrend','cflbrk','cmpx','regime','axisrev') for n in (5,8)]+['lr_fade','lr_break','lr_trendbreak','lr_nr7','lr_inside']
COST=.12

def worker(path):
    raw=sorted(json.load(open(path)),key=lambda r:r[0]); bars=[[float(x) for x in r[1:6]] for r in raw]; n=len(bars); split=n-720
    if n != 1440 or any(b[0]-a[0] != 60000 for a,b in zip(raw,raw[1:])): raise ValueError(f'{path}: need 1440 contiguous one-minute bars')
    out={name:{'pre':[],'forward':[],'cross':0,'open':0} for name in NAMES}
    for name in NAMES:
        fn=ix.resolve(name); busy=-1
        for i in range(100,n-1,3):
            if i<=busy: continue
            signal=fn(bars[max(0,i+1-ix.window_of(name)):i+1])
            if not signal or signal[1]<.58: continue
            side=signal[0]; entry=bars[i][3]; sl=entry*(1-side*.0024); tp=entry*(1+side*.0036)
            end=min(n-1,i+60); exit_i=None; px=None
            for j in range(i+1,end+1):
                why,price=hit_exit(side,entry,sl,tp,None,bars[j])
                if why: exit_i=j;px=price;break
            if exit_i is None and end==i+60: exit_i=end;px=bars[end][3]
            if exit_i is None: out[name]['open']+=1;break
            busy=exit_i; net=side*(px-entry)/entry*100-COST
            if i<split<=exit_i: out[name]['cross']+=1
            else: out[name]['pre' if exit_i<split else 'forward'].append(net)
    return os.path.basename(path),out,raw[0][0],raw[-1][0]

def m(v):
    g=sum(x for x in v if x>0); l=-sum(x for x in v if x<0)
    return {'n':len(v),'pf':round(g/l,4) if l else (None if not g else 99),'net_pct':round(sum(v),4)}

if __name__=='__main__':
    ap=argparse.ArgumentParser(description='Offline independent signal screen; no exchange calls')
    ap.add_argument('--data',required=True); ap.add_argument('--out',required=True); ap.add_argument('--jobs',type=int,default=4)
    args=ap.parse_args()
    paths=sorted(glob.glob(os.path.join(args.data,'*-USDT.json')))
    if not paths: raise ValueError('no symbol data')
    with ProcessPoolExecutor(max_workers=max(1,args.jobs)) as p: rows=list(p.map(worker,paths))
    results={}
    for name in NAMES:
        a=[v for _,d,_,_ in rows for v in d[name]['pre']]; b=[v for _,d,_,_ in rows for v in d[name]['forward']]
        results[name]={'pre':m(a),'forward':m(b),'cross_boundary':sum(d[name]['cross'] for _,d,_,_ in rows),'still_open':sum(d[name]['open'] for _,d,_,_ in rows)}
    selected={n:r for n,r in results.items() if r['pre']['n']>=10 and r['pre']['pf'] is not None and r['pre']['pf']>=1.2}
    result={'method':'independent raw signal, 3 minute sampling, 60 minute time cap, 0.36% target, 0.24% stop, 0.12% round-trip; pooled across symbols; no Base/Main/Real gates, no allocation, no intrabar path, exclude boundary crossings','interval_ms':[min(r[2] for r in rows),max(r[3] for r in rows)],'symbols':[r[0] for r in rows],'selection':'pre n>=10 and pf>=1.2','selected':selected,'all':results}
    with open(args.out,'w') as f: json.dump(result,f,indent=2)
    print(json.dumps({'symbols':len(rows),'screened':len(NAMES),'selected':selected},indent=2))
