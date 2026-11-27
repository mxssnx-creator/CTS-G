"""Build independent direct-Base paper executions from archived exchange candles."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'server/pulse')]


def offline():
    def deny(event, _):
        if event in ('socket.connect', 'socket.getaddrinfo'):
            raise RuntimeError("Network access during archived candle replay")
    sys.addaudithook(deny)
    # Imported engine modules construct an idle HTTP client even on an offline run.
    for name in ('all_proxy', 'https_proxy', 'http_proxy'):
        os.environ.pop(name, None)
        os.environ.pop(name.upper(), None)


def dump(path, value):
    with gzip.GzipFile(filename=str(path), mode='wb', mtime=0) as out:
        out.write(json.dumps(value, separators=(',', ':'), allow_nan=False).encode())


SET_TEMPLATES = (
    "shortrange:{side}:1m:sl0.75:st4",
    "trend:{side}:1m:sl0.75:st10",
    "reversion:{side}:1m:sl0.75:st6",
    "breakout:{side}:1m:tr1.20:0.50",
    "sig-ema-cross-short-t1:{side}:1m:sl0.75:st6",
)
DEFAULT_SETS = [s.format(side=side) for s in SET_TEMPLATES for side in ('L', 'S')]


def make_book():
    import stage_eval as sim
    from execution_cost import FeeSchedule
    from fee_policy import cost_policy
    from set_engine import SetBook
    overlay = json.loads((ROOT / 'server/pulse/overlay-bingx-x02.json').read_text())
    # Keep native stop caps, drawdown, cooldown, prices and protection. Freeze
    # PF and rest settings so a different optimizer cannot change this trial.
    overlay.update(stageLastN={'enabled': True}, minPfStepAuto=False, pauseMode='off',
                   pausePack={}, holdAuto=False)
    overlay['positionCostPct'] = FeeSchedule().paper_budget(cost_policy(overlay), {}, 0)
    book = SetBook()
    book.load(overlay)
    return book, sim.lane_cfg(book)


def init_workers(set_ids):
    global BOOK, LANE_CFG, SETS
    offline()
    BOOK, LANE_CFG = make_book()
    SETS = [BOOK.sets[k] for k in set_ids]


def candle_rows(path, symbol, manifest):
    raw = (Path(path) / (symbol+'.json')).read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest['sha256'][symbol]:
        raise ValueError(f"{symbol} candle hash mismatch")
    data = json.loads(raw)
    clocks = range(manifest['start_ms'], manifest['end_ms'], 60000)
    if len(data) != len(clocks):
        raise ValueError(f"{symbol} incomplete candles")
    bars = []
    for candle, ts in zip(data, clocks):
        t, o, h, low, c, vol = candle
        if (t != ts or not all(map(math.isfinite, candle)) or not 0 < low <= min(o, c) <= max(o, c) <= h or vol < 0):
            raise ValueError(f"{symbol} invalid OHLCV at {ts}")
        bars.append([o, h, low, c, vol])
    return bars


def raw_symbol(task):
    import set_engine as se
    path, symbol, index, manifest = task
    bars = candle_rows(path, symbol, manifest)
    packs = sorted({st.pack for st in SETS})
    warm = BOOK.warmup
    signals = se.pack_signals(bars, packs, dict(BOOK.ind_settings, portedSymbol=symbol), manifest['start_ms']/1000 + 60, warm)
    directions = {p: [s[0] for s in signals[p]] for p in packs}
    confidence = {p: [s[1] for s in signals[p]] for p in packs}
    candidates = {p: [i for i in range(warm, len(bars)) if directions[p][i] and confidence[p][i] >= LANE_CFG['entry_conf']] for p in packs}
    by_side = BOOK._cand_by_side(candidates, directions)
    acc = se.BarIndex(bars)
    result = {}
    for st in SETS:
        if BOOK.stop_cap_exceeded(st) or not BOOK.symbol_allows(st.pack, symbol):
            result[st.id] = []
            continue
        lane = se.SetLane(st, symbol)
        output = lane.scan(0, len(bars), bars, directions[st.pack], confidence[st.pack], True,
                           LANE_CFG, by_side[st.pack, st.side], acc)
        # Decisions and settlements use bar ends. Already-net native P&L is immutable.
        rows = [[r['entry_i']+1, r['exit_i']+1, index, r['pnl']] for r in output]
        if lane.open:
            rows.append([lane.open['i']+1, None, index, None])
        result[st.id] = rows
    return result


def score_set(task):
    import direct_base_grid as db
    key, rows, length, cache = task
    rows.sort(key=lambda r: (r[1] if r[1] is not None else length+1, r[2], r[0]))
    # An unresolved order can be admitted, but its sentinel close is beyond
    # every entry and its mark can never enter a gate or realized P&L.
    native = BOOK.grid_history(BOOK.sets[key],
        closes=[60*(r[1] if r[1] is not None else length+1) for r in rows],
        entries=[60*r[0] for r in rows], nets=[r[3] if r[3] is not None else 0.0 for r in rows])
    streams, base, ends = db.memberships(native)
    data = {'name': key, 'orders': rows, 'base': base, 'ends': ends, 'streams': streams}
    target = Path(cache) / (hashlib.sha256(key.encode()).hexdigest()[:16]+'.json.gz')
    dump(target, data)
    print(f"{key}: {len(rows)} native orders, {len(ends)} Base memberships, {len(streams)} distinct streams", flush=True)
    return str(target)


def period(path, keys, warm_hours, pool, cache):
    manifest = json.loads((path / '_meta.json').read_text())
    length, remainder = divmod(manifest['end_ms']-manifest['start_ms'], 60000)
    if remainder or not (warm_hours*60 < length <= 4320) or length % 60 or manifest['start_ms'] % 3600000:
        raise ValueError('Use complete UTC hours, including warm-up, up to 72 hours per period')
    names = sorted(manifest['symbols'])
    if not names or len(names) != len(set(names)):
        raise ValueError('Expected distinct symbols')
    tapes = {k: [] for k in keys}
    tasks = [(str(path), s, i, manifest) for i, s in enumerate(names)]
    for number, result in enumerate(pool.map(raw_symbol, tasks), 1):
        for k in keys:
            tapes[k].extend(result[k])
        if number % 5 == 0:
            print(f"{path.name}: replayed {number}/{len(names)} symbols", flush=True)
    print(f"{path.name}: built {sum(map(len, tapes.values()))} native candidates", flush=True)
    paths = list(pool.map(score_set, [(k, tapes.pop(k), length, cache) for k in keys]))
    return {'manifest': manifest, 'symbols': names, 'minutes': length, 'warm': warm_hours*60, 'parts': paths}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--development', type=Path, required=True)
    parser.add_argument('--holdout', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True, help='Standalone HTML report')
    parser.add_argument('--confirmation', type=Path, help='Optional later confirmation output for the same frozen choices')
    parser.add_argument('--warm-hours', type=int, default=24)
    parser.add_argument('--jobs', type=int, default=3)
    parser.add_argument('--set', dest='sets', action='append', help='Native directional Set ID; repeat for independent Sets')
    args = parser.parse_args(argv)
    if not 1 <= args.jobs <= 8 or not 1 <= args.warm_hours < 72:
        parser.error('jobs must be 1–8 and warm-up 1–71 hours')
    offline()
    import direct_base_grid as db
    book, _ = make_book()
    keys = args.sets or DEFAULT_SETS
    if len(keys) != len(set(keys)) or any(k not in book.sets or not book.sets[k].side for k in keys):
        parser.error('Choose unique directional Sets from the x02 overlay')
    from set_engine import _ported_pack_sources
    if _ported_pack_sources([book.sets[k].pack for k in keys]):
        parser.error('The study accepts native single-symbol indication packs')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cts-direct-') as tmp, ProcessPoolExecutor(
            args.jobs, initializer=init_workers, initargs=(keys,)) as pool:
        panels = []
        for i, path in enumerate((args.development, args.holdout)):
            cache = Path(tmp)/str(i)
            cache.mkdir()
            panels.append(period(path, keys, args.warm_hours, pool, cache))
        first, second = panels
        if first['symbols'] != second['symbols'] or first['manifest']['end_ms'] > second['manifest']['start_ms']:
            raise ValueError('Development and holdout require the same universe and ordered, disjoint periods')
        meta = {'base': [r.name for r in db.base_rules(book.stage_grid)], 'end': [r.name for r in db.end_rules()],
                'baseFloors': db.BASE_FLOORS, 'endFloors': db.END_FLOORS, 'sets': keys,
                'costPct': book.cost_pct, 'lookbackHours': book.evidence_bars()/60, 'periods': panels}
        source = Path(tmp) / 'study.json'
        source.write_text(json.dumps(meta))
        command = ['node', str(HERE/'report.mjs'), str(source), str(args.out.resolve())]
        if args.confirmation:
            command.extend(['--confirmation',str(args.confirmation)])
        subprocess.run(command, check=True)
    return 0
