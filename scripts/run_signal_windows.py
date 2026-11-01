"""Complete source-catalogue independent-window replay, one capital book per source.

Each source is a separate diagnostic scenario, NOT summed into one portfolio.
The source event producer is pinned, and all supplied configurations are run.
"""
import argparse
from collections import defaultdict,Counter
import concurrent.futures
import gzip
import hashlib
import json
from pathlib import Path
import resource
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server/pulse'))
from signal_progress import SignalProgress
from signal_replay import open_position,advance_position,close_result,hourly_audit,canonical
from signal_windows import metrics


def replay_source(data,series,cfg,start,trade_start,end):
    book=SignalProgress(cfg['progress'])
    for stream in series:
        for p in cfg['protections']:
            for side in (-1,1):book.register({**stream['identity'],'side':side},p)
    index=defaultdict(lambda:defaultdict(dict))
    for stream in series:
        key=book.event_key(stream['identity'])
        for t,side in stream['events']:
            if start<=t<end:index[t][key[0]][key]=side
    risk,fee=cfg['risk'],cfg['progress']['fees']
    cash=peak=risk['initialEquity'];max_dd=0;latched=False
    opened=0;open_book={};trades=[];marks=[];admission_marks=[];rejects=Counter();stage_progress=[]
    hour_start=cash;last_hour=None;prefreeze=None
    def mark(prices,t):
        equity=cash
        for item in open_book.values():
            p=item['position'];px=prices[item['candidate']['identity']['symbol']]*(1-p.side*fee['slippagePerLeg'])
            equity+=p.qty*(px-p.entry)*p.side-p.qty*px*fee[fee['exitLiquidity']]
        return {'t':t,'equity':equity,'openOrders':len(open_book)}
    def observe(prices,t):
        nonlocal peak,max_dd,latched
        row=mark(prices,t);admission_marks.append({**row,'openedCount':opened})
        peak=max(peak,row['equity']);max_dd=max(max_dd,(peak-row['equity'])/peak)
        latched|=max_dd>=risk['maxDrawdown']
        return row
    step_times=[]
    for i,t in enumerate(range(start,end,60000)):
        prices={s:rows[i][1] for s,rows in data.items()}
        if t==trade_start:prefreeze=book.summary(t)
        if t>=trade_start:
            before=observe(prices,t)
            if not marks:marks.append(before)
            if t//3600000!=last_hour:hour_start=marks[-1]['equity'];last_hour=t//3600000
        began=time.perf_counter();candidates=[]
        for symbol,rows in data.items():candidates.extend(book.step(symbol,rows[i],index.get(t,{}).get(symbol,{})))
        step_times.append(time.perf_counter()-began)
        if t>=trade_start:
            for c in sorted(candidates,key=lambda c:(c['real']['maxReturnDrawdown'],-c['real']['net'],c['id'])):
                if c['id'] in open_book:continue
                current=observe(prices,t)['equity']
                exposure=sum(x['position'].qty*prices[x['candidate']['identity']['symbol']] for x in open_book.values())
                reason='drawdown' if latched else 'hourly-loss' if (hour_start-current)/hour_start>=risk['maxHourlyLoss'] else 'max-open' if len(open_book)>=risk['maxOpen'] else 'exposure' if exposure+risk['notionalPerOrder']>min(risk['maxGrossExposure'],current) else ''
                if reason:rejects[reason]+=1;continue
                pos=open_position(t,prices[c['identity']['symbol']],c['identity']['side'],c['protection'],fee,risk['notionalPerOrder'])
                cash-=pos.entry_fee;opened+=1
                open_book[c['id']]={'candidate':c,'position':pos,'entrySequence':opened}
                observe(prices,t)
            for cid,item in list(open_book.items()):
                c,pos=item['candidate'],item['position'];symbol=c['identity']['symbol'];p=c['protection']
                opposite=index.get(t,{}).get(symbol,{}).get(book.event_key(c['identity']))==-pos.side
                if p['strategyRange']=='exit' and opposite and pos.entry_t<t:
                    result=close_result(pos,prices[symbol],t,'source-exit',fee)
                else:result=advance_position(pos,data[symbol][i],p,fee)
                if result:
                    cash+=result['netCash']+pos.entry_fee
                    trades.append({**result,'context':cid,'identity':c['identity'],'protection':p,'baseRule':c['baseRule'],
                        'mainRule':c['mainRule'],'admission':{'base':c['base'],'main':c['main'],'real':c['real']},'entrySequence':item['entrySequence']})
                    del open_book[cid]
            row=mark({s:rows[i][4] for s,rows in data.items()},t+60000);marks.append(row)
            peak=max(peak,row['equity']);max_dd=max(max_dd,(peak-row['equity'])/peak);latched|=max_dd>=risk['maxDrawdown']
        if (t+60000)%3600000==0:stage_progress.append(book.summary(t+60000))
    end_summary=book.summary(end,detail=True)
    # Duplicate window policy listings are stored once in final, not every hour.
    for snap in stage_progress:
        snap.pop('basePolicy',None);snap.pop('mainPolicy',None)
    return {'schema':'cts-g-signals-window-result-v2','scope':'separate modeled source portfolio; not exchange fills or fresh holdout',
        'config':cfg,'window':{'start':start,'tradeStart':trade_start,'end':end},'source':series[0]['identity']['source'],
        'series':len(series),'final':end_summary,'prehistory':prefreeze,'stageProgress':stage_progress,
        'trades':trades,'openPositions':[{'context':k,'candidate':v['candidate'],'position':v['position'].__dict__,'entrySequence':v['entrySequence']} for k,v in open_book.items()],
        'marks':marks,'admissionMarks':admission_marks,'modeledExecution':metrics([r['unitNet'] for r in trades]),
        'initialEquity':risk['initialEquity'],'finalEquity':marks[-1]['equity'],'maxEquityDrawdown':max_dd,'riskRejections':dict(rejects),
        'counts':{'opened':opened,'settled':len(trades),'open':len(open_book)},'hourly':hourly_audit(marks,trades,trade_start,end),
        'stepPerformance':{'maxMs':max(step_times)*1000,'p95Ms':sorted(step_times)[int(len(step_times)*.95)]*1000,'meanMs':sum(step_times)/len(step_times)*1000},
        'limitations':['No live order adapter, DCA/Block/Relate position stacks, partial exchange fills or liquidation model',
                       'Sources are separate capital scenarios; results cannot be summed as one funded portfolio',
                       'No untouched holdout; no guaranteed positive future or hourly PF']}


