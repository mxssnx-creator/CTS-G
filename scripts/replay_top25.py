"""Select exactly 25 research candidates on training evidence; test Block/Axis.

No exchange API, remote source deployment, or production settings mutation.
Axis children use the actual Coordinator and SetBook stage qualification.
The historical shadow parent keeps producing CLOSED evidence while a child is
blocked; this is explicitly historical evidence, never exchange-confirmed data.
"""
import argparse, datetime as dt, gzip, hashlib, heapq, json, pathlib, sys, time
from concurrent.futures import ProcessPoolExecutor, as_completed
from types import SimpleNamespace
import numpy as np
from replay_complete import grids
from replay_five_days import replay
from coord_engine import Coordinator, AXIS_SPECS, consec_loss
from set_engine import SetBook
from position_cost import last_n_cost_pf
from block_engine import calculate_block_volume_increment_ratio, calculate_block_minimum_profit_factor

ROOT=pathlib.Path(__file__).resolve().parents[1]
AXES=[(axis,n) for axis,s in AXIS_SPECS.items() for n in range(s['min'],s['max']+1,s['step'])]

def pf_number(value):
    return float('inf') if value=='∞' else float(value or 0)

def identity(symbol,kind,direction,cfg):
    # Requested floors / catalog aliases must not fill the shortlist repeatedly.
    effective={k:cfg.get(k) for k in ('strategy','slPct','trailArmPct','trailGivePct','honorTp','maxHoldBars','scratchBars','scratchMinPct')}
    effective['tpPct']=cfg['tpPct'] if cfg.get('honorTp',True) else None
    return json.dumps([symbol,kind,direction,effective],sort_keys=True,separators=(',',':'))

def selection_key(row):
    # Holdout fields deliberately do not participate in this sort.
    r=row['selectionMetrics']
    return (-pf_number(r['trainPf']),r['trainDdPct'],-r['trainN'],row['identity'])

def select_top25(source, count=25):
    g=grids();short=[];examined=eligible=0
    for path in sorted(pathlib.Path(source).glob('20d_*.json.gz')):
        summary=json.loads(path.with_name(path.name.replace('.json.gz','.summary.json')).read_text())
        packed=json.loads(gzip.decompress(path.read_bytes()));columns=packed['columns'];ix={k:i for i,k in enumerate(columns)}
        distinct={}
        for values in packed['rows']:
            cfg=g[summary['family']][values[ix['config']]]
            if cfg['strategy'] not in ('base','trail'):continue
            examined+=1
            if values[ix['trainN']]<8:continue
            eligible+=1
            key=identity(summary['symbol'],summary['kind'],values[ix['direction']],cfg)
            if key in distinct:continue
            row=dict(identity=key,candidateId=hashlib.sha256(key.encode()).hexdigest()[:12],
                symbol=summary['symbol'],kind=summary['kind'],direction=values[ix['direction']],
                parameters=cfg,group=summary['key'],sourceConfig=values[ix['config']],
                selectionMetrics={k:values[ix[k]] for k in ('trainPf','trainN','trainDdPct')},
                originalMetrics=dict(zip(columns,values)))
            distinct[key]=row
        short.extend(heapq.nsmallest(count,distinct.values(),key=selection_key))
    chosen=sorted(short,key=selection_key)[:count]
    if len(chosen)!=count:raise ValueError(f'Fewer than {count} distinct sample-sufficient parent configurations')
    for rank,row in enumerate(chosen,1):row.update(rank=rank,acceptedFor='additional historical research tests',liveAccepted=False)
    return dict(candidates=chosen,examinedParentRows=examined,sampleSufficientParentRows=eligible,
        selected=count,rule='Train PF descending; Train DD ascending; Train N descending; deterministic effective-config identity',
        period='First 14 days of the 20-day sample select; final six days validate retrospectively',
        qualification=f'Research queue accepts {count}; outcome gate is independent: N>=8 and classic net PF>=1.05 in both segments; DD time<=16h',
        leakageNote='Earlier reports already exposed these dates. This is a retrospective chronological check, not an unseen forward test.')

