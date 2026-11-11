"""Independent per-configuration stage windows. The policy is part of the lane identity."""
from __future__ import annotations
import copy
import math

DEFAULT_LAST_N_LANES = [
    {'id':'major','role':'major','lastN':{'baseLong':30,'baseShort':15,'mainLong':20,'mainShort':[5,10],'realN':12,'realNegN':12}},
    {'id':'minor','role':'minor','lastN':{'baseLong':20,'baseShort':10,'mainLong':10,'mainShort':[5],'realN':8,'realNegN':8}},
    {'id':'independent','role':'independent','lastN':{'baseLong':10,'baseShort':5,'mainLong':10,'mainShort':[1,3,5],'realN':5,'realNegN':5}},
]
ROLES = ('major','minor','independent','additional')
SCOPES = ('overall','symbol','indication','type','config')

def negative_filter(raw):
    """Optional recent-loss veto. This never converts losses into inverse profits."""
    if raw is None: return None
    if not isinstance(raw,dict) or set(raw)-{'windows','mode','maxPf'}: raise ValueError('Invalid negative filter')
    windows=raw.get('windows');mode=raw.get('mode','any');floor=raw.get('maxPf',1.)
    if not isinstance(windows,list) or not windows or len(windows)>16 or any(not number(n,1,200,True) for n in windows):
        raise ValueError('Invalid negative filter windows')
    if mode not in ('any','all') or not number(floor,0,1): raise ValueError('Invalid negative filter rule')
    return {'windows':sorted(set(windows)),'mode':mode,'maxPf':floor}

def negative_filter_ok(values, policy):
    if not policy: return True
    bad=[]
    for n in policy['windows']:
        if len(values)<n: return False
        rows=values[-n:]
        if any(not isinstance(v,(int,float)) or isinstance(v,bool) or not math.isfinite(v) for v in rows): return False
        try:
            gain=math.fsum(max(0.,v) for v in rows);loss=math.fsum(max(0.,-v) for v in rows)
        except OverflowError: return False
        if not math.isfinite(gain) or not math.isfinite(loss): return False
        bad.append(loss>gain and gain <= loss*policy['maxPf'])
    return not (any(bad) if policy['mode']=='any' else all(bad))

def number(v,lo,hi,integer=False):
    return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and lo<=v<=hi and (not integer or int(v)==v)

def last_n(raw):
    if not isinstance(raw,dict): raise ValueError('lastN must be an object')
    result={}
    for k,v in raw.items():
        if k in ('mainShort','negWindows'):
            if not isinstance(v,list) or not v or len(v)>16 or any(not number(n,1,200,True) for n in v): raise ValueError('Invalid '+k)
            result[k]=sorted(set(v))
        elif k in ('baseLong','mainLong','baseShort','realN','realNegN'):
            lo=0 if k=='baseShort' else 1
            if not number(v,lo,200,True): raise ValueError('Invalid '+k)
            result[k]=int(v)
        else: raise ValueError('Unknown lastN field '+str(k))
    return result

def profiles(raw):
    if raw is None: return []  # preserve existing configuration until explicitly migrated
    if not isinstance(raw,list) or len(raw)>8: raise ValueError('At most 8 independent Last-N lanes')
    result=[]; ids=set()
    for item in raw:
        if not isinstance(item,dict): raise ValueError('Invalid Last-N lane')
        ident=item.get('id');role=item.get('role','independent');scope=item.get('scope','config')
        if not isinstance(ident,str) or not ident.strip() or len(ident)>64 or ident in ids: raise ValueError('Unique Last-N lane ID required')
        if role not in ROLES or scope not in SCOPES: raise ValueError('Invalid Last-N role or scope')
        ids.add(ident)
        p={'id':ident,'role':role,'scope':scope,'lastN':last_n(item.get('lastN',{}))}
        if 'negativeFilter' in item: p['negativeFilter']=negative_filter(item['negativeFilter'])
        for key in ('minPfByStep','minPfBySlRatio'):
            m=item.get(key,{})
            if not isinstance(m,dict): raise ValueError('Invalid '+key)
            checked={}
            for k,v in m.items():
                try: x=float(k)
                except (TypeError,ValueError): raise ValueError('Invalid '+key+' index')
                if not (number(x,2,50,True) if key=='minPfByStep' else number(x,.25,2.5)): raise ValueError('Invalid '+key+' index')
                if not number(v,1.000001,100): raise ValueError('PF must be finite and positive above 1')
                checked[str(int(x)) if x==int(x) else str(x)]=v
            p[key]=checked
        result.append(p)
    return result

def expand_entries(configs):
    """Variants never vote each other into a pass: each builds its own native Base/Main/Real tape.

    scope is reporting/coordination metadata, not permission to borrow another exact config's closes.
    Explicit symbols produce separate configured packs; an empty symbol list preserves the original pool.
    """
    if not isinstance(configs,(list,tuple)): return []
    result=[]
    for item in configs:
        if not isinstance(item,dict): continue
        variants=profiles(item.get('lastNLanes')) or [None]
        symbols=item.get('symbols',[])
        if not isinstance(symbols,list) or len(symbols)>200 or any(not isinstance(s,str) or not s.strip() for s in symbols): raise ValueError('Invalid lane symbols')
        for variant in variants:
            for symbol in sorted(set(symbols)) or ['']:
                p=copy.deepcopy(item);p.pop('lastNLanes',None);p.pop('symbols',None)
                p['lanePolicy']=variant;p['symbol']=symbol
                result.append(p)
    return result
