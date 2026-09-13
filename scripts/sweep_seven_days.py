"""Restartable seven-day, all-policy matrix. Price paths are computed once.

The independent shadow Set continues collecting closes even while admission
is false. Live-admission policies use only closes before an entry. Results are
research lanes, not an additive portfolio or an exchange execution claim.
"""
import argparse
import ctypes
import gzip
import hashlib
import html
import itertools
import json
import pathlib
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
from replay_five_days import replay
from fetch_historic_window import validate
from set_engine import IND_KINDS, IND_TAG_KIND, indication_kind_votes
from position_cost import POSITIVE_PF
from control_policy import ControlRule, METRICS, training_winner

ROOT=pathlib.Path(__file__).resolve().parents[1]
POLICIES=np.array(list(itertools.product(range(5,76,5),range(5,26,5),(1,3,5,7,9),(1,5,10),(0,1,2))),dtype=np.int32)
STAGE_WINDOWS=((5,3),(10,5),(15,10))
POLICIES=np.array([(*p[:4],*STAGE_WINDOWS[p[4]]) for p in POLICIES],dtype=np.int32)
POLICY_KEYS=('lastN','deactivationN','maxDdtHours','recalcAfterCloses','mainN','realN')


def configs():
    out=[]
    for step,sl in itertools.product(range(1,31),(.2,.4,.6,.8)):
        base=dict(strategy='base',step=step,tpPct=max(.3,step*.1),slPct=sl,minSlPct=sl,
                  levels=0,incrementPct=0.,volumeRatio=0.,honorTp=True)
        out.append(base)
        for arm,give,honor in itertools.product((.3,.6,.9,1.2,1.5),(.1,.2,.3,.4,.5),(True,False)):
            out.append(dict(base,strategy='trail',trailArmPct=arm,trailGivePct=give,honorTp=honor))
        for level in range(1,7):
            out.append(dict(base,strategy='block',levels=level,incrementPct=.2,volumeRatio=.25))
        for level,inc in itertools.product((1,2,3),(.05,.1,.2)):
            out.append(dict(base,strategy='dca',levels=level,incrementPct=inc,volumeRatio=.25))
    return out


def kernel(path, min_pf=POSITIVE_PF):
    lib=ctypes.CDLL(str(path))
    arr=np.ctypeslib.ndpointer(flags='C_CONTIGUOUS')
    lib.sweep.argtypes=[ctypes.c_int,arr,arr,arr,arr,ctypes.c_int,arr,ctypes.c_int,ctypes.c_int,ctypes.c_double,arr]
    lib.sweep.restype=None
    return lambda *args: lib.sweep(*args[:-1],float(min_pf),args[-1])


def prepare(path,out):
    raw=path.read_bytes();blob=json.loads(raw)
    validate(blob['rows'],blob['start']-3600000,blob['end'])
    if blob['end']-blob['start']!=7*86400000:raise ValueError('Exact seven-day window required')
    bars=[b for _,b in blob['rows']];signals={k:np.zeros((len(bars),2)) for k in IND_KINDS}
    settings={f'type{k.title()}':True for k in IND_KINDS}
    for i in range(60,len(bars)):
        for d,c,tag in indication_kind_votes(bars[i-59:i+1],settings,blob['rows'][i][0]/1000):
            kind=IND_TAG_KIND.get(tag)
            if kind:signals[kind][i]=(d,c)
    dest=out/(blob['symbol']+'.npz')
    np.savez_compressed(dest,bars=np.array(bars),**signals)
    return dict(path=str(dest),symbol=blob['symbol'],start=blob['start'],end=blob['end'],
                bars=len(bars),sha256=hashlib.sha256(raw).hexdigest(),source=blob['source'])