def variants(parent,admission='strict'):
    rows=[]
    for axis,count in [('',0)]+AXES:
        for blocks,ratio in [(0,0.)]+[(n,r) for n in range(1,7) for r in (.25,.5,1.)]:
            cfg=dict(parent)
            cfg.update(strategy='block' if blocks else parent['strategy'],levels=blocks,
                incrementPct=.2 if blocks else 0,volumeRatio=ratio,
                entryVolumeRatio=count*.01 if axis else 1.,axis=axis,axisCount=count,
                admission=admission,
                mode='Axis + Block' if axis and blocks else ('Axis' if axis else ('Block' if blocks else 'Baseline')))
            rows.append(cfg)
    assert len(rows)==494
    return rows

def parent_observations(bars,signals,side,cfg,start_ms):
    tape=[];equity=np.zeros(len(bars));closed=[]
    def close(row):
        closed.append(row)
        cost=row['costFraction']*100
        tape.append(dict(t=(start_ms+row['bar']*60000)/1000,bar=row['bar'],
            qty=1.,entry=row['parentPrice'],pnl=row['netFraction']*row['parentPrice'],
            pnl_pct=row['netFraction']+row['costFraction'],position_cost_pct=cost,
            cost_source='research-model',exchange_confirmed=False,reason=row['reason']))
    metrics=replay(bars,signals,side,[cfg],on_close=close,on_equity=lambda i,e:equity.__setitem__(i,e[0]))[0]
    return metrics,tape,equity,closed

def causal_gates(bars,tape,equity,settings):
    """Snapshot after each shadow close; it becomes available on the next bar."""
    coord=Coordinator();coord.load(settings.get('cts',{}),settings.get('overlay',{}))
    book=SetBook();book.load(settings.get('overlay',{}))
    parent=SimpleNamespace(id='research-parent',kind='base',parent_set_id='')
    allowed=np.zeros((len(bars),len(AXES)),dtype=bool)
    isolated=np.zeros_like(allowed);block=np.zeros((len(bars),18),dtype=bool)
    history=[];events=[];cursor=0;last_axes=np.zeros(len(AXES),bool);last_isolated=last_axes.copy();last_block=np.zeros(18,bool)
    high=0.;age=0;max_age=0
    for i in range(60,len(bars)):
        # Only earlier bars can affect this bar's order decisions.
        previous=equity[i-1];high=max(high,previous)
        age=age+60 if previous<high-1e-12 else 0;max_age=max(max_age,age)
        changed=False
        while cursor<len(tape) and tape[cursor]['bar']<i:
            history.append(tape[cursor]);cursor+=1;changed=True
        if changed:
            metric=last_n_cost_pf(history,book.pf_n,book.cost_pct)
            ledger=book._stage_qualification(parent,dict(last15_n=metric['count'],last15_ratio=metric['ratio'],ddOk=max_age<=book.max_dd_s))
            base=bool(ledger['base'])
            # A separately labelled mechanics arm evaluates Axis children even
            # when the historical Base parent is unqualified. Native Axis
            # PF/sample tests stay intact; this arm can NEVER promote to live.
            children=coord.axis_variants(parent.id,history,[])
            bykey={v['axisKey']:v for v in children}
            last_isolated=np.array([bool(bykey.get(f'{a}:{n}',{}).get('qualified')) for a,n in AXES])
            last_axes=last_isolated & base
            losses=consec_loss([r['pnl'] for r in history])
            allow,reasons,cm=coord.add_gate(history,losses,intern={'pf':metric['ratio'],'n':metric['count']})
            stack=coord.add_stack_cap(6,cm.get('lastPf',1.))
            recent=last_n_cost_pf(history,8,book.cost_pct)
            # Live continuation PF floor and Block's base-1 incremental floor.
            last_block=np.array([bool(allow and n<=stack and (recent['count']<8 or recent['ratio']>=coord.min_pf)
                and metric['ratio']>=calculate_block_minimum_profit_factor(coord.min_pf,
                    settings.get('overlay',{}).get('blockProfitFactorRatio',1.25),calculate_block_volume_increment_ratio(n,r)))
                for n in range(1,7) for r in (.25,.5,1.)])
            events.append(dict(effectiveBar=i,closedThroughBar=history[-1]['bar'],parentN=int(metric['count']),
                parentCostPf=metric['ratio'],baseQualified=base,ddtS=max_age,qualifiedAxisCount=int(last_axes.sum()),
                isolatedAxisCount=int(last_isolated.sum()),
                allowedBlockVariants=int(last_block.sum()),axisRows=children,gateReasons=reasons))
        allowed[i]=last_axes;isolated[i]=last_isolated;block[i]=last_block
    return allowed,block,events,dict(baseFloor=book.stage_min_pf['base'],requiredSamples=book.eval_need(),
        maxDdtS=book.max_dd_s,positionCostPct=book.cost_pct,axes=coord.snapshot()['axes']),isolated

