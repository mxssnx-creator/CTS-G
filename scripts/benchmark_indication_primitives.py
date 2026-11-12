#!/usr/bin/env python3
"""Alternate identical ordinary-pack signals against the pre-optimization math."""
import argparse,contextlib,hashlib,json,statistics,sys,time
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server/pulse'))
import indication_lib as lib
import indication_extra as extra
import set_engine as se
from test_indication_primitives import old_atr,old_ema,old_rsi,old_wma,old_kama


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--market',type=Path,required=True)
    ap.add_argument('--overlay',type=Path,required=True)
    ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--windows',type=int,default=12)
    ap.add_argument('--repeats',type=int,default=5)
    args=ap.parse_args()
    raw=args.market.read_bytes();rows=json.loads(raw)
    bars=[r[1:6]for r in rows];ov=json.loads(args.overlay.read_text());book=se.SetBook();book.load(ov)
    start=len(bars)-args.windows
    results=[];expected=None
    for repetition in range(args.repeats):
        for mode in (('plain','optimized')if repetition%2==0 else('optimized','plain')):
            with contextlib.ExitStack()as stack:
                if mode=='plain':
                    families=dict(lib.FAMILIES);families['kama']=(families['kama'][0],old_kama)
                    stack.enter_context(patch.multiple(lib,_atr=old_atr,_ema=old_ema,_rsi=old_rsi,_wma=old_wma,FAMILIES=families))
                    stack.enter_context(patch.dict(extra.INDICATORS,lib.build()))
                lib._tls.memo={};lib.clear_vector_memo()
                before=time.perf_counter()
                signals=[se.bar_signal(bars,i,book.packs,book.ind_settings,rows[i][0]/1000+60)for i in range(start,len(bars))]
                elapsed=time.perf_counter()-before
                digest=hashlib.sha256(json.dumps(signals,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
                if expected is None:expected=digest
                if digest!=expected:raise AssertionError('Optimized directions/confidences/reasons differ from the plain oracle')
                result=dict(repetition=repetition,mode=mode,seconds=elapsed,sha256=digest,vectorMemo=lib.vector_memo_info())
                results.append(result)
                print(json.dumps({'mode':mode,'seconds':round(elapsed,4),'repetition':repetition}),flush=True)
    ratios=[next(x['seconds']for x in results if x['mode']=='plain'and x['repetition']==i)/next(x['seconds']for x in results if x['mode']=='optimized'and x['repetition']==i)for i in range(args.repeats)]
    report=dict(schema='cts-g-indication-primitive-benchmark-v1',marketSha256=hashlib.sha256(raw).hexdigest(),
        librarySha256=hashlib.sha256(Path(lib.__file__).read_bytes()).hexdigest(),packs=list(book.packs),windows=args.windows,
        repetitions=args.repeats,medianSpeedup=statistics.median(ratios),pairedSpeedups=ratios,allOutputsIdentical=True,
        signalRowsPerRun=args.windows*len(book.packs),allSignalRowsCompared=args.windows*len(book.packs)*len(results),
        sha256=expected,results=results,scope='raw ordinary-pack signal calculation only; unchanged settings and market windows; excludes replay scoring, network, execution and startup imports')
    args.out.write_text(json.dumps(report,indent=2,allow_nan=False))
    print(json.dumps({k:report[k]for k in ['medianSpeedup','allOutputsIdentical','allSignalRowsCompared']}),flush=True)


if __name__=='__main__':main()