def execute(task):
    source,kind,side,out,lib,signature,rule=task;out=pathlib.Path(out)
    key=f"{source['symbol']}_{kind}_{'LONG' if side==1 else 'SHORT'}"
    dest=out/(key+'.json.gz')
    if dest.exists():
        old=json.loads(gzip.decompress(dest.read_bytes()))
        if reusable_group(old,source,signature,rule):return old
    start=time.monotonic();blob=np.load(source['path']);bars=blob['bars'];signals=blob[kind]
    all_cfg=configs();unique=[];aliases=[];seen={}
    for i,c in enumerate(all_cfg):
        ck=json.dumps({k:v for k,v in c.items() if k not in ('step','minSlPct')},sort_keys=True)
        if ck not in seen:seen[ck]=len(unique);unique.append(c);aliases.append([])
        aliases[seen[ck]].append(i)
    n=len(unique);event_chunks=[]
    def closes(ids,entries,bar,net,cost):
        event_chunks.append(np.column_stack((ids,entries,np.full(len(ids),bar),net,cost)))
    replay(bars,signals,side,unique,on_closes=closes,return_rows=False,cost_pct=.1)
    events=np.concatenate(event_chunks) if event_chunks else np.zeros((0,5));del event_chunks
    if len(events):
        events=events[np.lexsort((events[:,1],events[:,0]))]
    offsets=np.searchsorted(events[:,0],np.arange(n+1)) if len(events) else np.zeros(n+1,dtype=int)
    fn=kernel(lib);metrics=np.zeros((len(POLICIES),len(METRICS)));summary=np.zeros((len(POLICIES),13))
    top=[];best_train=None;positive=0;traded=0;qualified=0;ever_positive=0;unique_results=hashlib.sha256()
    sensitivity={(n,pf):0 for n,pf in itertools.product((3,5,8),(1.,1.01,1.02))}
    states={};strict_qualified=0;selected=[];training_qualified=0
    # Columns: train-qualified independent price paths, train N/net/cost-R,
    # control N/net/cost-R/gain/loss and qualified path count. Alias IDs never
    # overweight this cohort comparison.
    cohort=np.zeros((len(POLICIES),10))
    by_strategy={};split=60+int((len(bars)-60)*.7)
    for i,cfg in enumerate(unique):
        rows=events[offsets[i]:offsets[i+1]]
        ids=aliases[i];multiplicity=len(ids)
        metrics.fill(0)
        if len(rows):
            fn(len(rows),np.ascontiguousarray(rows[:,1],dtype=np.int32),np.ascontiguousarray(rows[:,2],dtype=np.int32),
               np.ascontiguousarray(rows[:,3]),np.ascontiguousarray(rows[:,4]),len(POLICIES),POLICIES,len(bars)-1,split,metrics)
        unique_results.update(metrics.tobytes())
        pos=metrics[:,2]>1e-12;has=metrics[:,0]>0
        costpf=np.divide(metrics[:,13],metrics[:,0],out=np.zeros(len(POLICIES)),where=has)*.1+1
        trainpf=1+.1*np.divide(metrics[:,16],metrics[:,8],out=np.zeros(len(POLICIES)),where=metrics[:,8]>0)
        testpf=1+.1*np.divide(metrics[:,19],metrics[:,10],out=np.zeros(len(POLICIES)),where=metrics[:,10]>0)
        assessed=rule.masks(metrics);q=assessed['qualified'];training=assessed['training']
        strict_qualified+=int((training & (metrics[:,10]>=8) & (metrics[:,11]>1e-12) & (testpf>POSITIVE_PF+1e-9)).sum())*multiplicity
        training_qualified+=int(training.sum())*multiplicity
        for name,mask in assessed['states'].items():states[name]=states.get(name,0)+int(mask.sum())*multiplicity
        for (control_n,control_pf) in sensitivity:
            passed=training & (metrics[:,10]>=control_n) & (metrics[:,11]>1e-12) & (testpf>control_pf+1e-9)
            sensitivity[(control_n,control_pf)]+=int(passed.sum())*multiplicity
        cohort[:,0]+=training
        for col,source_col in enumerate((8,9,16,10,11,19,17,18),1):cohort[:,col]+=metrics[:,source_col]*training
        cohort[:,9]+=q
        winner=training_winner(metrics,rule,eligible=training)
        if winner is not None:
            m=metrics[winner]
            item=dict(config=ids[0],aliases=ids,policy=winner,metrics=dict(zip(METRICS,m.tolist())),
                costPf=float(costpf[winner]),classicPf=float(m[4]/m[5]) if m[5]>1e-12 else None,
                qualified=bool(q[winner]))
            item['status']=rule.classify(item['metrics'])
            selected.append(item)
        positive+=int(pos.sum())*multiplicity;traded+=int(has.sum())*multiplicity;qualified+=int(q.sum())*multiplicity
        ever_positive+=int(pos.any())*multiplicity
        st=by_strategy.setdefault(cfg['strategy'],dict(tested=0,positive=0,qualified=0))
        st['tested']+=len(POLICIES)*multiplicity;st['positive']+=int(pos.sum())*multiplicity;st['qualified']+=int(q.sum())*multiplicity
        summary[:,0]+=pos*multiplicity;summary[:,1]+=has*multiplicity;summary[:,2]+=q*multiplicity
        summary[:,3]+=metrics[:,9]*multiplicity;summary[:,4]+=metrics[:,11]*multiplicity
        summary[:,5]+=metrics[:,8]*multiplicity;summary[:,6]+=metrics[:,10]*multiplicity
        summary[:,7:13]+=metrics[:,14:20]*multiplicity
        # Keep a bounded, deterministic review table; every result still
        # contributes to exact counters and the full-matrix digest above.
        candidate_ids=np.flatnonzero(has)
        if candidate_ids.size:
            ranking=sorted(candidate_ids,key=lambda j:(bool(q[j]),metrics[j,2],-metrics[j,6]),reverse=True)[:3]
            for j in ranking:
                m=metrics[j]
                item=dict(config=ids[0],aliases=ids,policy=int(j),metrics=dict(zip(METRICS,m.tolist())),
                          costPf=float(costpf[j]),classicPf=float(m[4]/m[5]) if m[5]>1e-12 else None,qualified=bool(q[j]))
                top.append(item)
            train_ids=np.flatnonzero(metrics[:,8]>=8)
            if len(train_ids):
                j=max(train_ids,key=lambda j:(metrics[j,9],-int(j)))
                # Selection score uses training net only; holdout is disclosed
                # after selection and never used to change this choice.
                if best_train is None or metrics[j,9]>best_train['metrics']['trainNet']:
                    m=metrics[j];best_train=dict(config=ids[0],policy=int(j),metrics=dict(zip(METRICS,m.tolist())),costPf=float(costpf[j]))
        if len(top)>300:
            top=sorted(top,key=lambda r:(r['qualified'],r['metrics']['net'],-r['metrics']['maxDd']),reverse=True)[:100]
    result=dict(key=key,signature=signature,symbol=source['symbol'],kind=kind,direction='LONG' if side==1 else 'SHORT',
                configs=len(all_cfg),uniquePricePaths=n,policies=len(POLICIES),tested=len(all_cfg)*len(POLICIES),positive=positive,
                traded=traded,qualified=qualified,positiveConfigs=ever_positive,byStrategy=by_strategy,
                summary=summary.tolist(),top=sorted(top,key=lambda r:(r['qualified'],r['metrics']['net'],-r['metrics']['maxDd']),reverse=True)[:100],
                trainingChoice=best_train,trainingSelections=selected,trainingQualified=training_qualified,strictQualified=strict_qualified,
                outcomes=states,sensitivity=[dict(controlN=k[0],controlPf=k[1],qualified=v) for k,v in sensitivity.items()],
                policyCohort=cohort.tolist(),controlRule=rule.settings(),elapsedS=time.monotonic()-start,resultSha256=unique_results.hexdigest(),source=source)
    dest.write_bytes(gzip.compress(json.dumps(result,separators=(',',':'),allow_nan=False).encode(),mtime=0))
    print(json.dumps({k:result[k] for k in ('key','tested','positive','qualified','elapsedS')}),flush=True)
    return result


