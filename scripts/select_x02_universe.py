#!/usr/bin/env python3
"""Select 50 disjoint VST symbols using contract, liquidity and data checks only.

Only unauthenticated public market GETs are used. No return, PF, strategy score
or account-setting mutation participates in selection.
"""
import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server/pulse'))
from bingx_fast import FastBingX,ErrorLog
from set_engine import SetBook
from pulse_trader import Pulse
from x02_native_session import HOST,CONN,atomic_json,frozen_overlay,seed_history


def ordinary_crypto_contract(row):
    symbol=row.get('symbol','');asset=str(row.get('asset',symbol.removesuffix('-USDT')))
    return (symbol.endswith('-USDT') and str(row.get('status'))=='1'
            and str(row.get('apiStateOpen','true')).lower()=='true'
            and row.get('displayName',symbol)==symbol
            and not asset.startswith(('NCFX','NCSK','NCCM','NCIX'))
            and asset not in ('XAU','XAG','WTI','BRENT'))


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--exclude',nargs='+',type=Path,required=True)
    ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--symbol-count',type=int,default=50,choices=range(1,51))
    ap.add_argument('--overlay',type=Path,default=ROOT/'server/pulse/overlay-bingx-x02.json')
    ap.add_argument('--seed',type=Path,help='reuse recent, validated VST candles without re-downloading them')
    args=ap.parse_args()
    if args.out.exists():ap.error('choose a new market snapshot directory')
    args.out.mkdir(mode=0o700,parents=True)
    api=FastBingX('','',ErrorLog(str(args.out/'public-errors.jsonl')),base=HOST)
    def read(path,params=None):
        for attempt in range(3):
            response=api.public(path,params)
            if isinstance(response,dict) and str(response.get('code'))=='0':return response['data']
            if attempt<2:time.sleep(1)
        raise RuntimeError('public VST read unavailable: '+path)
    excluded=set()
    for path in args.exclude:
        data=json.loads(path.read_text());excluded.update(data['symbols']if isinstance(data,dict)else data)
    contracts=read('/openApi/swap/v2/quote/contracts')
    tickers=read('/openApi/swap/v2/quote/ticker')
    quotes={row['symbol']:row for row in tickers}
    def volume(symbol):
        row=quotes.get(symbol,{})
        try:return float(row.get('quoteVolume')or row.get('volume')or 0)
        except (ValueError,TypeError):return 0
    candidates=sorted((row['symbol']for row in contracts if ordinary_crypto_contract(row)
                       and row['symbol']not in excluded),key=lambda s:(-volume(s),s))
    count=args.symbol_count
    if len(candidates)<count:raise RuntimeError('fewer disjoint active contracts than requested')
    raw=json.loads(args.overlay.read_text());book=SetBook();book.load(frozen_overlay(raw,candidates[:count],symbol_count=count))
    if args.seed:seed_history(book,args.seed,candidates,time.time())
    seeded=frozenset(book.bars)
    target=book.history_bars()+1
    if target>4320:raise ValueError('history including context exceeds the 72-hour budget')
    parser=Pulse.__new__(Pulse)
    def fetch(symbol):
        try:
            if symbol in seeded and len(book.bars[symbol])>=book.history_bars()-1 and book.usable(symbol):
                return symbol,{'lastClosedBarAt':book.bar_t[symbol],'bars':book.bars[symbol]},''
            unique={};params={'symbol':symbol,'interval':'1m','limit':1000}
            previous=None
            for page in range(8):
                rows=read('/openApi/swap/v2/quote/klines',params)
                if not isinstance(rows,list)or not rows:return symbol,None,'missing bars'
                unique.update((parser._kline_time(row),row)for row in rows)
                bars,last=parser._closed_klines([unique[k]for k in sorted(unique)])
                book.ingest_bars(symbol,bars,last_t=last)
                why=book.bar_quality(symbol)
                if why and why!='short':return symbol,None,why
                oldest=min(unique)
                if previous is not None and oldest>=previous:return symbol,None,'no backward page progress'
                if len(unique)>=target:break
                previous=oldest
                params={**params,'limit':min(1000,target-len(unique)+1),'endTime':oldest-1}
            if len(bars)<book.history_bars()-1:return symbol,None,'insufficient contiguous history'
            closed=[k for k in sorted(unique)if k/1000+60<=time.time()]
            clocks=closed[-len(bars):]
            if any(b-a!=60000 for a,b in zip(clocks,clocks[1:])):return symbol,None,'minute gap'
            if not book.usable(symbol):return symbol,None,book.bar_quality(symbol)
            return symbol,{'lastClosedBarAt':last,'bars':bars},''
        except Exception as exc:return symbol,None,type(exc).__name__
    selected={};rejected={};todo=iter(candidates)
    with ThreadPoolExecutor(max_workers=4,thread_name_prefix='vst-public') as pool:
        pending=deque()
        for _ in range(4):
            symbol=next(todo,None)
            if symbol:pending.append((symbol,pool.submit(fetch,symbol)))
        while pending and len(selected)<count:
            symbol,future=pending.popleft();symbol,data,why=future.result()
            if data:selected[symbol]=data
            else:rejected[symbol]=why
            print(json.dumps({'selected':len(selected),'checked':len(selected)+len(rejected),'symbol':symbol,'quality':why or 'valid'}),flush=True)
            atomic_json(args.out/'progress.json',{'selected':list(selected),'rejected':rejected,'excluded':sorted(excluded)})
            if len(selected)<count:
                symbol=next(todo,None)
                if symbol:pending.append((symbol,pool.submit(fetch,symbol)))
        for _,future in pending:future.cancel()
    if len(selected)!=count:raise RuntimeError('could not obtain all requested valid disjoint market histories')
    symbols=list(selected)
    atomic_json(args.out/'session.json',{'host':HOST,'connection':CONN,'symbols':symbols,
                'selection':'ordinary crypto contracts with matching symbol/display name; synthetic aliases/commodities excluded; descending reported quote volume; full candle quality/continuity; no PnL selection',
                'excluded':sorted(excluded),'rejected':rejected,'historyBars':book.history_bars(),
                'sourceSha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    atomic_json(args.out/'candles.json',{'capturedAt':time.time(),'barSeconds':60,'symbols':selected})
    atomic_json(args.out/'symbols.json',symbols)
    print('Prepared 50 disjoint, valid VST histories',flush=True)


if __name__=='__main__':main()
