"""Independent Relate policy, causal lane, persistence, and bounded sizing checks."""
import math
import json
import os
import tempfile
from copy import deepcopy
import common
from relate_engine import RelateBook, clean_configs, policy_key, stop_within_ratio, stats

ID={'config':'micro:trend:tp6:sl.5','symbol':'BTC-USDT','side':'LONG','indication':'trend:major1:minor2',
    'strategy':'base','coordination':'main-real','range':'micro','lane':'pos'}

def config(**patch):
    return {'id':'test','enabled':True,'steps':12,'ratio':.2,'mode':'shared','sources':['config'],
            'lastN':60,'minTrades':60,'minPf':1.05,'maxStack':8.,'maxSlRatio':.5,**patch}

def warm(book,identity=ID,n=60,ret=.01):
    for i in range(n):
        d=book.decide(identity,i*2,.003,.01,qualified=True)
        book.close(d,str(identity)+str(i),i*2+1,ret,accepted=True,final=True)

def progression_twelve_steps():
    b=RelateBook([config()]);warm(b)
    levels=[];sizes=[]
    for i in range(15):
        d=b.decide(ID,200+i*2,.003,.01,qualified=True)
        p=next(iter(d['policies'].values()))['parts']['config']
        levels.append(p['level']);sizes.append(d['total'])
        b.close(d,'progress'+str(i),201+i*2,.01,accepted=True,final=True)
    want=[min(12,i+1) for i in range(15)]
    reset=b.decide(ID,240,.003,.01,qualified=True)
    b.close(reset,'loss',241,-.001,accepted=True,final=True)
    d=b.decide(ID,242,.003,.01,qualified=True)
    return levels==want and sizes==sorted(sizes) and max(sizes)<=8 and d['total']==1, f'levels={levels}; capped={max(sizes)}; loss reset={d["total"]}'

def independent_contexts():
    b=RelateBook([config()]);warm(b)
    changed=[]
    for key in ID:
        x={**ID,key:ID[key]+'-other'}
        changed.append(b.decide(x,200,.003,.01,qualified=True)['total'])
    qualified=b.decide(ID,200,.003,.01,qualified=True)['total']
    rejected=b.decide(ID,200,.003,.01,qualified=False)['total']
    return all(v==1 for v in changed) and qualified>1 and rejected==1, f'isolated={len(changed)}; own={qualified}; unqualified={rejected}'

def stop_and_cost_floor():
    b=RelateBook([config()]);warm(b)
    cases=[(.005,.01,1.2),(.005001,.01,1),(.0021,.002,1),(0,.01,1),(.002,0,1),(float('nan'),.01,1)]
    got=[b.decide(ID,200,sl,tp,qualified=True)['total'] for sl,tp,_ in cases]
    return all(abs(g-w)<1e-10 for g,(_,_,w) in zip(got,cases)), str(got)

def partials_unknown_duplicates():
    b=RelateBook([config()]);warm(b,n=59)
    d=b.decide(ID,200,.003,.01,qualified=True)
    a=b.close(d,'parent',201,.01,accepted=True,final=False)
    b.close(d,'rejected',201,.01,accepted=False,final=True)
    mid=b.closed
    accepted=b.close(d,'parent',202,None,accepted=True,final=True)
    duplicate=b.close(d,'parent',203,.99,accepted=True,final=True)
    d2=b.decide(ID,204,.003,.01,qualified=True)
    s=next(iter(d2['policies'].values()))['stats']['config']
    return not a and mid==59 and accepted and not duplicate and b.closed==60 and s['unknown']==1 and d2['total']==1, str(s)

def future_and_policy_edit():
    b=RelateBook([config()]);warm(b)
    now=b.decide(ID,30,.003,.01,qualified=True)['total']
    old=b.decide(ID,200,.003,.01,qualified=True)
    b.configure([config(ratio=.4)])
    b.close(old,'old-receipt',201,.01,accepted=True,final=True)
    new=b.decide(ID,202,.003,.01,qualified=True)['total']
    return now==1 and old['total']>1 and new==1, f'future excluded={now}; immutable old={old["total"]}; new policy cold={new}'

def parallel_shared_additive_cap():
    cfgs=[config(id='shared',sources=['config','symbol'],maxStack=2),config(id='additive',mode='additive',sources=['config','symbol'],maxStack=2)]
    b=RelateBook(cfgs);warm(b)
    d=b.decide(ID,200,.003,.01,qualified=True,total_cap=1.3)
    extra=sum(p['extra'] for c in d['policies'].values() for p in c['parts'].values())
    scaled=b.resized(d,1.1)
    actual=sum(p['extra'] for c in scaled['policies'].values() for p in c['parts'].values())
    return abs(d['total']-1.3)<1e-10 and abs(extra-.3)<1e-10 and abs(actual-.1)<1e-10 and len(d['policies'])==2, f'requested={d["total"]}; applied extra={actual}; independent policies={len(d["policies"])}'

def persistence_and_loss_basis():
    with tempfile.TemporaryDirectory() as td:
        p=os.path.join(td,'relate.json');b=RelateBook([config()],p);warm(b)
        d=b.decide(ID,200,.003,.01,qualified=True)
        b.close(d,'tiny-loss',201,-1e-18,accepted=True,final=True)
        b2=RelateBook([config()],p);result=b2.decide(ID,202,.003,.01,qualified=True)
        v=next(iter(result['policies'].values()))['stats']['config']
        return not b2.error and b2.closed==61 and result['total']==1 and v['loss']>0 and not b2.close(d,'tiny-loss',203,.1,accepted=True,final=True), str(v)

