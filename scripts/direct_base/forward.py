"""Replay frozen experimental choices on a later, unopened market period."""
import argparse
from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path
import subprocess
import tempfile

import study


def run():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data', type=Path, required=True)
    p.add_argument('--selection', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--jobs', type=int, default=3)
    p.add_argument('--warm-hours', type=int, default=24)
    a = p.parse_args()
    study.offline()
    import direct_base_grid as direct
    choices = json.loads(a.selection.read_text())
    keys = sorted({c['set'] for c in choices.values()})
    book, _ = study.make_book()
    if not 1 <= a.jobs <= 8 or not 1 <= a.warm_hours < 72 or any(k not in book.sets for k in keys):
        p.error('Invalid worker count, warm-up or Set selection')
    with tempfile.TemporaryDirectory(prefix='cts-forward-') as tmp, ProcessPoolExecutor(
            a.jobs, initializer=study.init_workers, initargs=(keys,)) as pool:
        panel = study.period(a.data, keys, a.warm_hours, pool, tmp)
        metadata = {'period': panel, 'selections':choices, 'sets': keys, 'costPct':book.cost_pct,
                    'base': [r.name for r in direct.base_rules(book.stage_grid)], 'end':[r.name for r in direct.end_rules()],
                    'baseFloors':direct.BASE_FLOORS, 'endFloors':direct.END_FLOORS}
        path = Path(tmp)/'data.json'
        path.write_text(json.dumps(metadata))
        a.out.parent.mkdir(parents=True,exist_ok=True)
        subprocess.run(['node',str(study.HERE/'forward.mjs'),str(path),str(a.out)],check=True)

if __name__ == '__main__':
    run()