def reusable_group(result, source, signature, rule):
    previous=result.get('source') or {}
    return (result.get('signature')==signature and result.get('controlRule')==rule.settings()
            and all(previous.get(k)==source.get(k) for k in ('sha256','start','end','symbol','source')))


def calculation_signature():
    # Only computation code and dependencies participate. Report copy changes
    # do not invalidate 1.28B numerical result vectors.
    import ast
    tree=ast.parse(pathlib.Path(__file__).read_text())
    tree.body=[n for n in tree.body if not (isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in ('main','calculation_signature')) and not isinstance(n,ast.If)]
    files=('scripts/policy_sweep.cpp','scripts/control_policy.py','scripts/replay_five_days.py',
           'server/pulse/indication_engine.py','server/pulse/set_engine.py',
           'server/pulse/position_cost.py','server/pulse/block_engine.py','server/pulse/validation_policy.py')
    raw=ast.dump(tree,include_attributes=False).encode()+b''.join((ROOT/f).read_bytes() for f in files)+np.__version__.encode()
    return hashlib.sha256(raw).hexdigest(),{f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in files}


def validate_result_sources(results, data):
    sources={symbol:hashlib.sha256((data/(symbol+'.json')).read_bytes()).hexdigest() for symbol in ('BCH-USDT','SOL-USDT','XRP-USDT')}
    return all(r['source']['sha256']==sources.get(r['symbol']) for r in results)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--data',required=True);p.add_argument('--output',required=True)
    p.add_argument('--workers',type=int,default=2);p.add_argument('--report-only',action='store_true')
    p.add_argument('--control-min-trades',type=int,default=0)
    p.add_argument('--control-min-pf',type=float,default=1.0)
    a=p.parse_args();rule=ControlRule(control_n=a.control_min_trades,control_pf=a.control_min_pf)
    out=pathlib.Path(a.output).resolve();out.mkdir(parents=True,exist_ok=True)
    signature,hashes=calculation_signature();manifest=out/'run-provenance.json'
    if a.report_only:
        provenance=json.loads(manifest.read_text())
        if provenance['signature']!=signature or provenance['controlRule']!=rule.settings():
            raise ValueError('Calculation sources/control rule changed; rerun the sweep')
        results=[json.loads(gzip.decompress(x.read_bytes())) for x in out.glob('*.json.gz')]
    else:
        manifest.write_text(json.dumps(dict(signature=signature,sources=hashes,numpy=np.__version__,controlRule=rule.settings(),
            selection='Each risk Set chooses its policy by training net among training-qualified policies, independently. Holdout never changes that selection.',
            controlDataPreviouslyInspected=True),indent=2)+'\n')
        lib=out/'policy_sweep.so'
        subprocess.run(['g++','-O3','-std=c++17','-shared','-fPIC',str(ROOT/'scripts/policy_sweep.cpp'),'-o',str(lib)],check=True)
        sources=[prepare(pathlib.Path(a.data)/(s+'-USDT.json'),out) for s in ('BCH','SOL','XRP')]
        if len({(s['start'],s['end']) for s in sources})!=1:raise ValueError('Different symbol periods')
        tasks=[(s,k,side,str(out),str(lib),signature,rule) for s in sources for k in IND_KINDS for side in (1,-1)]
        results=[]
        with ProcessPoolExecutor(max_workers=max(1,a.workers)) as pool:
            fs=[pool.submit(execute,t) for t in tasks]
            for f in as_completed(fs):results.append(f.result())
    expected={f"{symbol}-USDT_{kind}_{side}" for symbol in ('BCH','SOL','XRP') for kind in IND_KINDS for side in ('LONG','SHORT')}
    if {r['key'] for r in results}!=expected or len(results)!=48 or any(r['signature']!=signature for r in results):
        raise ValueError('Missing, duplicate or stale result groups')
    if not validate_result_sources(results,pathlib.Path(a.data)):
        raise ValueError('Source candle contents changed; rerun the sweep')
    from render_control_report import render
    print(json.dumps(dict(summary=render(results,out,signature,rule),groups=len(results))),flush=True)


if __name__=='__main__':main()
