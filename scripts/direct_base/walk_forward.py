"""Fit a post-Base paper policy on chronological development panels, or replay its frozen settings."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

import numpy as np

from . import study
import direct_base_grid as direct

# Each component is a distinct exact Set and signed gate. Two sides always have separate histories.
COMPONENTS = {
    'fixed12': ('sl0.75:st12', 'T3-', 'S8-'),
    'fixed20': ('sl0.75:st20', 'T3+', 'S8-'),
    'trail': ('tr1.20:0.50', 'S8+', 'T3-'),
    'trailLast': ('tr1.20:0.50', 'T3-', 'S8-'),
    'compound': ('tr1.20:0.50', 'L10+ S4+ T1+', 'S8- T1+'),
}
PORTFOLIOS = {k: [k] for k in COMPONENTS}
# Disjoint price protections give the three component books separate order identities.
PORTFOLIOS['balanced'] = ['fixed12', 'fixed20', 'trail']
POLICY = {
    'version': 1, 'signalMinutes': 10, 'medianMovePct': .15, 'minLagShare': .7,
    'maxOwnMoveShare': .6, 'trendMinutes': 720, 'warmMinutes': 1440,
    'components': COMPONENTS, 'portfolios': PORTFOLIOS,
    'baseFloors': direct.BASE_FLOORS, 'endFloors': direct.END_FLOORS,
    'caps': [0, 2], 'axis': [0, 1, 2, 3], 'block': [0, 1, 2, 3, 4],
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def directions(bars, policy=POLICY):
    """Use only closed, synchronous bars. All returns and the median use the same two endpoints."""
    close, volume = bars[:, :, 3], bars[:, :, 4]
    log = np.log(close)

    def returns(n):
        result = np.zeros_like(close)
        result[:, n:] = 100 * (log[:, n:] - log[:, :-n])
        return result

    move = returns(policy['signalMinutes'])
    market = np.median(move, axis=0)
    sign = np.sign(market)
    lag = (market - move) * sign
    active = ((abs(market) >= policy['medianMovePct']) & (lag > policy['minLagShare'] * abs(market))
              & (abs(move) < policy['maxOwnMoveShare'] * abs(market)) & (volume > 0))
    active[:, :policy['signalMinutes']] = False
    trend = np.sign(np.median(returns(policy['trendMinutes']), axis=0))
    return np.where(active, sign, 0).astype(np.int8), trend, lag


def memberships(rows, template, book, length, component, bases, ends, components=COMPONENTS):
    _, base_name, end_name = components[component]
    history = book.grid_history(template, closes=[60*(r[1] or length+1) for r in rows],
                                entries=[60*r[0] for r in rows], nets=[r[3] or 0 for r in rows])
    grid = {}
    for bf in direct.BASE_FLOORS:
        base_history = direct.fixed_history(history, bf)
        root = base_history.root
        parents = direct.members(bases[base_name].mask(root), root.ids)
        for ef in direct.END_FLOORS:
            child = direct.fixed_history(history, ef, parents)
            ids = direct.members(ends[end_name].mask(child.root), parents)
            grid[f'{bf:g}/{ef:g}'] = {'parentCount': len(parents), 'end': ids}
    return grid


def manifest_info(folder):
    manifest = json.loads((folder/'_meta.json').read_text())
    start, end = manifest['start_ms'], manifest['end_ms']
    if (start % 3600000 or end % 3600000 or not POLICY['warmMinutes']*60000 < end-start <= 72*3600000
            or start >= end):
        raise ValueError('Expected a 24-hour warm-up and complete UTC scoring hours within 72 hours.')
    symbols = sorted(manifest['symbols'])
    if len(symbols) < 3 or len(set(symbols)) != len(symbols) or set(symbols) != set(manifest['sha256']):
        raise ValueError('Expected a fixed, distinct multi-symbol universe with candle hashes.')
    return manifest, dict(start=start, end=end, symbols=symbols, minutes=(end-start)//60000,
                          warm=POLICY['warmMinutes'], source=manifest.get('source'), hashes=manifest['sha256'])


def build_panel(folder, manifest, info, book, cfg):
    import set_engine as engine

    bars = np.asarray([study.candle_rows(folder, sym, manifest) for sym in info['symbols']])
    signals, trend, lag = directions(bars)
    shapes = sorted({shape for shape, _, _ in COMPONENTS.values()})
    tapes, templates = {}, {}
    for shape in shapes:
        for side, label in ((1, 'L'), (-1, 'S')):
            template = book.sets[f'reversion:{label}:1m:{shape}']
            key = f'market-catchup:{label}:1m:{shape}'
            templates[key] = template
            tapes[key] = []

    for sym, name in enumerate(info['symbols']):
        prices = bars[sym].tolist()
        index = engine.BarIndex(prices)
        signal = signals[sym].tolist()
        candidates = {side: np.flatnonzero(signals[sym] == side).tolist() for side in (1, -1)}
        confidence = [1.0]*len(prices)
        for key, template in templates.items():
            if book.stop_cap_exceeded(template) or not book.symbol_allows(template.pack, name):
                continue
            # The new indication inherits the template's price protection and risk, with its own Set identity.
            lane = engine.SetLane(replace(template, id=key), name)
            fills = lane.scan(book.warmup, len(prices), prices, signal, confidence, True, cfg,
                              candidates[template.side], index)
            raw = [(f['entry_i'], f['exit_i']+1, f['pnl']) for f in fills]
            if lane.open:
                raw.append((lane.open['i'], None, None))
            for i, exit_minute, net in raw:
                tapes[key].append([i+1, exit_minute, sym, net, int(trend[i]) == template.side, float(lag[sym,i])])

    bases = {r.name: r for r in direct.base_rules(book.stage_grid)}
    ends = {r.name: r for r in direct.end_rules()}
    parts = {}
    for key, rows in tapes.items():
        rows.sort(key=lambda r: (r[1] or info['minutes']+1, r[2], r[0]))
        template = templates[key]
        parts[key] = {'orders': rows, 'rules': {
            component: memberships(rows, template, book, info['minutes'], component, bases, ends)
            for component, (shape, _, _) in COMPONENTS.items() if key.endswith(':'+shape)}}
    return dict(period=info, sets=parts, costPct=book.cost_pct)


def run_node(mode, body, out):
    with tempfile.TemporaryDirectory(prefix='cts-walk-') as temp:
        path = Path(temp)/'input.json'
        path.write_text(json.dumps(body, separators=(',', ':'), allow_nan=False))
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['node', str(study.HERE/'walk_forward.mjs'), mode, str(path), str(out)], check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(required=True, dest='method')
    fit = sub.add_parser('fit', help='Select using distinct, chronological development panels')
    fit.add_argument('--development', type=Path, action='append', required=True)
    fit.add_argument('--details', type=Path, help='Full development scores and settlement ledgers')
    fit.add_argument('--profile', choices=['market-catchup','native'], default='market-catchup')
    replay = sub.add_parser('replay', help='Apply a frozen fit to a later market panel')
    replay.add_argument('--data', type=Path, required=True)
    replay.add_argument('--selection', type=Path, required=True)
    for p in (fit, replay):
        p.add_argument('--jobs', type=int, choices=range(1,9), default=3)
        p.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    folders = args.development if args.method == 'fit' else [args.data]
    inputs = [manifest_info(f) for f in folders]
    infos = [i for _, i in inputs]
    if any(a['end'] > b['start'] or a['symbols'] != b['symbols'] for a,b in zip(infos,infos[1:])):
        parser.error('Development panels must be ordered, disjoint and use the same symbols.')
    from . import native_policy
    saved = json.loads(args.selection.read_text()) if args.method == 'replay' else None
    profile = args.profile if args.method == 'fit' else saved['policy'].get('profile', 'market-catchup')
    policy = native_policy.POLICY if profile == 'native' else POLICY
    protocol = digest(policy)
    if args.method == 'fit':
        if len(infos) < 3:
            parser.error('Fit needs at least three distinct market periods.')
        frozen = None
    else:
        frozen = saved
        frozen_id = frozen.pop('sha256', None)
        if frozen_id != digest(frozen):
            parser.error('Frozen selection fingerprint mismatch.')
        frozen['sha256'] = frozen_id
        if profile == 'native' and frozen.get('engine') != native_policy.engine_digest():
            parser.error('Production replay and fitted engine fingerprints differ.')
        if (frozen['protocol'] != protocol or frozen['symbols'] != infos[0]['symbols']
                or infos[0]['start'] + 60000*infos[0]['warm'] < frozen['trainingEnd']):
            parser.error('Frozen policy, symbol universe or forward chronology mismatch.')
    study.offline()
    book, cfg = study.make_book()
    panels = []
    for folder, (m, info) in zip(folders, inputs):
        if profile == 'native':
            tapes = native_policy.native_orders(folder,m,info,args.jobs)
            panels.append(native_policy.panel_from_tapes(folder,m,info,book,tapes))
        else:
            panels.append(build_panel(folder,m,info,book,cfg))
    body = dict(protocol=protocol, policy=policy, panels=panels, selection=frozen)
    if args.method == 'fit':
        with tempfile.TemporaryDirectory(prefix='cts-fit-') as temp:
            detail = Path(temp)/'fit.json'
            run_node('fit', body, detail)
            result = json.loads(detail.read_bytes())
        development = result.pop('development')
        if profile == 'native':
            result['engine'] = native_policy.engine_digest()
        result['sha256'] = digest(result)
        if args.details:
            args.details.parent.mkdir(parents=True, exist_ok=True)
            args.details.write_text(json.dumps(dict(selection=result['sha256'], panels=development), separators=(',', ':'))+'\n')
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2)+'\n')
    else:
        run_node('replay', body, args.out)
    return 0
