"""Functional suite: the Set grid (every combination is a Set) and the cost and price units."""
from __future__ import annotations

from common import COST, Skip, overlay, lane_cfg, PULSE  # noqa: F401  (PULSE sets sys.path)
from position_cost import (
    SL_RATIOS, TP_STEP_DEFAULT_MAX, TP_STEP_MAX, TP_STEP_MIN, TP_UNIT_PCT, TRAIL_STEP_DEFAULT, TRAIL_STEP_MAX, TRAIL_STEP_SETTING_MAX,
    TRAIL_STEP_SETTING_MIN, cost_as_frac, net_pnl_pct, snap_sl_ratio, trail_floor_steps,
)
from risk_variants import trail_grid
from set_engine import SetBook, SetLane, make_set_id, step_tp_pct


def _book(**extra):
    b = SetBook()
    b.load(overlay(**extra))
    return b


def tp_steps():
    steps = list(range(TP_STEP_MIN, TP_STEP_DEFAULT_MAX + 1))
    tp = [round(k * TP_UNIT_PCT, 6) for k in steps]
    ok = steps[0] == 2 and steps[-1] == 32 and len(steps) == 31 and tp[0] == 0.2 and tp[-1] == 3.2 and TP_STEP_MAX == 50      # shipped grid 0.2-3.2 %, ceiling 5.0 %
    ok = ok and all(abs((tp[i + 1] - tp[i]) - 0.1) < 1e-9 for i in range(len(tp) - 1))
    return ok, f"steps={steps[0]}..{steps[-1]} n={len(steps)}; TP {tp[0]}%..{tp[-1]}% in 0.1% steps (absolute, not PositionCost multiples)"


def sl_ratios():
    ok = len(SL_RATIOS) == 13 and SL_RATIOS[0] == 0.5 and SL_RATIOS[-1] == 3.5
    ok = ok and all(abs((SL_RATIOS[i + 1] - SL_RATIOS[i]) - 0.25) < 1e-9 for i in range(12))
    return ok, f"n={len(SL_RATIOS)} {SL_RATIOS[0]}..{SL_RATIOS[-1]}"


def default_grid_counts():
    """The default grid against the rules that build it, not against a remembered number: every ratio x every step for each full pack, the distinct stop
    distances for the Exit Sets, the trailing pairs above the minimum trailing distance, and Short Range's own steps 2-6."""
    sb = _book()
    kinds = {}
    for st in sb.by_idx:
        kinds[st.kind] = kinds.get(st.kind, 0) + 1
    steps, ratios = list(range(TP_STEP_MIN, TP_STEP_DEFAULT_MAX + 1)), list(SL_RATIOS)
    short = list(range(2, 7))
    give_floor = trail_floor_steps() * sb.cost_pct
    m = sb.trail_min_step
    pairs = [(k, j) for k in range(m, TRAIL_STEP_MAX + 1) for j in range(m, k + 1) if j * TP_UNIT_PCT + 1e-9 >= give_floor]
    short_pairs = [1 for k, j in pairs if k * TP_UNIT_PCT <= short[-1] * TP_UNIT_PCT + 1e-9]
    full_base, full_exit = len(ratios) * len(steps), len({round(r * k, 6) for r in ratios for k in steps})
    short_base, short_exit = len(ratios) * len(short), len({round(r * k, 6) for r in ratios for k in short})
    want = {"base": 2 * full_base + short_base, "exit": 2 * full_exit + short_exit, "trail": 2 * len(pairs) + len(short_pairs)}
    ok = (len(sb.packs) == 3 and all(kinds.get(k) == v for k, v in want.items()) and len(sb.by_idx) == sum(want.values())
          and sb.steps == steps and len(sb.sl_ratios) == 13)
    return ok, f"sets={len(sb.by_idx)} base={kinds.get('base')} exit={kinds.get('exit')} trail={kinds.get('trail')} packs={len(sb.packs)}; rules want {want}"


def ids_unique():
    sb = _book()
    ids = [st.id for st in sb.by_idx]
    idx = [st.idx for st in sb.by_idx]
    ok = len(set(ids)) == len(ids) and idx == list(range(len(idx)))
    return ok, f"unique={len(set(ids))} of {len(ids)} indexes_contiguous={idx == list(range(len(idx)))}"


def trail_rules_default():
    sb = _book()
    trails = [st for st in sb.by_idx if st.kind == "trail"]
    give_floor = trail_floor_steps() * COST
    min_arm = min(st.trail_arm for st in trails)
    bad_give = [st.id for st in trails if st.trail_give > st.trail_arm + 1e-9]
    under_floor = [st.id for st in trails if st.trail_give + 1e-9 < give_floor]
    ok = (not bad_give and not under_floor and min_arm >= give_floor - 1e-9
          and abs(max(st.trail_arm for st in trails) - TRAIL_STEP_MAX * TP_UNIT_PCT) < 1e-9)
    return ok, f"min_arm={min_arm:.3f}% (floor {give_floor:.2f}%) max_arm={max(st.trail_arm for st in trails):.2f}% give_over_arm={len(bad_give)} under_floor={len(under_floor)}"