def worker(job):
    name,series,data,cfg,start,trade_start,end,out,provenance=job
    target=Path(out)/(hashlib.sha256(name.encode()).hexdigest()[:16]+'.json.gz')
    began=time.perf_counter();result=replay_source(data,series,cfg,start,trade_start,end)
    result['provenance']=provenance
    result['performance']={'seconds':time.perf_counter()-began,'peakRssKiB':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
    target.write_bytes(gzip.compress(canonical(result).encode(),mtime=0))
    return {'source':name,'file':target.name,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
        'counts':result['counts'],'execution':result['modeledExecution'],'netEquity':result['finalEquity']-result['initialEquity'],
        'drawdown':result['maxEquityDrawdown'],'hourly':result['hourly'],'performance':result['performance'],
        'stepPerformance':result['stepPerformance'],'final':{k:v for k,v in result['final'].items() if k not in ('records','basePolicy','mainPolicy')},
        'prehistory':{k:v for k,v in result['prehistory'].items() if k not in ('basePolicy','mainPolicy')}}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--feed',type=Path,required=True);ap.add_argument('--events',type=Path,required=True)
    ap.add_argument('--config',type=Path,required=True);ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--sources',default='all');ap.add_argument('--jobs',type=int,default=2)
    ap.add_argument('--prehistory-hours',type=int,default=100)
    args=ap.parse_args()
    if args.out.exists():ap.error('preserve earlier study outputs')
    cfg=json.loads(args.config.read_text())
    if set(cfg)!={'schema','mode','progress','protections','risk'} or cfg['schema']!='cts-g-signals-windows-v2' or cfg['mode']!='offline-only':ap.error('unsupported research configuration')
    risk=cfg['risk']
    if set(risk)!={'initialEquity','notionalPerOrder','maxOpen','maxGrossExposure','maxDrawdown','maxHourlyLoss'} or any(type(v) not in (int,float) or not 0<v<1e9 for v in risk.values()):ap.error('invalid explicit risk controls')
    if type(risk['maxOpen']) is not int or not risk['notionalPerOrder']<=risk['maxGrossExposure']<=risk['initialEquity'] or max(risk['maxDrawdown'],risk['maxHourlyLoss'])>=1:ap.error('invalid book bounds')
    feedbytes=args.feed.read_bytes();raw=json.loads(gzip.decompress(feedbytes) if args.feed.suffix=='.gz' else feedbytes)
    eventbytes=args.events.read_bytes();event=json.loads(gzip.decompress(eventbytes) if args.events.suffix=='.gz' else eventbytes)
    if event['provenance']['feedSha256']!=hashlib.sha256(feedbytes).hexdigest():ap.error('source events and candles differ')
    start,end=event['provenance']['start'],event['provenance']['end']
    if (not 12<=args.prehistory_hours<=720 or end-start!=(args.prehistory_hours+12)*3600000 or
            cfg['progress']['historyMinutes']>args.prehistory_hours*60):
        ap.error('complete configured prehistory plus 12h trading required')
    trade_start=start+args.prehistory_hours*3600000
    data={s:[[r['t'],r['o'],r['h'],r['l'],r['c'],r['v']] for r in raw['candles'][s+'|1'] if start<=r['t']<end] for s in event['provenance']['symbols']}
    groups=defaultdict(list)
    for stream in event['series']:groups[stream['identity']['source']].append(stream)
    names=sorted(groups) if args.sources=='all' else args.sources.split(',')
    if len(set(names))!=len(names) or any(n not in groups for n in names):ap.error('unknown/repeated study source')
    files=[ROOT/'server/pulse'/n for n in ('signal_progress.py','signal_windows.py','signal_replay.py')]+[Path(__file__)]
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    provenance={'eventsSha256':hashlib.sha256(eventbytes).hexdigest(),'feedSha256':hashlib.sha256(feedbytes).hexdigest(),
        'configSha256':hashlib.sha256(canonical(cfg).encode()).hexdigest(),'sourceSha256':hashes,'unseenHoldout':False}
    args.out.mkdir(parents=True)
    (args.out/'plan.json').write_text(canonical({'sources':names,'config':cfg,'provenance':provenance}))
    jobs=[(name,groups[name],data,cfg,start,trade_start,end,str(args.out),provenance) for name in names]
    summaries=[]
    with concurrent.futures.ProcessPoolExecutor(max_workers=max(1,min(4,args.jobs))) as pool:
        future={pool.submit(worker,job):job[0] for job in jobs}
        for f in concurrent.futures.as_completed(future):
            summary=f.result();summaries.append(summary)
            (args.out/'summary.json').write_text(canonical(sorted(summaries,key=lambda x:x['source'])))
            print(json.dumps({'finished':len(summaries),'total':len(names),'source':summary['source'],'net':summary['netEquity'],'pf':summary['execution']['pf'],
                              'settled':summary['counts']['settled'],'seconds':summary['performance']['seconds']}),flush=True)
    if any(hashlib.sha256(p.read_bytes()).hexdigest()!=hashes[str(p.relative_to(ROOT))] for p in files):raise ValueError('Sources changed during study')


if __name__=='__main__':main()
