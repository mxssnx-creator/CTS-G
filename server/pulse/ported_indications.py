"""Exact CTS-A-O Tactic / Inverse sources, evaluated in one bounded Node batch.

No venue calls, PnL inversion or gate exemptions. A completed-bar batch is shared by every
TP/SL strategy configuration. Missing market references or worker failures produce no entry.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
from collections import OrderedDict
from array import array
from concurrent.futures import ThreadPoolExecutor

_module_root=Path(__file__).resolve().parents[2]
ROOT=Path(os.environ.get('CTSG_PROJECT_ROOT') or _module_root)
if not (ROOT/'shared/cts-indications/catalog.json').is_file() and (Path(__file__).resolve().parent/'shared/cts-indications/catalog.json').is_file():ROOT=Path(__file__).resolve().parent
CATALOG=json.loads((ROOT/'shared/cts-indications/catalog.json').read_text()) if (ROOT/'shared/cts-indications/catalog.json').is_file() else []
SOURCES = {s['id']: s for s in CATALOG}
NAMES = {f"ao__{s['id']}__t{tf}": (s['id'], tf) for s in CATALOG for tf in ((1,5,15) if s['kind']=='inverse' else (1,5,15,30))}
_CACHE: OrderedDict[str, dict] = OrderedDict()
_LOCK = threading.RLock()
LAST_ERROR = '' if CATALOG else 'Tactic / Inverse calculation assets unavailable'
_PENDING = set()
_EXECUTOR = None
class SignalSeries:
    def __init__(self, values, name, unavailable=False):self.values=array('b',values);self.name=name;self.unavailable=unavailable
    def __len__(self):return len(self.values)
    def __getitem__(self,index):
        if isinstance(index,slice):return [self[i] for i in range(*index.indices(len(self)))]
        value=self.values[index]
        return (int(value),1.0,self.name) if value else (0,0.0,'source unavailable' if self.unavailable else '')
def status():
    with _LOCK:return dict(pending=len(_PENDING),cached=len(_CACHE),bytes=sum(len(v) for entry in _CACHE.values() for v in entry.values()),error=LAST_ERROR)


def known(name: str) -> bool:
    return name in NAMES

def _bars(rows, first_close):
    return [dict(timestamp=round((first_close+(i-1)*60)*1000),open=float(r[0]),high=float(r[1]),low=float(r[2]),close=float(r[3]),volume=float(r[4])) for i,r in enumerate(rows)]

def batch(rows, names, first_close, symbol='ASSET-USDT', peers=(), asynchronous=False):
    global LAST_ERROR
    selected=sorted(set(n for n in names if known(n)))
    if not selected:return {}
    if len(rows)>5000 or len(selected)>384:raise ValueError('Tactic / Inverse batch exceeds bounds')
    payload=dict(symbol=symbol,bars=_bars(rows,first_close),sources=[dict(name=n,id=NAMES[n][0],tf=NAMES[n][1]) for n in selected],peers=list(peers))
    body=json.dumps(payload,separators=(',',':'),allow_nan=False)
    key=hashlib.sha256(body.encode()).hexdigest()
    with _LOCK:
        cached=_CACHE.get(key)
        if cached is not None:_CACHE.move_to_end(key);return cached
    if asynchronous:
        global _EXECUTOR
        with _LOCK:
            if key not in _PENDING and len(_PENDING)<16:
                if _EXECUTOR is None:_EXECUTOR=ThreadPoolExecutor(max_workers=1,thread_name_prefix='cts-indication')
                _PENDING.add(key)
                def calculate():
                    try:batch(rows,names,first_close,symbol,peers)
                    finally:
                        with _LOCK:_PENDING.discard(key)
                _EXECUTOR.submit(calculate)
        return {n:SignalSeries([0]*len(rows),n,True) for n in selected}
    try:
        result=subprocess.run([os.environ.get('CTS_INDICATION_NODE','node'),'--experimental-strip-types',str(ROOT/'shared/cts-indications/worker.ts')],input=body,text=True,capture_output=True,timeout=15,check=True)
        data=json.loads(result.stdout)
        out={n:SignalSeries(data['states'][n],n) for n in selected}
        if any(len(v)!=len(rows) for v in out.values()):raise ValueError('Invalid source result length')
        with _LOCK:
            _CACHE[key]=out
            while len(_CACHE)>32 or sum(len(v) for entry in _CACHE.values() for v in entry.values())>32*1024*1024:_CACHE.popitem(last=False)
        LAST_ERROR='';return out
    except (OSError,subprocess.SubprocessError,ValueError,KeyError) as exc:
        LAST_ERROR=type(exc).__name__+': '+str(exc)[:160]
        return {n:SignalSeries([0]*len(rows),n,True) for n in selected}

def direct(name, rows):
    # Direct library calls lack a venue clock. The timestamp-independent 1m formulas remain usable;
    # higher timeframes require SetBook's actual completed-bar timestamps and fail closed here.
    if not known(name) or NAMES[name][1]!=1:return None
    result=batch(rows,[name],1_700_000_040.0)
    value=result.get(name,[])
    return (value[-1][0],value[-1][1]) if value and value[-1][0] else None
