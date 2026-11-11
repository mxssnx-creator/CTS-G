"""Independent scalar reconstruction of archived Signals progression paths.

Gate, position, trailing and accounting calculations below do not import the
production implementations. The CLI compares sampled paths on frozen candles
with both the archived study and a fresh event-engine replay.
"""
from __future__ import annotations
import argparse
import gzip
import hashlib
import itertools
import json
import math
from pathlib import Path
import sys
import time


def policy(raw):
    return [(a,b,n,mode) for a,b,n,mode in itertools.product(raw['long'],raw['short'],raw['minimal'],raw['polarity'])
            if b<=a*raw['shortRatio']]


def rule_id(r):return f'l{r[0]}-s{r[1]}-m{r[2]}-{r[3]}'


def positive(values,n,cfg):
    if len(values)!=n:return False
    gains=sum(x for x in values if x>0);losses=-sum(x for x in values if x<0)
    total=high=dd=0
    for x in values:total+=x;high=max(high,total);dd=max(dd,high-total)
    return gains>0 and (losses==0 or gains/losses>=cfg['minPf']) and dd<=cfg['maxContextDrawdown']


def gate(history,t,rule,cfg):
    a,b,n,mode=rule
    rows=[v for end,v in history[-(a+n+1):] if t-cfg['historyMinutes']*60000<=end<t]
    if len(rows)<a+n:return False
    last=rows[-n:]
    recent=positive(last,n,cfg) if mode=='positive' else sum(last)<0
    return recent and positive(rows[-n-a:-n],a,cfg) and positive(rows[-n-b:-n],b,cfg)