def trail_min_step_clamp():
    """trailMinStep is in grid steps of 0.1 %; the overall minimum trailing distance (PositionCost + trailMinSteps steps, in percent) lifts it."""
    import math
    lo = _book(trailMinStep=1, trailMinSteps=1)       # setting floor 2, but PositionCost + 1 step = 0.24 % = 3 grid steps
    floored = _book(trailMinStep=1)                   # default trailMinSteps=2: PositionCost + 2 steps = 0.36 % = 4 grid steps
    hi = _book(trailMinStep=99)
    want_lo = max(TRAIL_STEP_SETTING_MIN, math.ceil(trail_floor_steps(1) * COST / TP_UNIT_PCT - 1e-9))
    want_fl = max(TRAIL_STEP_SETTING_MIN, math.ceil(trail_floor_steps() * COST / TP_UNIT_PCT - 1e-9))
    ok = (lo.trail_min_step == want_lo and floored.trail_min_step == want_fl and hi.trail_min_step == TRAIL_STEP_SETTING_MAX
          and abs(min(st.trail_arm for st in lo.by_idx if st.kind == "trail") - want_lo * TP_UNIT_PCT) < 1e-9
          and abs(min(st.trail_arm for st in floored.by_idx if st.kind == "trail") - want_fl * TP_UNIT_PCT) < 1e-9)
    return ok, f"clamp(1, 1 step)={lo.trail_min_step} default floor={floored.trail_min_step} clamp(99)={hi.trail_min_step} default setting={TRAIL_STEP_DEFAULT}"


def trail_grid_pure():
    g = trail_grid(3, COST)
    ok = len(g) == 210 and all(give <= arm + 1e-9 and give >= 3 * TP_UNIT_PCT - 1e-9 for _, arm, give in g)
    ok = ok and len({k for k, _, _ in g}) == 210
    ok = ok and trail_grid(3, 0.5) == g                  # the grid is absolute: PositionCost does not move it
    return ok, f"n={len(g)} unique_keys={len({k for k, _, _ in g})}; the same at another PositionCost"


def trail_distance_limit():
    """Every built Set's arm, give and initial stop fits the 3% price-distance limit (the grid's arms end at 2.2 %, so the stop is what binds)."""
    sb = _book(positionCostPct=0.15)
    trails = [st for st in sb.by_idx if st.kind == "trail"]
    ok = bool(trails) and all(st.trail_arm <= 3.0 + 1e-9 and st.trail_give <= 3.0 + 1e-9
                              and sb.lane_fracs(st)[0] <= 0.03 + 1e-12 for st in trails)
    return ok, f"trails={len(trails)} max_arm={max(st.trail_arm for st in trails):.2f}%"


def trail_limit_has_no_unsafe_fallback():
    """An impossible cost floor or initial stop removes trailing Sets instead of inventing a fallback."""
    floor = _book(positionCostPct=1.1, trailMinSteps=2)      # minimum trailing distance 3.3 % > the 3 % limit: nothing can trail
    stop = _book(positionCostPct=0.8, slMinSteps=3, trailMinSteps=1, stratShortRange=False,
                 trailVariants=["2.0:1.9"])                   # give floor 1.6 % is met, but the minimum stop (1 + 3) x 0.8 = 3.2 % is over the limit
    edge = _book(positionCostPct=1.0, slMinSteps=2, trailMinSteps=1, trailMinStep=2,
                 setMinStep=2, setStepMax=2, stratShortRange=False)
    above = _book(positionCostPct=1.00001, slMinSteps=2, trailMinSteps=1, trailMinStep=2,
                  setMinStep=2, setStepMax=2, stratShortRange=False)
    count = lambda b: sum(st.kind == "trail" for st in b.by_idx)
    ok = count(floor) == 0 and count(stop) == 0 and count(edge) > 0 and count(above) == 0
    ok = ok and not floor.coverage()["trails"] and floor.coverage()["dims"]["trail"] == 0
    return ok, f"floor={count(floor)} initial_stop={count(stop)} at_limit={count(edge)} over_limit={count(above)}"


