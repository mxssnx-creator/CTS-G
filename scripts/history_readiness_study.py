#!/usr/bin/env python3
"""Paired causal 24h/48h prehistory study; native lanes, no venue access.

Readiness is full requested N on each exact set's own closed stream, independent
of PF. Both all-set and predecessor-qualified denominators are retained. No
economic default is selected by this research program.
"""
from __future__ import annotations

import argparse
import bisect
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
from concurrent.futures import ProcessPoolExecutor

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / 'server/pulse')]
import stage_eval as sg
import replay_cache
from coord_engine import Coordinator
from micro_short_study import metric
from micro_feasibility_study import no_network, drawdown

TRIPLES = [(1, 1, 1), (3, 1, 1), (5, 3, 1), (5, 3, 3),
           (10, 5, 3), (10, 5, 5), (20, 10, 8), (30, 15, 12),
           (50, 25, 15), (80, 40, 20)]
CONTEXT = None


def source_hashes():
    files = [HERE / 'stage_eval.py', Path(__file__)] + sorted((HERE.parent / 'server/pulse').glob('*.py'))
    return {str(p.relative_to(HERE.parent)): replay_cache.file_sha(p) for p in files}


def coverage(population, full):
    return {'population': population, 'full': full, 'insufficient': population-full,
            'pct': 100*full/population if population else None}


def snapshot(book, tapes, at):
    names = ('base', 'main', 'real', 'realNeg')
    totals = {name: {'all': [0, 0], 'eligible': [0, 0]} for name in names}
    passed = {name: 0 for name in names}
    gp = gl = 0.
    for st in book.by_idx:
        e = sg.base_eval(book, tapes, st, at)
        base_ok = sg.gate_base(book, tapes, st, at)[0]
        ch, nets = sg.chain_at(book, tapes, st, at)
        main = [nets[k] for k in ch['main']]
        real = [nets[k] for k in ch['real']]
        negative = [nets[k] for k in ch['realneg']]
        assert all(math.isfinite(x) for x in main+real+negative), 'Unknown stage return'
        v = sg.se.stage_verdicts(main, real, negative, ch['short_n'], book.stage_params(st.pack, st))
        wn = book.win(st)
        counts = {'base': e['n_all'], 'main': len(main), 'real': len(real), 'realNeg': len(negative)}
        needs = {'base': wn.base_long, 'main': wn.main_n, 'real': wn.real_n, 'realNeg': wn.real_neg_n}
        eligible = {'base': True, 'main': base_ok, 'real': base_ok and v['main'],
                    'realNeg': book.neg_on and base_ok and v['neg']}
        for name in names:
            if name == 'realNeg' and not book.neg_on:
                continue
            enough = counts[name] >= needs[name]
            totals[name]['all'][0] += 1
            totals[name]['all'][1] += enough
            if eligible[name]:
                totals[name]['eligible'][0] += 1
                totals[name]['eligible'][1] += enough
        passed['base'] += bool(base_ok)
        passed['main'] += bool(base_ok and v['main'])
        passed['real'] += bool(base_ok and v['pos'])
        passed['realNeg'] += bool(base_ok and book.neg_on and v['negl'])
        if base_ok:
            gp += e['gp']
            gl += e['gl']
    return {'t': at, 'sets': len(book.by_idx), 'sampleCoverage': {
        name: {key: coverage(*values) for key, values in populations.items()}
        for name, populations in totals.items()}, 'passed': passed,
        'validatedPct': 100*passed['base']/len(book.by_idx),
        'selectedBasePf': gp/gl if gl else None, 'selectedBaseNoLoss': bool(gp and not gl)}


