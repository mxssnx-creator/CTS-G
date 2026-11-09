"""Actual native configuration expansion and stage isolation, without venue writes."""
import copy
from common import overlay, synth_bars
from set_engine import SetBook
from lane_policy import DEFAULT_LAST_N_LANES, profiles
from indication_config import source_name

def book():
    b=SetBook(); b.load(overlay(stratGeneral=False,stratIndications=False,stratShortRange=False,directionMode="split",holdAuto=False,
        indicationConfigs=[{'label':'owned','names':['momchg_n5_t1'],'relations':['','T5'],
          'stepMin':7,'stepMax':7,'slRatios':[.5],'types':['base'],
          'major':'trend','minor':'child','symbols':['BTC-USDT','ETH-USDT'],
          'lastNLanes':DEFAULT_LAST_N_LANES}]))
    return b

def separate_native_lanes():
    b=book(); packs=[p for p in b.packs if '~cfg' in p]
    assert len(packs)==12 and len({x.id for x in b.by_idx})==24
    assert not b.config_report['dropped']
    seen=set()
    for st in b.by_idx:
        meta=b.pack_ranges[st.pack];p=meta['lanePolicy'];w=b.win(st)
        assert w.base_long==p['lastN']['baseLong'] and w.real_n==p['lastN']['realN']
        assert b.stage_params(st.pack,st).real_n==w.real_n
        assert meta['major']=='trend' and meta['minor']=='child'
        assert b.symbol_allows(st.pack,meta['symbol']) and not b.symbol_allows(st.pack,'OTHER')
        seen.add((meta['symbol'],p['id'],source_name(st.pack),st.side))
    assert len(seen)==24
    a,c=b.by_idx[:2];a.proc.append((1.,-.1));assert not c.proc
    return True,'24 native symbol × relation × role × side sets; separate Main histories and windows'

def scoped_replay():
    b=book();bars=synth_bars(17,250);b.bars['BTC-USDT']=bars;b.bar_t['BTC-USDT']=1800000000-60
    hist={};b._replay_symbol('BTC-USDT',hist,1800000000.)
    assert hist and all(b.pack_ranges[b.sets[k].pack]['symbol']=='BTC-USDT' for k in hist)
    return True,'ETH sets never consume BTC native replay closes'

def edits_cold_and_floor_consistent():
    b=book();original=set(b.sets)
    ov=overlay(stratGeneral=False,stratIndications=False,stratShortRange=False,directionMode="split",holdAuto=False,
      indicationConfigs=[{'names':['momchg_n5_t1'],'stepMin':7,'stepMax':7,'slRatios':[.5],'types':['base'],
       'lastNLanes':[{'id':'minor','role':'minor','lastN':{'baseLong':20,'mainLong':10,'realN':8},
                      'minPfByStep':{'7':1.06},'minPfBySlRatio':{'.5':1.08}}]}])
    b.load(ov);assert not original.intersection(b.sets)
    for st in b.by_idx:
        assert b.floor_for(8,st.pack,st)==1.08 and b.stage_params(st.pack,st).floor==1.08
    ov['indicationConfigs'][0]['lastNLanes'][0]['lastN']['realN']=12
    previous=set(b.sets);b.load(ov);assert not previous.intersection(b.sets)
    return True,'PF overrides reach every native stage; policy edits cannot reuse a prior processed lane'

def invalid_policy_refused():
    for patch in [{'id':'x','lastN':{'realN':0}}, {'id':'x','minPfByStep':{'7':1}},
                  {'id':'x','lastN':{'mainLong':float('nan')}}, {'id':'x','scope':'unknown'}]:
        try: profiles([patch])
        except ValueError: pass
        else: raise AssertionError(patch)
    try: profiles([{'id':'x'},{'id':'x'}])
    except ValueError: pass
    else: raise AssertionError('duplicate')
    return True,'malformed windows, floor and duplicate identities fail before construction'

def full_base_and_negative_windows():
    from dataclasses import replace
    from stages import CFG_FULL
    from set_engine import stage_chain, stage_verdicts
    from lane_policy import negative_filter, negative_filter_ok
    p=replace(CFG_FULL,base_long=5,base_short=0,full_window=True,main_on=False,neg_on=False)
    ts=list(range(2,42,2));en=[t-1 for t in ts];nets=[.01]*len(ts)
    assert stage_chain(ts,en,nets,p)['main'][0]==5, 'full Base must wait for all 5 closes'
    assert stage_chain(ts,en,nets,replace(p,full_window=False))['main'][0]==1
    policy=negative_filter({'windows':[1,3],'mode':'any','maxPf':1})
    own=[.02,.02,.02,-.001]
    v=stage_verdicts(own,own,[],3,replace(p,main_on=True,main_n=3,short_on=False,real_n=3,negative_filter=policy))
    assert v['main_pf']>1 and not v['pos'] and not v['mainLossFilter']
    assert negative_filter_ok(own,negative_filter({'windows':[1,3],'mode':'all'}))
    assert not negative_filter_ok([.02,float('nan'),.02],policy)
    assert not negative_filter_ok([.02],policy)
    assert not negative_filter_ok([-1e-18],negative_filter({'windows':[1]}))
    b=book();old_ids=set(b.sets);cost=b.cost_pct
    try:b.load({'makerFeeRate':.01,'indicationConfigs':[{'lastNLanes':[{'id':'bad','negativeFilter':{'windows':[0]}}]}]})
    except ValueError:pass
    else:raise AssertionError('invalid update accepted')
    assert set(b.sets)==old_ids and b.cost_pct==cost,'invalid policy must be atomic'
    return True,'strict Base count, own negative evidence, unknown/tiny loss and atomic invalid edit'

CHECKS=[('policy.native-lanes',separate_native_lanes),('policy.native-symbol-replay',scoped_replay),
        ('policy.edit-and-floor',edits_cold_and_floor_consistent),('policy.reject-invalid',invalid_policy_refused),
        ('policy.full-base-negative',full_base_and_negative_windows)]

def low_windows_survive_global_and_range():
    from position_cost import overall_validation_n
    for n in (1,2,3,5,8,10,200):
        b=SetBook();b.load(overlay(setGateWindow=n,mainGateN=n,setGateShortN=0,
            lastNByRange={'micro':{'baseLong':n,'mainLong':n}}))
        assert b.gate_window==n and b.main_n==n and overall_validation_n({'setGateWindow':n})==n
        assert b.last_n_by_range['micro']['baseLong']==n and b.last_n_by_range['micro']['mainLong']==n
    return True,'1,2,3,5,8,10,200 preserved globally, in linked windows and per range'
CHECKS.append(('policy.low-global-and-range-windows',low_windows_survive_global_and_range))