def trail_limit_matches_replay_levels():
    """A limit-bound trailing Set opens at the same 3% stop in the two replay paths, for both sides."""
    b = _book(positionCostPct=1.0, slMinSteps=2, trailMinSteps=1, trailMinStep=2,
              setMinStep=2, setStepMax=2, stratShortRange=False)
    st = next(s for s in b.by_idx if s.kind == "trail")
    cfg = lane_cfg(cost_pct=1.0, sl_floor_steps=2)
    bar = [100.0, 100.0, 100.0, 100.0, 1.0]
    levels = []
    for direction in (1, -1):
        step, scan = SetLane(st, "S"), SetLane(st, "S")
        step.step(0, bar, direction, 1.0, True, cfg)
        scan.scan(0, 1, [bar], [direction], [1.0], True, cfg)
        expected = 100.0 * (1 - direction * b.lane_fracs(st)[0])
        levels.append((step.open["sl"], scan.open["sl"], expected))
    ok = all(abs(a - e) < 1e-9 and abs(c - e) < 1e-9 for a, c, e in levels)
    return ok, f"long/short step, scan, expected={levels}"


def setstep_clamp():
    lo = _book(setMinStep=0)
    hi = _book(setStepMax=99)
    small = _book(setMinStep=9, setStepMax=3)
    ok = lo.steps[0] == 2 and hi.steps[-1] == TP_STEP_MAX == 50 and small.steps and small.steps[0] <= small.steps[-1]
    return ok, f"min0->{lo.steps[0]} max99->{hi.steps[-1]} min9max3->{small.steps}"


def tp_is_k_tenths_of_a_percent():
    """TP = k x 0.1 % of price whatever PositionCost is; the lane lifts it to PositionCost, never above the step by a multiple."""
    bad = [k for k in range(2, 33) if abs(step_tp_pct(k, COST) - k * 0.001) > 1e-12 or abs(step_tp_pct(k, 0.5) - k * 0.001) > 1e-12]
    b = _book(positionCostPct=0.3, setMinStep=2, setStepMax=3, setSlRatios=[1.0], stratShortRange=False, stratGeneral=False)
    st = next(x for x in b.by_idx if x.kind == "base" and x.step == 2)
    sl_f, tp_f = b.lane_fracs(st)
    ok = not bad and abs(st.tp_pct - 0.002) < 1e-12 and abs(tp_f - 0.003) < 1e-12      # 0.2 % step, PositionCost 0.3 % lifts the lane's TP to 0.3 %
    return ok, f"k=2..32 bad={bad}; step 2 at PositionCost 0.3: set TP {st.tp_pct * 100:.2f}%, lane TP {tp_f * 100:.2f}%"


def cost_units():
    pairs = [
        ("cost_as_frac(0.15)", abs(cost_as_frac(0.15) - 0.0015) < 1e-12),
        ("cost_as_frac(0.04)", abs(cost_as_frac(0.04) - 0.0004) < 1e-12),
        ("net(+0.4%)", abs(net_pnl_pct(0.004, 0.15) - 0.0025) < 1e-12),
        ("net(0)", abs(net_pnl_pct(0.0, 0.15) + 0.0015) < 1e-12),
    ]
    return all(v for _, v in pairs), " ".join(f"{k}={'ok' if v else 'NO'}" for k, v in pairs)


def sl_snap():
    cases = [(0.6, 0.5), (0.8, 0.75), (9.0, 3.5), (-1.0, 0.5), (1.0, 1.0)]
    bad = [(x, snap_sl_ratio(x), want) for x, want in cases if abs(snap_sl_ratio(x) - want) > 1e-9]
    return not bad, f"bad={bad}" if bad else f"{len(cases)} cases"


def set_id_format():
    ok = make_set_id("general", 0.75, "", 2) == "general:1m:sl0.75:st2"
    ok = ok and make_set_id("indications", 3.5, "", 30) == "indications:1m:sl3.50:st30"
    return ok, make_set_id("general", 0.75, "", 2)


CHECKS = [
    ("grid.tp-steps-2-32", tp_steps),
    ("grid.sl-ratios-0.5-3.5", sl_ratios),
    ("grid.default-grid-follows-its-rules", default_grid_counts),
    ("grid.ids-unique-contiguous", ids_unique),
    ("grid.trail-give-le-arm-and-floor", trail_rules_default),
    ("grid.trail-min-step-clamp", trail_min_step_clamp),
    ("grid.trail-grid-pure", trail_grid_pure),
    ("grid.trail-distance-limit", trail_distance_limit),
    ("grid.trail-limit-no-unsafe-fallback", trail_limit_has_no_unsafe_fallback),
    ("grid.trail-limit-replay-levels", trail_limit_matches_replay_levels),
    ("grid.setstep-clamp", setstep_clamp),
    ("units.tp-is-k-tenths-of-a-percent", tp_is_k_tenths_of_a_percent),
    ("units.cost-and-net", cost_units),
    ("units.sl-snap", sl_snap),
    ("units.set-id-format", set_id_format),
]
