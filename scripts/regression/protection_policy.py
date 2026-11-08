"""Independent geometry and configuration isolation invariants for the shared initial SL policy."""
from common import overlay, lane_cfg, synth_bars
from position_cost import SL_RATIOS, sl_floor_frac, resolve_sl_tp, normalize_min_sl_distance_pct
from set_engine import SetBook, SetLane, bar_signal
from indication_config import source_name


def geometry_and_replay_parity():
    checked = 0
    for cost in (0, .11, .143, .176):
        for floor in (.1, .4, 1):
            b = SetBook()
            b.load(overlay(stratGeneral=True, stratIndications=False, stratShortRange=False,
                           positionCostPct=cost, minSlDistancePct=floor, setMinStep=2, setStepMax=2))
            for s in [x for x in b.by_idx if x.kind == 'base']:
                stop, target = b.lane_fracs(s)
                expected = max(.002 * s.sl_ratio, (cost + floor) / 100)
                assert abs(stop - expected) < 1e-12 and abs(target - .002) < 1e-12
                for direction in (1, -1):
                    cfg = lane_cfg(cost_pct=cost, min_sl_distance_pct=floor)
                    row = [100., 100., 100., 100., 1.]
                    a, c = SetLane(s, 'S'), SetLane(s, 'S')
                    a.step(0, row, direction, 1, True, cfg)
                    c.scan(0, 1, [row], [direction], [1.], True, cfg)
                    assert abs(a.open['sl'] - (100 - direction * expected * 100)) < 1e-10
                    assert a.open['sl'] == c.open['sl'] and a.open['tp'] == c.open['tp']
                    # Raising the floor for new entries must not reprice an already held position.
                    initial = (a.open['sl'], a.open['tp'])
                    a.step(1, row, direction, 1, True, {**cfg, 'min_sl_distance_pct': 1})
                    assert a.open and (a.open['sl'], a.open['tp']) == initial
                    checked += 1
    return True, f'{checked} side × ratio × cost × floor cases: grid, step and scan match; held levels frozen'


def resolve_never_moves_target():
    n = 0
    for bind in (True, False):
        for r in SL_RATIOS:
            sl, tp, _ = resolve_sl_tp(base_sl=.0005, base_tp=.002, sl_min=0, sl_max=.1,
                tp_min=0, tp_max=.1, cost_pct=.176, min_sl_distance_pct=.4, sl_to_tp=r, bind_sl_to_tp=bind)
            assert abs(tp - .002) < 1e-12 and sl >= .00576 - 1e-12
            n += 1
    a,b=SetBook(),SetBook()
    a.load(overlay(minSlDistancePct=.1,positionCostPct=.11));before=a.lane_fracs(a.by_idx[0])
    b.load(overlay(minSlDistancePct=1,positionCostPct=.176,slMinSteps=5,trailMinSteps=7))
    assert a.lane_fracs(a.by_idx[0])==before
    assert [normalize_min_sl_distance_pct(x) for x in (None,-1,9,float('nan'))]==[.1,.1,1,.1]
    return True, f'{n} bound/unbound SL cells retain TP; separate books cannot mutate each other'


def duplicate_signal_configs_are_independent():
    entries=[dict(label='major',names=['donch_n8_t1@T5@B1n8'],stepMin=2,stepMax=3,slRatios=[.25,.5],types=['base']),
             dict(label='minor',names=['donch_n8_t1@T5@B1n8'],stepMin=4,stepMax=4,slRatios=[1,2.5],types=['base'])]
    def build(es):
        b=SetBook();b.load(overlay(stratGeneral=False,stratIndications=False,stratShortRange=False,indicationConfigs=es));return b
    a,b=build(entries),build(list(reversed(entries)))
    assert len(a.packs)==2 and len(a.by_idx)==6
    assert {x.id for x in a.by_idx}=={x.id for x in b.by_idx}, 'editor ordering changed lane identities'
    rows=synth_bars(12,1000,drift=.0005)
    for i in range(900,1000):
        votes=bar_signal(rows,i,a.packs,a.ind_settings,1000+i*60)
        assert len(votes)==2 and len(set(votes.values()))==1, 'aliases changed their source signal'
    assert len({source_name(p) for p in a.packs})==1
    a.by_idx[0].hist.append({'test':'own'})
    assert not a.by_idx[-1].hist, 'history shared between configs'
    return True, 'same source with Trend+Break: two config lanes, six sets, stable reorder identity, independent histories'


def requested_step_ceiling_is_authoritative():
    b=SetBook();b.load(overlay(setMinStep=2,setStepMax=7,tpRanges={"wide":True}))
    assert b.steps == list(range(2,8))
    assert all(s.step<=7 for s in b.by_idx if s.kind in ('base','exit'))
    return True, 'enabling Wide does not override the explicitly configured maximum step 7'

CHECKS = [
 ('protection.requested-step-ceiling',requested_step_ceiling_is_authoritative),
 ('protection.geometry-grid-step-scan',geometry_and_replay_parity),
 ('protection.target-and-instance-isolation',resolve_never_moves_target),
 ('protection.indication-config-lane-isolation',duplicate_signal_configs_are_independent),
]
