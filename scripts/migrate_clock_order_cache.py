#!/usr/bin/env python3
"""Revalidate trusted shadow tapes after the isolated Main short-window tie fix.

This does not relax replay_cache.load. The old cache must exactly match its
original source tree and bytes. Every dependency except the one scoring-only
AST edit must match the target. No signal, exit, pause, grid or cost change is
accepted. The target's native gate/rank parity must pass before a new cache is
written. The original cache is retained unchanged with linked provenance.
"""
import argparse
import ast
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def prove_edit(old_source, new_source):
    old = ast.parse(old_source)
    new = ast.parse(new_source)
    cls = next(n for n in old.body if isinstance(n, ast.ClassDef) and n.name == 'SetBook')
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_streams')
    assignment = next(n for n in ast.walk(fn) if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == 'records' for t in n.targets))
    expected = ast.parse('records = sorted((t, e, value) for t, e, value in zip(*evidence) if t <= upper)').body[0]
    if ast.dump(assignment, include_attributes=False) != ast.dump(expected, include_attributes=False):
        raise ValueError('source does not have the exact known scoring-only expression')
    assignment.value.keywords.append(ast.keyword(arg='key', value=ast.parse('lambda row: row[0]', mode='eval').body))
    if ast.dump(old, include_attributes=False) != ast.dump(new, include_attributes=False):
        raise ValueError('changes outside the isolated tied-close scoring correction')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--source-root', required=True)
    p.add_argument('--target-root', default=str(ROOT))
    p.add_argument('--data', required=True)
    p.add_argument('--patch-file', required=True)
    p.add_argument('--cache', required=True)
    p.add_argument('--out', required=True)
    args = p.parse_args()
    source, target = Path(args.source_root).resolve(), Path(args.target_root).resolve()
    if Path(args.cache).resolve() == Path(args.out).resolve():
        raise ValueError('the original cache must be retained')
    prove_edit((source/'server/pulse/set_engine.py').read_text(), (target/'server/pulse/set_engine.py').read_text())
    os.environ['STAGE_OVERLAY_PATCH'] = Path(args.patch_file).read_text()
    sys.path[:0] = [str(target/'scripts'), str(target/'server/pulse')]
    import stage_eval as sg
    spec = importlib.util.spec_from_file_location('migration_cache_tools', ROOT/'scripts/replay_cache.py')
    cache_tools = importlib.util.module_from_spec(spec); spec.loader.exec_module(cache_tools)
    names, bars, times = sg.load_bars(args.data, 50)
    old = json.loads(Path(args.cache+'.meta.json').read_text())['fingerprint']
    source_fp = cache_tools.fingerprint(args.data, names, sg.replay_overlay(), source/'server/pulse', source/'scripts/stage_eval.py')
    if 'sourceAssets' not in old and not source_fp['sourceAssets']:
        source_fp.pop('sourceAssets')
    if old != source_fp:
        raise ValueError('cache differs from the original sources, markets or settings')
    fresh = cache_tools.fingerprint(args.data, names, sg.replay_overlay(), target/'server/pulse', target/'scripts/stage_eval.py')
    expected = copy.deepcopy(source_fp)
    expected['engine']['set_engine.py'] = fresh['engine']['set_engine.py']
    if 'sourceAssets' not in expected and not fresh['sourceAssets']:
        expected['sourceAssets'] = {}
    if expected != fresh:
        raise ValueError('a non-scoring dependency changed; regeneration required')
    shadow = cache_tools.load(args.cache, old)
    book = sg.make_book(); tapes = sg.Tapes(book, names, times, shadow)
    start = max(times[s][0] for s in names) + book.lookback*60
    end = min(times[s][-1] for s in names)
    hours = [float(h) for h in range(((int(start)+3599)//3600)*3600,int(end),3600)]
    checks, bad = sg.self_check(book,tapes,hours,samples=60)
    if bad:
        raise ValueError(f'target parity still fails: {checks-bad}/{checks}')
    # Legacy baseline readers predate sourceAssets. Keep their exact schema;
    # the full source and target provenance remains in the migration proof.
    native_fp = __import__('replay_cache').fingerprint(args.data,names,sg.replay_overlay(),sg.PULSE,sg.__file__)
    cache_tools.save(args.out, shadow, native_fp)
    meta_path = Path(args.out+'.meta.json'); meta=json.loads(meta_path.read_text())
    meta['scoringMigration'] = dict(sourceFingerprint=old,targetFingerprint=fresh,
                                  proof='Only SetBook._streams tied-close sort key changed; full AST comparison',
                                  nativeParity=dict(checks=checks,mismatches=bad))
    meta_path.write_text(json.dumps(meta,sort_keys=True))
    print(json.dumps(dict(cache=args.out,checks=checks,mismatches=bad,proof='exact scoring-only AST edit')),flush=True)


if __name__ == '__main__':
    main()
