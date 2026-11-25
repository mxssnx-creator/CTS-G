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
    bad=[{'ratio':.3},{'steps':16},{'minPf':1},{'maxSlRatio':1.2501},{'lastN':9},{'minTrades':4},{'minTrades':61,'lastN':60},{'maxStack':9},{'sources':['unknown']},{'id':''}]
    n=0
    for patch in bad:
        try:clean_configs([config(**patch)])
        except ValueError:n+=1
    return n==len(bad),f'refused={n}/{len(bad)}'

def config_wide_source_pools_symbols():
    """Owner 2026-11-25: source `configAll` is the same Set (config id, side, lane) over ALL symbols. Ten closes spread over two symbols qualify it, while the per-symbol
    `config` source of either symbol (5 closes each) does not reach minTrades 10."""
    cfg=config(sources=['config','configAll'],lastN=20,minTrades=10,minPf=1.05,maxStack=2.)
    b=RelateBook([cfg]);a=dict(ID);c=dict(ID,symbol='ETH-USDT')
    for i in range(10):
        ident=a if i%2==0 else c
        d=b.decide(ident,i*2,.003,.01,qualified=True)
        b.close(d,'w%d'%i,i*2+1,.01,accepted=True,final=True)
    d=b.decide(a,100,.003,.01,qualified=True);parts=next(iter(d['policies'].values()))['parts']
    only=RelateBook([config(sources=['config'],lastN=20,minTrades=10)])
    for i in range(10):
        ident=a if i%2==0 else c
        e=only.decide(ident,i*2,.003,.01,qualified=True);only.close(e,'o%d'%i,i*2+1,.01,accepted=True,final=True)
    e=only.decide(a,100,.003,.01,qualified=True)
    return parts['configAll']['level']>0 and parts['config']['level']==0 and e['total']==1.,f"configAll level {parts['configAll']['level']}, per-symbol config level {parts['config']['level']}, config-only total {e['total']}"

def lowered_minimum_and_stop_ceiling():
    """minTrades 10 qualifies at 10 closes and not at 9; maxSlRatio 1.25 admits a stop of 1.2 x TP and refuses 1.3 x TP."""
    def run(n,sl):
        b=RelateBook([config(sources=['config'],lastN=20,minTrades=10,maxSlRatio=1.25)]);warm(b,n=n)
        d=b.decide(ID,500,sl,.01,qualified=True);return next(iter(d['policies'].values()))['parts']['config']['level']
    ok10,ok9=run(10,.003),run(9,.003)
    near,far=run(10,.012),run(10,.013)
    return ok10>0 and ok9==0 and near>0 and far==0,f'level at 10 closes {ok10}, at 9 {ok9}; stop 1.2 x TP {near}, 1.3 x TP {far}'

def default_policy_is_unchanged_by_the_new_source():
    """A policy without configAll builds the same receipt parts and keys as before the source existed."""
    b=RelateBook([config()]);warm(b);d=b.decide(ID,200,.003,.01,qualified=True)
    p=next(iter(d['policies'].values()))
    return set(p['parts'])=={'config'} and p['parts']['config']['level']==1 and d['total']>1,f"sources {sorted(p['parts'])}, level {p['parts']['config']['level']}, total {d['total']}"

