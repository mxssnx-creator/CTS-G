#!/usr/bin/env python3
"""Compare a prior engine against the current bounded replay, without exchange I/O."""
import argparse
import hashlib
import importlib.util
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server/pulse'))
import set_engine


def measure(module, symbol, bars, prepared, settings):
    book = module.SetBook()
    book.load(settings)
    book.ingest_bars(symbol, bars)
    hist, counts, strategy = {}, {}, {}
    ticks = 0
    def progress():
        nonlocal ticks
        ticks += 1
    started = time.perf_counter()
    book._replay_symbol(symbol, hist, 1_700_000_000., prepared=prepared,
                        hist_counts=counts, strat_hist=strategy, on_step=progress)
    elapsed = time.perf_counter() - started
    payload = {'hist':{k:[dict(r) for r in v] for k,v in sorted(hist.items())},
               'counts':counts, 'strategy':{k:[dict(r) for r in v] for k,v in strategy.items()}}
    digest = hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return dict(seconds=round(elapsed,6), sets=len(book.sets), closes=sum(counts.values()),
                retainedRows=sum(map(len,hist.values())), callbacks=ticks, resultSha256=digest)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline', required=True, type=pathlib.Path)
    p.add_argument('--output', required=True, type=pathlib.Path)
    args = p.parse_args()
    spec = importlib.util.spec_from_file_location('cts_replay_baseline', args.baseline)
    baseline = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = baseline
    spec.loader.exec_module(baseline)
    settings = dict(histEnabled=True, histLookbackBars=2880, histMinBars=60,
                    histWarmup=0, stratGeneral=True, stratIndications=False, stratTrailing=True,
                    stratBlock=True, histSimulateBlock=True, histSimulateDca=True,
                    setMinStep=1, setStepMax=4, slToTpRatios=[.2,.6])
    results = []
    for symbol in ('BCH-USDT','SOL-USDT','XRP-USDT','OVERFLOW-FIXTURE'):
        if symbol == 'OVERFLOW-FIXTURE':
            bars = [[100.,102.,98.,100.,1.] for _ in range(1000)]
            prepared = ({'general':[(1 if i%400<200 else -1,.9,'fixture') for i in range(len(bars))]}, {}, 0)
        else:
            path = ROOT / 'reports/7d-simulation-20260911/data' / (symbol+'.json')
            data = json.loads(path.read_text())
            bars = [row[1] for row in data['rows'][-2880:]]
            probe = set_engine.SetBook(); probe.load(settings); probe.ingest_bars(symbol,bars)
            prepared = probe.prepare_replay_signals(symbol, 1_700_000_000.)
        old = measure(baseline,symbol,bars,prepared,settings)
        new = measure(set_engine,symbol,bars,prepared,settings)
        row = dict(symbol=symbol,bars=len(bars),baseline=old,current=new,
                   equivalent=old['resultSha256']==new['resultSha256'],
                   speedup=round(old['seconds']/new['seconds'],3))
        results.append(row)
        print(json.dumps(row),flush=True)
        if not row['equivalent']:
            raise SystemExit('Replay evidence diverged: '+symbol)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(dict(
        kind='offline isolated replay benchmark; not live throughput',settings=settings,
        baselineSourceSha256=hashlib.sha256(args.baseline.read_bytes()).hexdigest(),
        currentSourceSha256=hashlib.sha256(pathlib.Path(set_engine.__file__).read_bytes()).hexdigest(),
        results=results),indent=2)+'\n')


if __name__ == '__main__':
    main()
