"""Causal post-Base Relate sizing, shared by Pulse and offline execution studies.

Ported from CTS-A-O's RelateBook contract: a qualified winning parent can grow
its next recorded portion; losses reset it. Sizing never improves unit-return PF
by itself. Only confirmed owned final parents teach this live book. Parallel
policies and relations keep independent histories and immutable entry receipts.
"""
from __future__ import annotations
from collections import deque
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from typing import Any

SOURCES = ('config', 'overall', 'symbol', 'direction', 'indication', 'strategy', 'coordination', 'range')
MAX_STACK = 8.0
DEFAULT = {'id': 'relate-12', 'enabled': False, 'steps': 12, 'ratio': .2,
           'mode': 'shared', 'sources': ['config', 'symbol'], 'lastN': 250,
           'minTrades': 60, 'minPf': 1.1, 'maxStack': 2., 'maxSlRatio': .5}


def finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def clean_configs(raw):
    if raw is None:
        return [dict(DEFAULT)]
    if not isinstance(raw, list) or not 1 <= len(raw) <= 12:
        raise ValueError('Relate requires 1..12 independent policies')
    result, ids = [], set()
    for patch in raw:
        if not isinstance(patch, dict): raise ValueError('Relate policy must be an object')
        c = {**DEFAULT, **patch}
        if not isinstance(c['id'], str) or not c['id'] or len(c['id']) > 64 or c['id'] in ids:
            raise ValueError('Relate policy IDs must be unique nonempty strings')
        ids.add(c['id'])
        if not isinstance(c['enabled'], bool): raise ValueError('Relate enabled must be boolean')
        for key, lo, hi in [('steps', 2, 15), ('lastN', 60, 250), ('minTrades', 60, 250)]:
            x=c[key]
            if not finite(x) or x != int(x) or not lo <= x <= hi: raise ValueError('Invalid Relate '+key)
            c[key]=int(x)
        if c['minTrades'] > c['lastN']: raise ValueError('Relate minTrades exceeds lastN')
        for key, lo, hi in [('ratio', .2, 6), ('minPf', 1.000001, 100), ('maxStack', 1, MAX_STACK), ('maxSlRatio', .01, .5)]:
            if not finite(c[key]) or not lo <= c[key] <= hi: raise ValueError('Invalid Relate '+key)
        if abs(c['ratio']*5-round(c['ratio']*5)) > 1e-9: raise ValueError('Relate ratios use 0.2 increments')
        if c['mode'] not in ('shared','additive'): raise ValueError('Invalid Relate mode')
        if not isinstance(c['sources'],list) or not c['sources'] or any(s not in SOURCES for s in c['sources']):
            raise ValueError('Invalid Relate sources')
        c['sources']=[s for s in SOURCES if s in c['sources']]
        result.append(c)
    return result


