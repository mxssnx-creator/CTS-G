#!/usr/bin/env python3
"""Offline paired trailing-hold screen. No exchange access and no live settings changes.

For identical signal entries, evaluate timed and unlimited lanes until a common
horizon. Unclosed positions are marked to the horizon close, net of one assumed
round-trip cost. Entry cohorts must fit entirely within prehistory or forward.
This is a raw signal experiment, not a Base/Main/Real or portfolio simulation.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / '..' / 'server' / 'pulse'))
import set_engine as se
from position_cost import cost_as_frac

HOLDS = (30, -1)
HORIZON = 180
SAMPLE_EVERY = 15
WARMUP = 100
CANDIDATES = ((0.36, 0.36), (0.72, 0.36))


def outcome(st, symbol, bars, entry_index, direction, confidence, cfg, hold, horizon=HORIZON):
    """One isolated same-entry counterfactual; horizon close marks open exposure."""
    if entry_index + horizon >= len(bars):
        raise ValueError('horizon exceeds available completed bars')
    lane = se.SetLane(st, symbol)
    lane.hold = hold
    lane.step(entry_index, bars[entry_index], direction, confidence, True, cfg)
    if lane.open is None:
        raise ValueError('entry was not accepted')
    entry_price = float(lane.open['entry'])
    for i in range(entry_index + 1, entry_index + horizon + 1):
        closed = lane.step(i, bars[i], 0, 0, False, cfg)
        if closed is not None:
            return {'net': float(closed['pnl']), 'marked': False, 'reason': closed['reason'],
                    'hold_bars': i - entry_index}
    marked = (float(bars[entry_index + horizon][3]) - entry_price) / entry_price * direction
    return {'net': marked - cost_as_frac(cfg['cost_pct']), 'marked': True,
            'reason': 'open_marked_at_horizon', 'hold_bars': horizon}


def metrics(rows):
    gains = sum(r['net'] for r in rows if r['net'] > 0)
    losses = -sum(r['net'] for r in rows if r['net'] < 0)
    return {'n': len(rows), 'pf': round(gains / losses, 4) if losses else None,
            'no_losses': losses == 0 and gains > 0,
            'net_return_pct_sum': round(sum(r['net'] for r in rows) * 100, 4),
            'marked_open': sum(r['marked'] for r in rows),
            'closed': sum(not r['marked'] for r in rows)}


def worker(path):
    raw = json.loads(Path(path).read_text())
    raw.sort(key=lambda row: row[0])
    if len(raw) != 1440 or any(b[0] - a[0] != 60000 for a, b in zip(raw, raw[1:])):
        raise ValueError(f'{path}: expected 1,440 contiguous minute bars')
    bars = [[float(x) for x in r[1:6]] for r in raw]
    book = se.SetBook()
    book.load(json.loads((HERE / '..' / 'server' / 'pulse' / 'overlay-bingx-x01.json').read_text()))
    cfg = {'entry_conf': book.entry_conf, 'time_bars': book.hist_time_bars,
           'scratch_bars': book.scratch_bars, 'scratch_min': book.scratch_min,
           'cooldown': book.cooldown_bars, 'cost_pct': book.cost_pct,
           'honor_tp': book.hist_honor_tp, 'sl_floor_steps': book.sl_min_steps}
    start_time = raw[0][0] / 1000 + 60
    signals = se.pack_signals(bars, book.packs, book.ind_settings, start_time, book.warmup)
    selected = []
    for pack in book.packs:
        for arm, give in CANDIDATES:
            candidates = [x for x in book.by_idx if x.kind == 'trail' and x.pack == pack
                          and abs(x.trail_arm - arm) < 1e-9 and abs(x.trail_give - give) < 1e-9]
            if len(candidates) != 1:
                raise ValueError(f'expected one Set for {pack}/{arm}/{give}, got {len(candidates)}')
            selected.append(candidates[0])
    results = {}
    split = 720
    for st in selected:
        data = {'pre': {str(h): [] for h in HOLDS}, 'forward': {str(h): [] for h in HOLDS}}
        for label, start, end in (('pre', WARMUP, split), ('forward', split, len(bars))):
            for i in range(start, end - HORIZON, SAMPLE_EVERY):
                direction, confidence, _ = signals[st.pack][i]
                if not direction or confidence < cfg['entry_conf']:
                    continue
                for hold in HOLDS:
                    data[label][str(hold)].append(outcome(st, Path(path).stem, bars, i, direction, confidence, cfg, hold))
        results[st.id] = data
    return {'symbol': Path(path).stem, 'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            'first_ms': raw[0][0], 'last_ms': raw[-1][0], 'cost_pct': book.cost_pct, 'sets': results}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--jobs', type=int, default=4)
    args = p.parse_args()
    paths = sorted(Path(args.data).glob('*-USDT.json'))
    if not paths:
        raise ValueError('no symbol bars found')
    with ProcessPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        symbols = list(pool.map(worker, map(str, paths)))
    if len({(s['first_ms'], s['last_ms'], s['cost_pct']) for s in symbols}) != 1:
        raise ValueError('symbols must share time range and cost')
    rows = {}
    for sid in symbols[0]['sets']:
        cohorts = {}
        for phase in ('pre', 'forward'):
            cohorts[phase] = {str(h): metrics([r for s in symbols for r in s['sets'][sid][phase][str(h)]]) for h in HOLDS}
        ref, alt = cohorts['pre']['30'], cohorts['pre']['-1']
        # A finite ratio is needed for each arm. A no-loss sample is reported,
        # but too small to rank as a reliable estimate.
        select = (ref['n'] >= 10 and alt['n'] == ref['n'] and ref['pf'] is not None and
                  alt['pf'] is not None and alt['pf'] >= 1.2 and alt['pf'] >= ref['pf'] * 1.02)
        rows[sid] = {'pre': cohorts['pre'], 'forward': cohorts['forward'],
                     'research_selected': 'unlimited' if select else '30m'}
    output = {'method': {'type': 'offline matched-entry signal screen', 'horizon_minutes': HORIZON,
               'timed_minutes': 30, 'entry_sample_interval_minutes': SAMPLE_EVERY,
               'prehistory_bars': 720, 'forward_bars': 720, 'warmup_bars': WARMUP,
               'selection': 'prehistory matched n>=10, finite PFs, unlimited marked PF >= 1.2 and >= timed PF * 1.02',
               'scope': 'independent overlapping counterfactual entries, no Base/Main/Real gates or allocation; same-bar OHLC assumption; no exchange execution',
               'cost': 'configured round-trip cost applied once, including estimated close cost on marked-open positions'},
              'interval_ms': [symbols[0]['first_ms'], symbols[0]['last_ms']],
              'cost_pct': symbols[0]['cost_pct'], 'symbols': [{k: s[k] for k in ('symbol', 'sha256')} for s in symbols],
              'sets': rows}
    Path(args.out).write_text(json.dumps(output, indent=2) + '\n')
    print(json.dumps({'symbols': len(symbols), 'sets': len(rows), 'out': args.out}))


if __name__ == '__main__':
    main()