def reconstruct(bars,events,br,mr,side,p,cfg):
    histories={s:[] for s in ('base','main','real')};positions={s:None for s in histories}
    candidates=[];opened=dict.fromkeys(histories,0);fee=cfg['fees']
    entry_fee=fee[fee['entryLiquidity']];exit_fee=fee[fee['exitLiquidity']];slip=fee['slippagePerLeg']
    for t,o,h,low,c,_ in bars:
        signal=events.get(t)
        if signal==side:
            base=gate(histories['base'],t,br,cfg);main=gate(histories['main'],t,mr,cfg)
            n=cfg['realLastN']
            if n=='coordinated':real_ok=main
            else:
                n=mr[2] if n=='lane-minimal' else n
                real=[v for end,v in histories['real'][-n-1:] if t-cfg['historyMinutes']*60000<=end<t][-n:]
                real_ok=positive(real,n,cfg)
            if base and main and real_ok:candidates.append(t)
            for stage,admit in [('base',True),('main',base),('real',base and main)]:
                if admit and positions[stage] is None:
                    entry=o*(1+side*slip)
                    positions[stage]={'t':t,'entry':entry,'stop':entry*(1-side*p['sl']),'best':entry}
                    opened[stage]+=1
        for stage,pos in positions.items():
            if pos is None:continue
            end=t+60000;exit_=None;entry=pos['entry'];stop=pos['stop']
            if p['strategyRange']=='exit' and signal==-side and pos['t']<t:exit_=o;end=t
            elif side*(o-stop)<=0:exit_=o
            elif (low<=stop if side>0 else h>=stop):exit_=stop
            elif p['tp'] and (h>=entry*(1+p['tp']) if side>0 else low<=entry*(1-p['tp'])):
                exit_=entry*(1+side*p['tp'])
            else:
                if p['kind']!='normal':
                    pos['best']=max(pos['best'],h) if side>0 else min(pos['best'],low)
                    if side*(pos['best']/entry-1)>=p['trailArm']:
                        new=pos['best']-side*entry*p['trailGap']
                        pos['stop']=max(stop,new) if side>0 else min(stop,new)
                        if side*(c-pos['stop'])<=0:exit_=pos['stop']
                if exit_ is None and t+60000-pos['t']>=p['holdMinutes']*60000:exit_=c
            if exit_ is not None:
                actual=exit_*(1-side*slip)
                net=side*(actual-entry)/entry-entry_fee-actual/entry*exit_fee
                histories[stage].append((end,net));positions[stage]=None
    return histories,positions,opened,candidates


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    for key in ('study','events','feed','out'):ap.add_argument('--'+key,type=Path,required=True)
    ap.add_argument('--roots',type=int,default=24);args=ap.parse_args()
    from collections import defaultdict
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server/pulse'))
    from signal_progress import SignalProgress
    def read(path):return json.loads(gzip.decompress(path.read_bytes()) if path.suffix=='.gz' else path.read_bytes())
    plan=read(args.study/'plan.json');summaries=read(args.study/'summary.json');events=read(args.events);feed=read(args.feed)
    if hashlib.sha256(args.events.read_bytes()).hexdigest()!=plan['provenance']['eventsSha256']:raise ValueError('different events')
    if hashlib.sha256(args.feed.read_bytes()).hexdigest()!=plan['provenance']['feedSha256']:raise ValueError('different feed')
    cfg=plan['config']['progress'];brules=policy(cfg['base']);mrules=policy(cfg['main'])
    by_rule={rule_id(r):r for r in brules};by_main={rule_id(r):r for r in mrules}
    streams={tuple(sorted(s['identity'].items())):s for s in events['series']}
    selected=[];covered=set()
    ranked=sorted(summaries,key=lambda r:(-r['final']['stages']['real']['closed'],-r['final']['stages']['main']['closed'],r['source']))
    for summary in ranked:
        archive_path=args.study/summary['file']
        if hashlib.sha256(archive_path.read_bytes()).hexdigest()!=summary['sha256']:raise ValueError('archive hash changed')
        result=read(archive_path);roots=defaultdict(list)
        for r in result['final']['records']:roots[r['root']].append(r)
        candidates=sorted(roots.items(),key=lambda kv:(-sum(r['cumulative']['n'] for r in kv[1] if r['stage']=='real'),-len(kv[1]),kv[0]))
        for rid,records in candidates:
            r=records[0];signature=(r['protection']['kind'],r['protection']['strategyRange'],r['identity']['side'])
            if signature in covered and len(selected)<12:continue
            selected.append((result['window'],records,summary['source']));covered.add(signature)
            if len(selected)>=args.roots:break
            if len(covered)>=12 and len(selected)%2==0:break
        if len(selected)>=args.roots:break
    began=time.perf_counter();checks=[];paths=0
    for window,records,source in selected:
        raw=records[0];ident=raw['identity'];p=raw['protection'];symbol=ident['symbol']
        stream=streams[tuple(sorted((k,v) for k,v in ident.items() if k!='side'))]
        signals=dict(stream['events'])
        bars=[[r[k] for k in ('t','o','h','l','c','v')] for r in feed['candles'][symbol+'|1'] if window['start']<=r['t']<window['end']]
        book=SignalProgress(cfg);rid=book.register(ident,p);key=book.event_key(ident);actual_candidates=[]
        for bar in bars:actual_candidates.extend(book.step(symbol,bar,{key:signals[bar[0]]} if bar[0] in signals else {}))
        root=book.roots[rid];root_checks=0
        pairs=set((rule_id(brules[i]),rule_id(mrules[(i*7)%len(mrules)])) for i in range(0,len(brules),5))
        pairs.update(tuple(r['path']) for r in records if r['stage']=='real')
        # Every main stream observed in this root is represented at least once.
        pairs.update((r['path'],rule_id(mrules[0])) for r in records if r['stage']=='main')
        for br,mr in sorted(pairs):
            hist,pos,opened,times=reconstruct(bars,signals,by_rule[br],by_main[mr],ident['side'],p,cfg)
            for stage,node in [('base',root.base),('main',root.main.get(br)),('real',root.real.get((br,mr)))]:
                values=[v for _,v in hist[stage]];count=node.history.count if node else 0
                net=node.history.cumulative()['net'] if node else 0
                if count!=len(values) or not math.isclose(net,sum(values),rel_tol=1e-10,abs_tol=1e-11) or bool(node and node.position)!=bool(pos[stage]) or (node.opened if node else 0)!=opened[stage]:
                    raise AssertionError((source,rid,br,mr,stage,'scalar divergence',count,len(values),net,sum(values)))
                root_checks+=1
            actual=[c['decisionT'] for c in actual_candidates if c['baseRule']==br and c['mainRule']==mr]
            if actual!=times:raise AssertionError((source,rid,br,mr,'admission divergence'))
            paths+=1
        for r in records:
            node=root.base if r['stage']=='base' else root.main.get(r['path']) if r['stage']=='main' else root.real.get(tuple(r['path']))
            if node is None or node.history.count!=r['cumulative']['n'] or not math.isclose(node.history.cumulative()['net'],r['cumulative']['net'],abs_tol=1e-11) or bool(node.position)!=r['open']:
                raise AssertionError((source,rid,r['stage'],r['path'],'archive divergence'))
        checks.append({'source':source,'root':rid,'identity':ident,'protection':p['id'],'paths':len(pairs),'stageChecks':root_checks,'archivedStreams':len(records)})
        print(json.dumps(checks[-1]),flush=True)
    result={'schema':'signals-window-scalar-audit-v1','passed':True,'roots':len(checks),'paths':paths,'checks':checks,
        'seconds':time.perf_counter()-began,'scope':'deterministic sample; all 1764 policy pairs separately covered on controlled fixtures',
        'provenance':plan['provenance'],'oracleSha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    args.out.write_text(json.dumps(result,indent=2,allow_nan=False))


if __name__=='__main__':main()
