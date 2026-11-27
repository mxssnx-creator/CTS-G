"""Native reversion Sets with fixed signed gates, independent histories and causal inventory controls."""
from concurrent.futures import ProcessPoolExecutor
import hashlib

import numpy as np

from . import study
import direct_base_grid as direct


COMPONENTS = {
    'target20': ('sl0.75:st20', 'L10+', 'S8- T1+'),
    'trendTrail': ('tr1.20:0.50', 'S8+', 'S8- T1+'),
    'counterTrail': ('tr2.00:0.80', 'S16-', 'S12+'),
    'plainTrail': ('tr2.00:0.80', 'S8-', 'S12+'),
}
PORTFOLIOS = {k: [k] for k in COMPONENTS}
PORTFOLIOS['nativeBasket'] = ['target20', 'trendTrail', 'counterTrail']
POLICY = {
    'version': 2, 'profile': 'native', 'source': 'reversion', 'warmMinutes': 1440,
    'components': COMPONENTS, 'portfolios': PORTFOLIOS,
    'baseFloors': direct.BASE_FLOORS, 'endFloors': direct.END_FLOORS,
    'caps': [2], 'inventory': [2, 6, 12], 'guard': [False, True],
    'axis': [0, 1, 2, 3], 'block': [0, 1, 2, 3, 4],
}
# Include the price, indication, fee, Set and stage implementations that produced the admission and valuation.
def engine_digest():
    files = [study.ROOT/'server/pulse'/name for name in (
        'set_engine.py','stage_grid.py','direct_base_grid.py','indication_extra.py','execution_cost.py',
        'fee_policy.py','axis_sets.py','block_rounds.py','overlay-bingx-x02.json')]
    files += [study.HERE/name for name in ('study.py','metrics.mjs','walk_forward.py','walk_forward.mjs','native_policy.py')]
    h = hashlib.sha256()
    for path in files:
        h.update(path.name.encode()+b'\0'+path.read_bytes()+b'\0')
    return h.hexdigest()


def source_keys():
    return sorted({f'reversion:{side}:1m:{shape}' for shape, _, _ in COMPONENTS.values() for side in ('L','S')})


def native_orders(folder, manifest, info, jobs):
    keys = source_keys()
    tasks = [(str(folder), name, i, manifest) for i,name in enumerate(info['symbols'])]
    tapes = {k: [] for k in keys}
    with ProcessPoolExecutor(jobs, initializer=study.init_workers, initargs=(keys,)) as pool:
        for result in pool.map(study.raw_symbol, tasks):
            for key, rows in result.items():
                tapes[key].extend(rows)
    return tapes


def panel_from_tapes(folder, manifest, info, book, tapes):
    from .walk_forward import memberships
    bars = np.asarray([study.candle_rows(folder, s, manifest) for s in info['symbols']])
    prices = bars[:,:,3]
    logs = np.log(prices)
    ret = np.zeros_like(logs)
    ret[:,60:] = 100*(logs[:,60:]-logs[:,:-60])
    market = np.median(ret, axis=0)
    bases = {r.name:r for r in direct.base_rules(book.stage_grid)}
    ends = {r.name:r for r in direct.end_rules()}
    parts = {}
    for key in source_keys():
        template = book.sets[key]
        rows = sorted(tapes[key], key=lambda r:(r[1] or info['minutes']+1,r[2],r[0]))
        orders = []
        for en,ex,sym,net in rows:
            # A market mark includes the same full round-trip allowance as a settled native fill.
            mark = None if ex is not None else template.side*(prices[sym,-1]/prices[sym,en-1]-1)-book.cost_pct/100
            orders.append([en,ex,sym,net,True,float(-template.side*ret[sym,en-1]),mark])
        rules = {}
        for component,(shape,b,e) in COMPONENTS.items():
            if not key.endswith(':'+shape):
                continue
            grid=memberships(orders,template,book,info['minutes'],component,bases,ends,COMPONENTS)
            for case in grid.values():
                if component=='trendTrail':
                    case['end']=[i for i in case['end'] if template.side*market[orders[i][0]-1]>0]
                elif component=='counterTrail':
                    case['end']=[i for i in case['end'] if template.side*market[orders[i][0]-1]<-.5]
            rules[component]=grid
        parts[key]={'orders':orders,'rules':rules}
    return dict(period=info,sets=parts,costPct=book.cost_pct)
