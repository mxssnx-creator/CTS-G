"""Top-50 adjusted-only candle experiment. Historical shadow evidence, no API.

Count/ratio cells are alternatives, not an aggregated exchange portfolio.
Exchange minimum size, latency and the current account's live gate are not
modeled. The actual order-boundary policy is tested separately.
"""
import argparse
import json
import pathlib
import time
import numpy as np
from replay_top25 import select_top25, parent_observations, causal_gates, BLOCK_RATIOS
from replay_five_days import replay
from block_active import observe_continuation
from concurrent.futures import ProcessPoolExecutor, as_completed


def run_one(args):
    candidate, source, settings = args
    blob=json.loads((pathlib.Path(source)/(candidate['symbol']+'.prepared.json')).read_text())
    bars=blob['bars']; signals=blob['signals'][candidate['kind']]
    side=1 if candidate['direction']=='LONG' else -1
    start=blob['end']-len(bars)*60000
    base,tape,eq,closed=parent_observations(bars,signals,side,candidate['parameters'],start)
    _, block, events, policy, _ = causal_gates(bars,tape,eq,settings)
    # Real-stage qualification is separate from the lower Base Axis gate.
    real_floor=float(settings.get('overlay',{}).get('realMinPf',1.05))
    real=np.zeros(len(bars),dtype=bool)
    for index,event in enumerate(events):
        end=events[index+1]['effectiveBar'] if index+1<len(events) else len(bars)
        real[event['effectiveBar']:end]=(event['parentN']>=max(8,policy['requiredSamples'])
            and event['parentCostPf']>=real_floor and event['ddtS']<=policy['maxDdtS'])
    continuation=np.zeros(len(bars),dtype=bool);anchors={}
    for i in range(60,len(bars)):
        direction,confidence=signals[i]
        if direction!=side or confidence<.58:
            anchors.clear();continue
        continuation[i]=observe_continuation(anchors,'reference',float(bars[i][3]),side,i*60)
    configs=[]
    for count in range(1,7):
        for ratio in BLOCK_RATIOS:
            # This is a real adjusted Block lane: the parent starts at one
            # unit and the weighted average/quantity changes only when the
            # causal Block gate permits the configured count/ratio.
            c=dict(candidate['parameters'],strategy='block',levels=count,
                   incrementPct=.2,volumeRatio=ratio,entryVolumeRatio=1.,
                   blockCount=count,blockRatio=ratio)
            configs.append(c)
    own_net=[[] for _ in configs]
    def entry(i):
        return block[i] & real[i] & continuation[i] & np.array([
            not rows or sum(rows[-25:])>0 for rows in own_net])
    def close(row):own_net[row['config']].append(row['netFraction'])
    results=replay(bars,signals,side,configs,entry_filter=entry,on_close=close)
    for row,c in zip(results,configs):
        row.update(blockCount=c['blockCount'],blockRatio=c['blockRatio'],
                   referenceVolume=1,adjustedVolume=row['maxVolume'],normalVolume=0,
                   deltaNetPct=round(row['netPct']-base['netPct'],6))
        assert row['maxVolume'] <= 1 + min(1, c['levels']*c['blockRatio']) + 1e-9
        assert row['n']==sum(row['dailyN'])
    return dict(candidate=candidate,baseline=base,results=results,
                qualified=sum(r['qualified'] for r in results),
                continuationBars=int(continuation.sum()),realQualifiedBars=int(real.sum()))


def main():
    p=argparse.ArgumentParser();p.add_argument('--source',required=True)
    p.add_argument('--settings',required=True);p.add_argument('--output',required=True)
    p.add_argument('--count',type=int,default=80,help='number of parent lanes to test')
    p.add_argument('--window',choices=('20h','5d','14d','20d'),default='20h')
    p.add_argument('--all-eligible',action='store_true',help='include sample-sufficient but unqualified parents')
    a=p.parse_args();started=time.monotonic()
    selected=select_top25(a.source,a.count,a.window,qualified_only=not a.all_eligible);settings=json.loads(pathlib.Path(a.settings).read_text())
    results=[]
    with ProcessPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(run_one,(r,a.source,settings)) for r in selected['candidates']]
        for f in as_completed(futures):
            r=f.result();results.append(r);print('Completed',r['candidate']['rank'],flush=True)
    out=dict(selection=selected,parents=a.count,variants=a.count*6*len(BLOCK_RATIOS),
             qualified=sum(r['qualified'] for r in results),
             elapsedS=round(time.monotonic()-started,2),
             results=sorted(results,key=lambda r:r['candidate']['rank']),
             limitation='Historical independent alternatives; 1-minute continuation resolution; shadow coordination evidence; no live fills, minimum quantities, funding or combined exchange account modeled.')
    pathlib.Path(a.output).write_text(json.dumps(out,allow_nan=False))


if __name__=='__main__':main()