def evaluate_one(args):
    candidate,source,settings,out=args;started=time.monotonic()
    blob=json.loads((pathlib.Path(source)/(candidate['symbol']+'.prepared.json')).read_text())
    bars=blob['bars'];signals=blob['signals'][candidate['kind']];side=1 if candidate['direction']=='LONG' else -1
    start_ms=blob['end']-len(bars)*60000;cfg=candidate['parameters'];n=len(bars);split=60+int((n-60)*.7)
    base,tape,base_eq,closed=parent_observations(bars,signals,side,cfg,start_ms)
    # Selection source and exact same-candle rerun must agree on every metric.
    for key,value in candidate['originalMetrics'].items():
        if key not in ('config','direction') and base[key]!=value:raise AssertionError((candidate['candidateId'],key,base[key],value))
    axis_gate,block_gate,gate_events,policy,isolated_gate=causal_gates(bars,tape,base_eq,settings)
    all_cfg=variants(cfg)+variants(cfg,'isolated-mechanics');count=len(all_cfg)
    axis_index=np.array([AXES.index((c['axis'],c['axisCount'])) if c['axis'] else -1 for c in all_cfg])
    block_index=np.array([(c['levels']-1)*3+(.25,.5,1.).index(c['volumeRatio']) if c['levels'] else -1 for c in all_cfg])
    has_axis=axis_index>=0;has_block=block_index>=0
    isolated=np.array([c['admission']=='isolated-mechanics' for c in all_cfg])
    def entry(i):return (~has_axis)|np.where(isolated,isolated_gate[i,np.maximum(axis_index,0)],axis_gate[i,np.maximum(axis_index,0)])
    def add(i):
        direction,confidence=signals[i]
        return ((~has_block)|block_gate[i,np.maximum(block_index,0)]) & (direction==side) & (confidence>=.58)
    peak=np.zeros(count);dd=np.zeros(count);age=np.zeros(count);ddt=np.zeros(count)
    train_dd=None;train_net=None;test_peak=np.zeros(count);test_dd=np.zeros(count)
    samples=[];sample_bars=[];daily=[]
    def observe(i,e):
        nonlocal peak,dd,age,ddt,train_dd,train_net,test_peak,test_dd
        combined=e+np.where(has_axis,base_eq[i],0)
        peak=np.maximum(peak,combined);dd=np.maximum(dd,peak-combined)
        age=np.where(combined<peak-1e-12,age+60,0);ddt=np.maximum(ddt,age)
        if i==split-1:train_dd=dd.copy();train_net=combined.copy()
        if i>=split:
            local=combined-train_net;test_peak=np.maximum(test_peak,local);test_dd=np.maximum(test_dd,test_peak-local)
        if (i-60)%60==0 or i in (split-1,n-1):samples.append(combined.copy());sample_bars.append(i)
        if (i-60+1)%1440==0 or i==n-1:daily.append(combined.copy())
    results=replay(bars,signals,side,all_cfg,entry_filter=entry,add_filter=add,on_equity=observe)
    curves=np.array(samples).T;daily_eq=np.array(daily).T
    for k,r in enumerate(results):
        c=all_cfg[k]
        combined_net=r['netPct']+(base['netPct'] if c['axis'] else 0)
        r.update(parameters=c,variantId=f"{candidate['candidateId']}-{k:03d}",
            combinedNetPct=round(combined_net,6),combinedDdPct=round(dd[k]*100,6),combinedMaxDdtS=int(ddt[k]),
            combinedTrainNetPct=round(train_net[k]*100,6),combinedTrainDdPct=round(train_dd[k]*100,6),
            combinedHoldoutNetPct=round(combined_net-train_net[k]*100,6),combinedHoldoutDdPct=round(test_dd[k]*100,6),
            deltaNetPct=round(combined_net-base['netPct'],6),
            liveEligible=False,
            portfolioDailyNetPct=np.round(np.diff(np.r_[0,daily_eq[k]])*100,6).tolist())
    # Model-based shortlist choice uses only training PnL and drawdown.
    picks={}
    for admission,mode in [(a,m) for a in ('strict','isolated-mechanics') for m in ('Baseline','Block','Axis','Axis + Block')]:
        eligible=[r for r in results if r['parameters']['mode']==mode and r['parameters']['admission']==admission]
        picked=min(eligible,key=lambda r:(-r['combinedTrainNetPct'],r['combinedTrainDdPct'],r['config']))
        picks[admission+':'+mode]=dict(variantId=picked['variantId'],config=picked['config'],curve=np.round(curves[picked['config']]*100,6).tolist())
    payload=dict(candidate=candidate,baseline=base,variants=results,policy=policy,
        gateEvents=gate_events,parentClosedTape=tape,parentExecutions=closed,
        selectedByTraining=picks,curveTimes=[start_ms+i*60000 for i in sample_bars],
        elapsedS=round(time.monotonic()-started,2))
    dest=pathlib.Path(out)/(candidate['candidateId']+'.top25.json.gz')
    dest.write_bytes(gzip.compress(json.dumps(payload,separators=(',',':'),allow_nan=False).encode(),mtime=0))
    return dict(candidateId=candidate['candidateId'],rank=candidate['rank'],variants=len(results),
        qualified=sum(r['qualified'] for r in results),axisQualifiedDecisions=sum(e['qualifiedAxisCount'] for e in gate_events),
        elapsedS=payload['elapsedS'],sha256=hashlib.sha256(dest.read_bytes()).hexdigest())

def main():
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--settings',required=True)
    p.add_argument('--output',required=True);p.add_argument('--workers',type=int,default=2);p.add_argument('--select-only',action='store_true');a=p.parse_args()
    out=pathlib.Path(a.output);out.mkdir(parents=True,exist_ok=True)
    chosen=select_top25(a.source);(out/'top25-selection.json').write_text(json.dumps(chosen,indent=2))
    print('Selected 25 distinct training-ranked parent configurations',flush=True)
    if a.select_only:return
    settings=json.loads(pathlib.Path(a.settings).read_text());summaries=[]
    with ProcessPoolExecutor(max_workers=min(2,max(1,a.workers))) as pool:
        jobs=[pool.submit(evaluate_one,(c,a.source,settings,str(out))) for c in chosen['candidates']]
        for future in as_completed(jobs):
            row=future.result();summaries.append(row);print(json.dumps(row),flush=True)
    (out/'top25-summary.json').write_text(json.dumps(dict(candidates=25,variants=sum(r['variants'] for r in summaries),
        results=sorted(summaries,key=lambda r:r['rank'])),indent=2))

if __name__=='__main__':main()