CHECKS=[('relate.config-wide-source-pools-symbols',config_wide_source_pools_symbols),('relate.lowered-minimum-and-stop-ceiling',lowered_minimum_and_stop_ceiling),('relate.default-policy-unchanged-by-the-new-source',default_policy_is_unchanged_by_the_new_source),
 ('relate.twelve-step-growth-loss-reset',progression_twelve_steps),('relate.exact-context-isolation',independent_contexts),
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


def _pending_recovery(side='LONG', changes=None):
    """An owned intent and a position snapshot are separate from its order fill."""
    from types import SimpleNamespace
    import pulse_trader as pt
    from execution_cost import FeeSchedule
    identity = {**ID, 'side': side}
    book = RelateBook([config()])
    warm(book, identity)
    receipt = book.decide(identity, 200, .003, .01, qualified=True)
    cid = pt.TAG + 'o-relate-restart'
    book.reserve(cid, receipt, 1, {'sl': .003, 'tp': .01,
                                'setId': identity['config'], 'lane': identity['lane']})
    pos = pt.Position(identity['symbol'], side, receipt['total'], 99.999, 1000,
                      90, 110, 100)
    order = {'orderId': '11', 'symbol': pos.symbol, 'positionSide': side,
             'side': 'BUY' if side == 'LONG' else 'SELL', 'clientOrderID': cid,
             'executedQty': str(pos.qty), 'origQty': str(pos.qty),
             'avgPrice': '100', 'status': 'FILLED', **(changes or {})}
    pulse = pt.Pulse.__new__(pt.Pulse)
    pulse.relate, pulse.overlay = book, {}
    pulse.contracts = {pos.symbol: SimpleNamespace(maker_fee=.0002, taker_fee=.0005)}
    pulse.fee_schedule = FeeSchedule()
    pulse.api = SimpleNamespace(get=lambda *args: {'code': 0, 'data': {'order': order}})
    pulse.open = {pos.symbol: pos}
    saved = []
    pulse.save_open_book = lambda: saved.append(True) or True
    return pulse, pos, order, saved


def recovered_relate_requires_exact_reported_fill():
    from dataclasses import asdict
    refused = []
    mutations = [{'symbol': 'FOREIGN-USDT'}, {'positionSide': 'SHORT'}, {'side': 'SELL'},
                 {'clientOrderID': 'foreign'}, {'orderId': ''},
                 {'executedQty': None}, {'executedQty': ''}, {'executedQty': 'NaN'},
                 {'executedQty': '0'}, {'executedQty': '999'},
                 {'avgPrice': 'NaN'}, {'avgPrice': 'Infinity'},
                 {'avgPrice': 'invalid'}, {'avgPrice': '0'}]
    for changes in mutations:
        pulse, pos, _row, saved = _pending_recovery(changes=changes)
        before, pending = asdict(pos), deepcopy(pulse.relate.pending)
        try:
            recovered = pulse.recover_relate_intent(pos)
        except (TypeError, ValueError, OverflowError):
            recovered = True  # malformed evidence must return unknown, not abort reconciliation
        refused.append(not recovered and asdict(pos) == before
                       and pulse.relate.pending == pending and not saved)
    for reply in ([], {'code': 0, 'data': ['not-an-order']},
                  {'code': 0, 'data': 'invalid'}, {'code': 0, 'data': {'order': []}}):
        pulse, pos, _row, saved = _pending_recovery()
        pulse.api.get = lambda *args: reply
        before, pending = asdict(pos), deepcopy(pulse.relate.pending)
        try:
            recovered = pulse.recover_relate_intent(pos)
        except (AttributeError, TypeError, ValueError, OverflowError):
            recovered = True
        refused.append(not recovered and asdict(pos) == before
                       and pulse.relate.pending == pending and not saved)
    return all(refused), f'foreign/missing/malformed fill scopes refused without mutation={refused}'


def recovered_relate_fee_evidence_survives_restart():
    from dataclasses import asdict
    import pulse_trader as pt
    results = []
    for side in ('LONG', 'SHORT'):
        for reported_zero in (False, True):
            pulse, pos, order, saved = _pending_recovery(
                side, {'commission': 0} if reported_zero else {})
            recovered = pulse.recover_relate_intent(pos)
            restored = pt.Position(**json.loads(json.dumps(asdict(pos))))
            source = 'reported-fee' if reported_zero else 'exchange-rate-estimate'
            expected_entry_fee = 0 if reported_zero else pos.qty * 100 * .0005
            evidence = (recovered and bool(saved) and not pulse.relate.pending
                        and restored.order_id == '11' and restored.entry == restored.base_entry == 100
                        and restored.notional == restored.qty * 100 and restored.opened_at == 1000
                        and restored.fee_source == source
                        and abs(restored.entry_fee_usdt - expected_entry_fee) < 1e-12
                        and restored.fee_legs.get('11', {}).get('at') == 200)
            exit_order = {**order, 'orderId': '12',
                          'side': 'SELL' if side == 'LONG' else 'BUY'}
            pulse.fill_cost(exit_order, restored.qty, 101, restored.symbol, restored)
            reads = []
            def exact(leg, since):
                reads.append((leg['orderId'], since))
                return .059 if leg['role'] == 'entry' else .061
            promoted = pt.reconcile_position_fees(restored, exact)
            # A reported zero remains zero; rate estimates may become exact once.
            expected_reads = [] if reported_zero else [('11', 200), ('12', 1000)]
            repeat = pt.reconcile_position_fees(restored, lambda *a: (_ for _ in ()).throw(
                AssertionError('complete fees queried again')))
            results.append(evidence and promoted and repeat and reads == expected_reads
                           and restored.fee_source == 'reported-fee'
                           and restored.base_entry_fee_usdt == (0 if reported_zero else .059))
    return all(results), f'long/short rates and reported-zero fees retain exact restart evidence={results}'


CHECKS += [('relate.recovered-intent-requires-exact-fill', recovered_relate_requires_exact_reported_fill),
           ('relate.recovered-intent-retains-fee-evidence', recovered_relate_fee_evidence_survives_restart)]