def readiness_parity(book, tapes, hours):
    """Readiness counters independently checked against native stored-state scoring."""
    checks = 0
    picks = [(book.by_idx[i*len(book.by_idx)//8], hours[i*len(hours)//8]) for i in range(8)]
    for st, at in picks:
        tx, te, pp = tapes.window(st.idx, at, book.history_bars()*60, 10**9)
        st.hist = [{'t': float(t), 'hold_s': float(t-entry), 'pnl_pct': float(gross),
                    'pnl': float(gross)-tapes.cf, 'reason': 'research'}
                   for t, entry, gross in zip(tx, te, pp)]
        st.live = []
        book._score_one(st, now=at)
        chain, _ = sg.chain_at(book, tapes, st, at)
        counts = {'base': sg.base_eval(book, tapes, st, at)['n_all'],
                  'main': len(chain['main']), 'real': len(chain['real']), 'realNeg': len(chain['realneg'])}
        assert st.sample_counts == counts, ('native sample count divergence', st.id, at, st.sample_counts, counts)
        checks += 4
        st.hist = []
    return checks


def trial(spec):
    range_name, prehours, names, bars, times, shadow, hours, output = CONTEXT
    bn, mn, rn, mode = spec
    started = time.perf_counter()
    book = sg.make_book()
    for pack in book.packs:
        policy = book.pack_ranges[pack]['lanePolicy']
        policy['lastN'] = {'baseLong': bn, 'baseShort': 0, 'mainLong': mn,
                          'mainShort': [1], 'realN': rn, 'realNegN': rn, 'negWindows': [1, 3]}
        policy['negativeFilter'] = {'windows': [1], 'mode': 'any', 'maxPf': 1} if mode == 'veto-1' else None
    book.gate_min = 1
    book.short_on = False
    book.neg_on = False
    tapes = sg.Tapes(book, names, times, shadow)
    checks, bad = sg.self_check(book, tapes, hours, samples=8)
    assert bad == 0, ('native/replay divergence', spec, bad)
    sample_checks = readiness_parity(book, tapes, hours)
    co = Coordinator()
    co.load({}, sg.replay_overlay())
    raw = sg.execute(book, names, bars, times, shadow, tapes, hours,
                     refresh_s=0, include_open=True, axis_coord=co)
    settled = sorted([r for r in raw if 'net' in r], key=lambda r: (r['t'], r['symbol']))
    held = [r for r in raw if r.get('open_at_end')]
    trades = [{k: r[k] for k in ('t', 't_in', 'symbol', 'set', 'pack', 'kind', 'lane', 'reason', 'net')} |
              {'side': r['tr']['dir'], 'entry': r['tr']['entry_px'], 'exit': r['tr']['exit_px'],
               'step': book.sets[r['set']].step, 'slRatio': book.sets[r['set']].sl_ratio}
              for r in settled]
    for r in trades:
        direct = (r['exit']/r['entry']-1)*r['side']-tapes.cf
        assert abs(direct-r['net']) < 1e-10, ('cost/price mismatch', r)
    stages = [snapshot(book, tapes, at) for at in hours]
    for row in stages:
        row['settled'] = metric([r['net'] for r in trades if row['t'] < r['t'] <= row['t']+3600])
    midpoint = hours[0]+len(hours)*1800
    halves = [metric([r['net'] for r in trades if (r['t'] <= midpoint) == first]) for first in (True, False)]
    result = {'id': f'{range_name}-pre{prehours}-b{bn}-m{mn}-r{rn}-{mode}',
              'range': range_name, 'prehistoryHours': prehours, 'baseN': bn, 'mainN': mn, 'realN': rn,
              'mode': mode, 'sets': len(book.by_idx), 'closed': metric([r['net'] for r in trades]),
              'halves': halves, 'held': len(held), 'marked': metric([r['net'] for r in trades]+[r['mark_net'] for r in held]),
              'unitDrawdownPct': drawdown([r['net'] for r in trades]), 'stages': stages, 'trades': trades,
              'parity': {'checks': checks, 'sampleCountChecks': sample_checks, 'mismatches': bad}, 'seconds': time.perf_counter()-started}
    result['passesScreen'] = all(x['n'] >= 10 and (x['noLoss'] or (x['pf'] is not None and x['pf'] > 1)) for x in halves)
    Path(output, result['id']+'.json').write_text(json.dumps(result, indent=2, allow_nan=False))
    return {k: v for k, v in result.items() if k not in ('trades', 'stages')}


def plans(names):
    base = sg.replay_overlay()
    for switch, _key, _default in sg.se.EXTRA_PACKS.values():
        base[switch] = False
    base.update(stratGeneral=False, stratIndications=False, stratShortRange=False, extraIndicators=[],
                directionMode='split', holdAuto=False, stepHoldOn=[True]*12, stepHoldMin=[15]*12,
                minPfStepAuto=False, histLookbackAuto=False, mainNegEnabled=False, stagesEnabled=True,
                mainGateEnabled=True, realGateEnabled=True, mainShortEnabled=False,
                stageFullWindow=True, setGateMinTrades=1, setGateMinN=0, mainMinN=0,
                groupGateEnabled=False, blockEnabled=False, dcaEnabled=False, relateEnabled=False,
                makerFeeRate=.0002, takerFeeRate=.0005, slippageFeeRatio=.1, minSlDistancePct=.1)
    base.pop('positionCostPct', None)
    result = []
    for name, lo, hi in [('micro', 2, 3), ('minimal', 4, 6), ('short', 7, 9)]:
        ov = copy.deepcopy(base)
        ov.update(setMinStep=lo, setStepMax=hi, tpRanges={k: k == name for k in ('micro', 'minimal', 'short', 'normal', 'long', 'wide')})
        ov['indicationConfigs'] = [{'label': name+' history readiness',
            'names': ['momchg_n5_t1', 'mpull_n5_t1', 'wickrev_n5_t1', 'sweep_n5_t1'],
            'relations': [''], 'stepMin': lo, 'stepMax': hi, 'slRatios': [.25, .5], 'types': ['base'],
            'symbols': names, 'major': name, 'minor': 'history',
            'lastNLanes': [{'id': 'independent', 'role': 'independent', 'scope': 'config',
                           'lastN': {'baseLong': 10, 'baseShort': 0, 'mainLong': 5, 'mainShort': [1], 'realN': 3, 'realNegN': 3}}]}]
        result.append({'range': name, 'overlay': ov})
    return result


def main():
    global CONTEXT
    ap = argparse.ArgumentParser()
    ap.add_argument('--market', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--jobs', type=int, default=3)
    ap.add_argument('--trade-hours', type=int, default=24)
    ap.add_argument('--ranges', default='micro,minimal,short')
    ap.add_argument('--quick', action='store_true', help='Only first positive triple, for harness smoke checks')
    args = ap.parse_args()
    assert 1 <= args.jobs <= 3 and 2 <= args.trade_hours <= 24
    sys.addaudithook(no_network)
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    names, allbars, alltimes = sg.load_bars(args.market, 12)
    assert len(names) == 12
    count = (48+args.trade_hours)*60
    assert all(len(alltimes[n]) >= count for n in names), '48h prehistory plus requested trade hours required'
    for n in names:
        allbars[n] = allbars[n][-count:]
        alltimes[n] = alltimes[n][-count:]
    assert all(alltimes[n] == alltimes[names[0]] for n in names), 'Symbols must align'
    assert all(b-a == 60 for a, b in zip(alltimes[names[0]], alltimes[names[0]][1:])), 'Missing/duplicate candle'
    start = alltimes[names[0]][0]
    trade_start = start+48*3600
    hours = list(range(trade_start, trade_start+args.trade_hours*3600, 3600))
    variants = [(bn, mn, rn, 'positive') for bn, mn, rn in TRIPLES]
    variants += [(5, 3, 1, 'veto-1'), (10, 5, 3, 'veto-1')]
    if args.quick:
        variants = variants[:1]
    baseline = source_hashes()
    selected_plans = [p for p in plans(names) if p['range'] in args.ranges.split(',')]
    protocol = {'symbols': names, 'source': baseline, 'marketSha256': {n: replay_cache.file_sha(Path(args.market, n+'.json')) for n in names},
                'dataStart': start, 'trade': [hours[0], hours[-1]+3600], 'prehistoryHours': [24, 48],
                'priorDataOverlap': True,
                'tradeHours': args.trade_hours, 'variants': variants, 'plans': selected_plans,
                'fees': 'Frozen quote maker0.02%, taker0.05%, 10% fee-relative slippage; not account authenticated; funding excluded',
                'readiness': 'Each exact config/symbol/direction/type/step/SL own completed stream; full requested N independent of PF',
                'population': 'All exact configured sets and predecessor-qualified current stage populations shown separately',
                'limitations': 'Retrospective, bounded truncated upstream context; not independent out-of-sample or venue execution. One physical position per symbol. Normal TP/SL only; automatic PF off to isolate history.',
                'promotion': 'None. Screen requires PF>1 and at least10 settlements in both chronological halves.'}
    if (output/'protocol.json').exists():
        assert json.loads((output/'protocol.json').read_text()) == json.loads(json.dumps(protocol)), 'Resume requires identical source, data and policy'
    (output/'protocol.json').write_text(json.dumps(protocol, indent=2, allow_nan=False))
    summary = []
    for plan in selected_plans:
        for prehours in (24, 48):
            pending = []
            for spec in variants:
                bn, mn, rn, mode = spec
                path = output / f"{plan['range']}-pre{prehours}-b{bn}-m{mn}-r{rn}-{mode}.json"
                if path.exists():
                    result = json.loads(path.read_text())
                    assert result['parity']['mismatches'] == 0 and result['id'] == path.stem
                    summary.append({k: v for k, v in result.items() if k not in ('trades', 'stages')})
                else:
                    pending.append(spec)
            if not pending:
                continue
            ov = copy.deepcopy(plan['overlay'])
            ov.update(histLookbackBars=prehours*60, histLookbackMax=prehours*60)
            os.environ['STAGE_OVERLAY_PATCH'] = json.dumps(ov)
            book = sg.make_book()
            assert not book.config_report['dropped'], book.config_report
            cut = (48-prehours)*60
            bars = {n: rows[cut:] for n, rows in allbars.items()}
            times = {n: rows[cut:] for n, rows in alltimes.items()}
            print('BUILD', plan['range'], prehours, 'hours', len(book.by_idx), 'sets', flush=True)
            fingerprint = replay_cache.fingerprint(args.market, names, sg.replay_overlay(), sg.PULSE, sg.__file__)
            cache = str(output / f"{plan['range']}-pre{prehours}.pkl")
            shadow = replay_cache.load(cache, fingerprint) if Path(cache).exists() else None
            if shadow is None:
                shadow = sg.build(names, bars, times, args.jobs)
                replay_cache.save(cache, shadow, fingerprint)
            CONTEXT = (plan['range'], prehours, names, bars, times, shadow, hours, str(output))
            with ProcessPoolExecutor(max_workers=args.jobs) as pool:
                for result in pool.map(trial, pending):
                    summary.append(result)
                    (output/'summary.json').write_text(json.dumps(summary, indent=2, allow_nan=False))
                    print(json.dumps(result), flush=True)
    assert source_hashes() == baseline, 'Engine changed during study; results must be regenerated'
    (output/'summary.json').write_text(json.dumps(summary, indent=2, allow_nan=False))
    final = {'cases': len(summary), 'passesScreen': sum(r['passesScreen'] for r in summary),
             'parityChecks': sum(r['parity']['checks'] for r in summary), 'parityMismatches': sum(r['parity']['mismatches'] for r in summary),
             'sampleCountChecks': sum(r['parity']['sampleCountChecks'] for r in summary),
             'sourceUnchanged': True}
    (output/'completion.json').write_text(json.dumps(final, indent=2))
    print('COMPLETE', json.dumps(final), flush=True)


if __name__ == '__main__':
    main()
