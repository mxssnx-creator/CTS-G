"""Independent geometry and configuration isolation invariants for the shared initial SL policy."""
from common import overlay, lane_cfg, synth_bars
from math import isclose
from pulse_trader import Position, Pulse, set_kind_of
from risk_variants import fixed_trail
from position_cost import SL_RATIOS, sl_floor_frac, resolve_sl_tp, normalize_min_sl_distance_pct
from set_engine import BarIndex, SetBook, SetLane, bar_signal, config_key, set_group
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


def trailing_distance_modes():
    settings = overlay(stratGeneral=True, stratIndications=False, stratShortRange=False,
                       directionMode="split", exitIgnoreTp=True, trailVariants=["0.9:0.5"])
    book = SetBook()
    book.load(settings)
    prior_indices = {st.id: st.idx for st in book.by_idx}
    book.load({**settings, "setTrailingDistance": "both"})
    assert all(book.sets[sid].idx == idx for sid, idx in prior_indices.items())
    trader = Pulse.__new__(Pulse)
    trader.sets = book
    trader.close_pos = lambda *_args: None
    trader.software_exit = lambda pos, kind, price, px: price >= px if pos.side == "LONG" else price <= px
    moves = []
    trader.replace_sl = lambda _pos, stop: moves.append(stop) or True
    fixed_orders = []
    cfg = lane_cfg(honor_tp=False, time_bars=120, scratch_bars=120)
    cases = 0
    for st in book.by_idx:
        if st.kind != "trail":
            continue
        direction = st.side
        peak = 100 + 10 * direction
        arm_bar = [100., max(100., peak), min(100., peak), peak, 1.]
        expected = peak - direction * (100. if fixed_trail(st.id) else peak) * st.trail_give / 100
        pullback = expected - direction * .01
        bars = [[100.]*4+[1.], arm_bar, [peak, max(peak, pullback), min(peak, pullback), pullback, 1.]]
        stream = SetLane(st, 'BTC-USDT')
        scalar = [closed for i, row in enumerate(bars) if (closed := stream.step(i, row, direction if i == 0 else 0, 1, True, cfg))]
        fast = SetLane(st, 'BTC-USDT').scan(0, len(bars), bars, [direction, 0, 0], [1., 0., 0.], True, cfg, acc=BarIndex(bars))
        assert len(scalar) == 1 and scalar == fast and isclose(scalar[0]['exit_px'], expected)
        sl, tp = book.lane_fracs(st)
        pos = Position('BTC-USDT', 'LONG' if direction > 0 else 'SHORT', 1., 100. + direction, 0.,
                       100. * (1 - direction * sl), 100. * (1 + direction * tp), peak,
                       base_entry=100., set_id=st.id, trail_set_id=st.id, trail_key=st.trail_key,
                       sl_pct=sl, tp_pct=tp, trail_arm=st.trail_arm/100, trail_give=st.trail_give/100)
        trader.manage_set_lane(pos, peak, 60., 60., .1)
        assert isclose(pos.trail, expected) and isclose(moves[-1], expected)
        assert set_kind_of(st.id) == "trail" and not trader.lane_honors_tp(pos)
        order = trader.cid(set_id=st.id, pack=st.pack, set_idx=st.idx, lane="neg")
        assert len(order) <= 32 and trader.parse_track(order)['set_id'] == st.id
        if fixed_trail(st.id):
            fixed_orders.append((order, st.id))
            assert set_group(st.id) != set_group(st.id.replace(":fixed", ""))
        cases += 1
    assert cases >= 4 and fixed_orders
    book.load(settings)
    for order, sid in fixed_orders:
        assert sid in book.retired and trader.parse_track(order)['set_id'] == sid
    book.load({**settings, "setTrailingDistance": "fixed"})
    assert all(fixed_trail(st.id) for st in book.by_idx if st.kind == "trail")
    return True, f'{cases} trailing Sets: fixed/dynamic × long/short, replay, stops, exact order identity and retired recovery'


def absolute_price_distance_grid():
    settings = overlay(stratGeneral=True, stratIndications=False, stratShortRange=False,
                       stratSignals=False, indicationConfigs=[], setStepAdapt=False, setMinStep=2, setStepMax=2,
                       tpStepMin=0, setPriceDistanceGrid=True, setTrailingDistance="both", trailMinStep=2,
                       trailStepMin=0, trailStepMax=2, trailMinLockPct=0, positionCostPct=.01,
                       histIgnoreTp=True, minSlDistancePct=.1, minSlSteps=0, minTrailSteps=0)
    book = SetBook()
    book.load(settings)
    sets = [st for st in book.by_idx if st.pack == "general"]
    base = [st for st in sets if st.kind == "base"]
    trail = [st for st in sets if st.kind == "trail" and isclose(st.trail_arm, 2.6)]
    gaps = {step / 10 for step in range(2, 27)}
    stops = {step / 10 for step in range(2, 23)}
    assert {st.price_sl_pct for st in base} == stops
    assert {(st.price_sl_pct, st.trail_give, fixed_trail(st.id)) for st in trail} == {
        (sl, gap, fixed) for sl in stops for gap in gaps for fixed in (False, True)
    }
    assert len({config_key(st.id) for st in trail}) == 21 * 25 * 2
    trader = Pulse.__new__(Pulse)
    trader.sets = book
    for st in (base[0], next(st for st in sets if st.kind == "exit"), trail[0], trail[-1]):
        assert isclose(book.lane_fracs(st)[0], st.price_sl_pct / 100)
        assert isclose(SetLane(st, "BTC-USDT").sl_base, st.price_sl_pct / 100)
        assert set_kind_of(st.id) == st.kind
        order = trader.cid(set_id=st.id, pack=st.pack, set_idx=st.idx, lane="neg")
        assert len(order) <= 32 and trader.parse_track(order)["set_id"] == st.id
    assert any(":trail-fixed:" in set_group(st.id) for st in trail)
    book.load({**settings, "setPriceSlDistances": [.2, 1.4], "setPriceTrailGaps": [.2, .4],
               "positionCostPct": .176, "minSlDistancePct": .4})
    assert {st.price_sl_pct for st in book.by_idx} == {1.4}
    assert all(st.trail_give in (.2, .4) for st in book.by_idx if st.kind == "trail")
    return True, f"{len(trail)} distinct trailing Sets, exact protection and compact order ownership"


CHECKS = [
 ('protection.requested-step-ceiling',requested_step_ceiling_is_authoritative),
 ('protection.geometry-grid-step-scan',geometry_and_replay_parity),
 ('protection.target-and-instance-isolation',resolve_never_moves_target),
 ('protection.indication-config-lane-isolation',duplicate_signal_configs_are_independent),
    ('protection.trailing-distance-modes', trailing_distance_modes),
    ('protection.absolute-price-grid', absolute_price_distance_grid),
]