def policy_key(c):
    return c['id']+':'+hashlib.sha256(json.dumps(c,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:20]


def identity_keys(x):
    required=('config','symbol','side','indication','strategy','coordination','range','lane')
    if not all(isinstance(x.get(k),str) and x[k] for k in required): raise ValueError('Incomplete Relate lane identity')
    # Broader relations are intentional sizing contexts, never substitutes for exact config qualification.
    lane=x['lane']
    return {
        'config': json.dumps([x[k] for k in required],separators=(',',':')),
        'overall': json.dumps(['overall',lane]),
        'symbol': json.dumps([x['symbol'],x['side'],lane]),
        'direction': json.dumps([x['side'],lane]),
        'indication': json.dumps([x['indication'],x['side'],lane]),
        'strategy': json.dumps([x['strategy'],x['side'],lane]),
        'coordination': json.dumps([x['coordination'],x['side'],lane]),
        'range': json.dumps([x['range'],x['side'],lane]),
    }


def stats(values):
    if not values: return {'n':0,'pf':None,'state':'empty','unknown':0,'gain':0.,'loss':0.}
    unknown=sum(not finite(v) for v in values)
    if unknown: return {'n':len(values),'pf':None,'state':'unknown','unknown':unknown,'gain':None,'loss':None}
    try:
        gp=math.fsum(v for v in values if v>0);gl=-math.fsum(v for v in values if v<0)
        pf=gp/gl if gl>0 else None
        if not math.isfinite(gp) or not math.isfinite(gl) or (pf is not None and not math.isfinite(pf)): raise OverflowError()
    except OverflowError:
        return {'n':len(values),'pf':None,'state':'unknown','unknown':len(values),'gain':None,'loss':None}
    state='finite' if gl>0 else 'no-loss' if gp>0 else 'flat'
    return {'n':len(values),'pf':pf,'state':state,'unknown':0,'gain':gp,'loss':gl}


def qualifies(s,c):
    return s['n']>=c['minTrades'] and not s['unknown'] and (s['state']=='no-loss' or (s['pf'] is not None and s['pf']>=c['minPf']))


def stop_within_ratio(sl,tp,ratio):
    # Effective price distances, after costs/minimum-distance and tick rounding. No stop tightening or TP invention.
    return finite(sl) and finite(tp) and finite(ratio) and sl>0 and tp>0 and sl<=tp*ratio+1e-12


def receipt_parts(receipt):
    """Validate the complete immutable receipt before mutating any relation."""
    if not isinstance(receipt,dict): raise ValueError('Invalid Relate receipt')
    keys=identity_keys(receipt.get('identity',{}))
    if not finite(receipt.get('at')) or not isinstance(receipt.get('qualified'),bool): raise ValueError('Invalid Relate receipt clock')
    base,total=receipt.get('base'),receipt.get('total')
    if not finite(base) or not finite(total) or not 0<base<=total<=MAX_STACK: raise ValueError('Invalid Relate funded total')
    policies=receipt.get('policies')
    if not isinstance(policies,dict) or len(policies)>12: raise ValueError('Invalid Relate receipt policies')
    updates=[];extra=0.
    for pk,p in policies.items():
        c=clean_configs([p['policy']])[0]
        if pk!=policy_key(c) or set(p['parts'])!=set(c['sources']): raise ValueError('Relate receipt policy changed')
        for source in c['sources']:
            part=p['parts'][source];volume=part.get('volume');level=part.get('level');addition=part.get('extra')
            if not finite(volume) or not 0<volume<=MAX_STACK or not finite(addition) or addition<0 or abs(volume-base-addition)>1e-9:
                raise ValueError('Invalid Relate funded portion')
            if type(level) is not int or not 0<=level<=c['steps']: raise ValueError('Invalid Relate level')
            extra+=addition;updates.append((pk,source+':'+keys[source],volume,level,c['lastN']))
    if abs(base+extra-total)>1e-9: raise ValueError('Relate funded portions do not reconcile')
    return updates


def validate_saved(saved):
    if not isinstance(saved,dict) or saved.get('version')!=1: raise ValueError('Unverified Relate state version')
    books,seen,pending=saved['books'],saved['seen'],saved.get('pending',{})
    if not all(isinstance(x,dict) for x in (books,seen,pending)): raise ValueError('Invalid Relate state maps')
    if type(saved['closed']) is not int or saved['closed']!=len(seen): raise ValueError('Invalid Relate close count')
    if any(not isinstance(k,str) or not k or not finite(t) for k,t in seen.items()): raise ValueError('Invalid Relate close identity')
    for pk,book in books.items():
        if not isinstance(pk,str) or not isinstance(book,dict): raise ValueError('Invalid Relate policy book')
        for key,state in book.items():
            if not isinstance(key,str) or not isinstance(state,dict) or not isinstance(state.get('rows'),list): raise ValueError('Invalid Relate relation')
            rows=state['rows'];previous=-math.inf
            if len(rows)>250: raise ValueError('Relate history exceeds capacity')
            for row in rows:
                if not isinstance(row,list) or len(row)!=4: raise ValueError('Invalid Relate history row')
                at,r,volume,level=row
                if not finite(at) or at<previous or (r is not None and not finite(r)) or not finite(volume) or not 0<volume<=MAX_STACK or type(level) is not int or not 0<=level<=15:
                    raise ValueError('Invalid Relate history values')
                previous=at
    for cid,intent in pending.items():
        if not isinstance(cid,str) or not cid or not isinstance(intent,dict) or not finite(intent.get('originQty')) or intent['originQty']<=0 or not isinstance(intent.get('protection'),dict): raise ValueError('Invalid Relate pending entry')
        receipt_parts(intent['receipt'])


class RelateBook:
    def __init__(self, configs=None, path=None):
        self.configs=clean_configs(configs);self.path=path;self.books={};self.seen={};self.pending={};self.error='';self.closed=0
        if path and os.path.exists(path):
            try:
                with open(path) as f: saved=json.load(f)
                validate_saved(saved)
                self.books=saved['books'];self.seen=saved['seen'];self.closed=saved['closed']
                self.pending=saved.get('pending',{})
                if len(self.seen)>100000: self.error='Relate audit capacity reached; new raises disabled'
            except Exception as e:
                self.error='Relate state unavailable: '+type(e).__name__

    def configure(self, configs):
        self.configs=clean_configs(configs)

    def decide(self, identity, now, sl, tp, *, qualified=False, base=1., total_cap=MAX_STACK):
        if not finite(now) or not finite(base) or base<=0 or not finite(total_cap) or total_cap<=0:
            raise ValueError('Invalid Relate decision inputs')
        keys=identity_keys(identity);cap=min(MAX_STACK,total_cap);base=min(base,cap)
        receipt={'identity':dict(identity),'at':now,'base':base,'total':base,'policies':{},'qualified':qualified is True}
        if not qualified or self.error: return receipt
        requests=[]
        for c in self.configs:
            if not c['enabled']: continue
            pk=policy_key(c);book=self.books.get(pk,{})
            parts={};diagnostics={};local_cap=min(cap,c['maxStack']);eligible=stop_within_ratio(sl,tp,c['maxSlRatio'])
            for source in c['sources']:
                state=book.get(source+':'+keys[source],{})
                rows=[r for r in state.get('rows',[]) if r[0]<now][-c['lastN']:]
                s=stats([r[1] for r in rows]);diagnostics[source]=s
                prev=rows[-1] if rows else None
                known=prev and len(prev)>=4 and finite(prev[2]) and prev[2]>0 and isinstance(prev[3],int)
                level=min(c['steps'],prev[3]+1) if eligible and qualifies(s,c) and known and prev[1]>0 else 0
                vol=min(local_cap,prev[2]*(1+c['ratio']) if prev[3]<c['steps'] else prev[2]) if level else base
                parts[source]={'level':level,'extra':max(0.,vol-base)}
            active=[p for p in parts.values() if p['level']>0]
            divisor=max(1,len(active)) if c['mode']=='shared' else 1
            for p in parts.values(): p['extra']/=divisor
            wanted=sum(p['extra'] for p in parts.values());room=max(0.,local_cap-base)
            scale=min(1.,room/wanted) if wanted else 0.
            for p in parts.values(): p['extra']*=scale
            requested=sum(p['extra'] for p in parts.values())
            receipt['policies'][pk]={'id':c['id'],'policy':c,'parts':parts,'stats':diagnostics,'stopEligible':eligible}
            requests.append(requested)
        extra=sum(requests);factor=min(1.,max(0.,cap-base)/extra) if extra else 0.
        for p in receipt['policies'].values():
            for part in p['parts'].values():
                part['extra']*=factor;part['volume']=base+part['extra']
        receipt['total']=min(cap,base+extra*factor)
        return receipt

    @staticmethod
    def resized(receipt, actual_total):
        """Capture venue-rounded/clipped effective portions, never a requested but unfunded raise."""
        r=json.loads(json.dumps(receipt,allow_nan=False));base=r['base'];wanted=r['total']-base
        if not finite(actual_total) or actual_total<=0: raise ValueError('Invalid funded Relate volume')
        if actual_total>r['total']+1e-9: raise ValueError('Venue sizing exceeded Relate request')
        actual_base=min(base,actual_total);extra=max(0.,actual_total-actual_base)
        for p in r['policies'].values():
            for part in p['parts'].values():
                part['extra']=part['extra']*extra/wanted if wanted>0 else 0.
                part['volume']=actual_base+part['extra']
        r['base']=actual_base;r['total']=actual_total
        return r

    def close(self, receipt, parent_id, at, unit_return, *, accepted=False, final=False):
        if accepted is not True or final is not True or not parent_id or not receipt.get('qualified') or not receipt.get('policies'): return False
        if not finite(at) or at<receipt['at']: raise ValueError('Relate close precedes entry')
        event=str(parent_id)
        if event in self.seen: return False
        updates=receipt_parts(receipt)
        r=float(unit_return) if finite(unit_return) else None
        for pk,key,volume,level,last_n in updates:
            book=self.books.setdefault(pk,{})
            state=book.setdefault(key,{'rows':[]});rows=state['rows']
            rows.append([at,r,volume,level])
            rows.sort(key=lambda x:x[0]);del rows[:-last_n]
        self.seen[event]=at;self.closed+=1
        # Never prune deduplication evidence and then re-admit an old parent as a new observation.
        if len(self.seen)>100000:
            self.error='Relate audit capacity reached; new raises disabled'
        self.save();return True

    def reserve(self, client_id, receipt, origin_qty, protection):
        if not client_id or not finite(origin_qty) or origin_qty<=0: raise ValueError('Invalid Relate entry intent')
        receipt_parts(receipt)
        if not isinstance(protection,dict): raise ValueError('Invalid Relate protection')
        self.pending[client_id]=json.loads(json.dumps({'receipt':receipt,'originQty':origin_qty,'protection':protection},allow_nan=False))
        self.save()

    def release(self, client_id):
        if client_id in self.pending:
            del self.pending[client_id]
            self.save()

    def save(self):
        if not self.path: return
        try:
            tmp=self.path+'.tmp'
            with open(tmp,'w') as f: json.dump({'version':1,'books':self.books,'seen':self.seen,'pending':self.pending,'closed':self.closed},f,allow_nan=False)
            os.replace(tmp,self.path)
        except Exception:
            self.error='Relate persistence failed; new raises disabled'
            raise

    def snapshot(self):
        return {'configs':self.configs,'closedParents':self.closed,'pendingEntries':len(self.pending),'relations':sum(len(b) for b in self.books.values()),
                'observations':sum(len(v['rows']) for b in self.books.values() for v in b.values()),'error':self.error,
                'basis':'confirmed owned parent unit returns; sizing portions tracked separately'}