def config_bounds():
    bad=[{'ratio':.3},{'steps':16},{'minPf':1},{'maxSlRatio':.5001},{'lastN':20},{'minTrades':61,'lastN':60},{'maxStack':9},{'sources':['unknown']},{'id':''}]
    n=0
    for patch in bad:
        try:clean_configs([config(**patch)])
        except ValueError:n+=1
    return n==len(bad),f'refused={n}/{len(bad)}'

CHECKS=[('relate.twelve-step-growth-loss-reset',progression_twelve_steps),('relate.exact-context-isolation',independent_contexts),
 ('relate.effective-stop-cost-floor-half-target',stop_and_cost_floor),('relate.parent-partials-unknown-idempotence',partials_unknown_duplicates),
 ('relate.future-exclusion-immutable-policy',future_and_policy_edit),('relate.parallel-portions-total-cap',parallel_shared_additive_cap),
 ('relate.restart-tiny-loss-preservation',persistence_and_loss_basis),('relate.settings-boundaries',config_bounds)]

def native_entry_close_and_recovery():
    from unittest.mock import patch
    import lanes
    import pulse_trader as pt
    from position_cost import tp_range_of
    b=lanes._book(setMinStep=7,setStepMax=7,setSlRatios=[.25],exitIgnoreTp=False)
    st=next(s for s in b.by_idx if s.pack=='indications' and s.kind=='base')
    ident={'config':st.id,'symbol':lanes.SYM,'side':'LONG','indication':st.pack+':'+st.tf,
           'strategy':st.kind,'coordination':'Main-Real','range':tp_range_of(b.step_bucket(st)),'lane':'pos'}
    rb=RelateBook([config()]);warm(rb,ident)
    rb.configure=lambda _:None
    bars=lanes._scenario_bars('tp',1)
    with patch.object(pt,'RelateBook',lambda *a,**kw:rb):
        _,close,_=lanes.live(b,st,bars,5,1)
    p=lanes.live.pulse
    if not close or not p.closed:return False,'native position did not settle'
    row=p.closed[-1];receipt=row.relate_receipt
    ok=bool(receipt) and receipt['total']>receipt['base'] and rb.closed==61 and not rb.pending
    return ok,f'native close={close}; volume={receipt.get("total")}; closed parents={rb.closed}; pending={len(rb.pending)}'


def persisted_intent_never_becomes_a_fill():
    with tempfile.TemporaryDirectory() as td:
        p=os.path.join(td,'state.json');b=RelateBook([config()],p);warm(b)
        d=b.decide(ID,200,.003,.01,qualified=True)
        b.reserve('pending',d,.1,{'sl':.003,'tp':.01,'setId':ID['config'],'lane':ID['lane']})
        loaded=RelateBook([config()],p)
        ok=loaded.closed==60 and loaded.pending['pending']['receipt']==d
        loaded.release('pending')
        return ok and not RelateBook([config()],p).pending,'pending ownership persists without inventing a close'

CHECKS += [('relate.native-entry-and-final-close',native_entry_close_and_recovery),('relate.pending-intent-restart',persisted_intent_never_becomes_a_fill)]


def overflow_and_same_time():
    assert stats([1e308,1e308])['state']=='unknown'
    assert stats([1e308,-1e-300])['state']=='unknown'
    b=RelateBook([config()]);warm(b,n=59)
    d=b.decide(ID,200,.003,.01,qualified=True)
    b.close(d,'same-clock',201,.01,accepted=True,final=True)
    assert b.decide(ID,201,.003,.01,qualified=True)['total']==1
    assert b.decide(ID,202,.003,.01,qualified=True)['total']>1
    return True,'overflow fails closed; same-time close is not prior evidence'
CHECKS.append(('relate.numeric-overflow-and-clock',overflow_and_same_time))


def corrupt_state_and_atomic_receipts():
    configs=[config(id='a'),config(id='b')]
    b=RelateBook(configs);warm(b)
    receipt=b.decide(ID,200,.003,.01,qualified=True)
    changed=deepcopy(receipt);list(changed['policies'].values())[-1]['parts']['config']['volume']=-1
    before=deepcopy((b.books,b.seen,b.closed))
    try:b.close(changed,'invalid',201,.01,accepted=True,final=True)
    except ValueError:pass
    else:return False,'invalid late policy accepted'
    assert (b.books,b.seen,b.closed)==before
    with tempfile.TemporaryDirectory() as td:
        path=os.path.join(td,'bad.json')
        valid={'version':1,'books':b.books,'seen':b.seen,'closed':b.closed,'pending':{}}
        mutations=[{'books':[]},{'seen':[]},{'pending':[]},{'closed':-1},
                   {'books':{'bad':{'relation':{'rows':[[1,.1,-1,1]]}}}},
                   {'pending':{'pending':{'receipt':changed,'originQty':1,'protection':{}}}}]
        for mutation in mutations:
            with open(path,'w') as f:json.dump({**valid,**mutation},f)
            restored=RelateBook(configs,path)
            assert restored.error and restored.snapshot()['observations']==0
            assert restored.decide(ID,202,.003,.01,qualified=True)['total']==1
        b.reserve('owned',receipt,1,{})
        receipt['total']=100
        assert b.pending['owned']['receipt']['total']<100
    return True,'6 corrupt snapshots refused; no partial policy mutation; pending receipt copied'
CHECKS.append(('relate.corrupt-state-and-atomic-receipt',corrupt_state_and_atomic_receipts))
