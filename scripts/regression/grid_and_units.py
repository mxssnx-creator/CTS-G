"""Functional suite: the Set grid (every combination is a Set) and the cost and price units."""
from __future__ import annotations

from common import COST, Skip, overlay, lane_cfg, PULSE  # noqa: F401  (PULSE sets sys.path)
from position_cost import (
    SL_RATIOS, TP_STEP_DEFAULT_MAX, TP_STEP_MAX, TP_STEP_MIN, TP_UNIT_PCT, TRAIL_STEP_DEFAULT, TRAIL_STEP_MAX, TRAIL_STEP_SETTING_MAX,
    TRAIL_STEP_SETTING_MIN, cost_as_frac, net_pnl_pct, snap_sl_ratio, trail_floor_steps,
)
from risk_variants import trail_grid
from set_engine import SetBook, SetLane, lane_fracs, make_set_id, make_trail_id, sl_floor_frac, step_tp_pct


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
    hi = _book(setStepMax=99)                              # Wide off (the default): the steps above 3.2 % belong to Wide, so the grid ends at 32
    hi_wide = _book(setStepMax=99, tpRanges={"wide": True})   # Wide on: up to the ceiling, 5.0 %
    small = _book(setMinStep=9, setStepMax=3)
    ok = (lo.steps[0] == 2 and hi.steps[-1] == TP_STEP_DEFAULT_MAX == 32 and hi_wide.steps[-1] == TP_STEP_MAX == 50
          and small.steps and small.steps[0] <= small.steps[-1])
    return ok, f"min0->{lo.steps[0]} max99->{hi.steps[-1]} (Wide on {hi_wide.steps[-1]}) min9max3->{small.steps}"


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


def _sig(b, pred=lambda st: True):
    """(id, kind, stop, target, arm, give) of every Set the predicate keeps: a Set's whole identity and its exits."""
    return {st.id: (st.kind, round(lane_fracs(st, b.cost_pct)[0], 12), round(lane_fracs(st, b.cost_pct)[1], 12), st.trail_arm, st.trail_give)
            for st in b.by_idx if pred(st)}


def tp_ranges_switch_only_their_own_steps():
    """Named TP ranges (Micro 2-3, Minimal 4-6, Short 7-12, Normal 13-20, Long 21-32, Wide 33-50): switching one range off removes exactly its
    base and Exit Sets of the general packs and leaves every other Set (other steps, trailing Sets, Short Range) byte-identical; Wide on adds steps
    33..50 and nothing else; the default is Micro .. Long (the shipped grid 2..32)."""
    from position_cost import tp_range_of
    kw = dict(setStepMax=32, stratShortRange=True)
    ref = _book(**kw)
    off = _book(tpRanges={"short": False}, **kw)
    wide = _book(tpRanges={"wide": True}, **kw)
    general = lambda st: st.pack != "shortrange" and st.kind in ("base", "exit")
    r_sig, o_sig, w_sig = _sig(ref), _sig(off), _sig(wide)
    gone = set(r_sig) - set(o_sig)
    gone_ok = bool(gone) and all(general(ref.sets[i]) and tp_range_of(ref.sets[i].step) == "short" for i in gone) and not (set(o_sig) - set(r_sig))
    rest_ok = all(o_sig[i] == r_sig[i] for i in o_sig)
    added = set(w_sig) - set(r_sig)
    wide_ok = (bool(added) and all(general(wide.sets[i]) and 33 <= wide.sets[i].step <= 50 for i in added) and not (set(r_sig) - set(w_sig))
               and all(w_sig[i] == r_sig[i] for i in r_sig))
    default_ok = ref.steps == list(range(2, 33)) and wide.steps == list(range(2, 51)) and 7 not in off.steps and 12 not in off.steps and 13 in off.steps
    names = [tp_range_of(k) for k in (2, 3, 4, 6, 7, 12, 13, 20, 21, 32, 33, 50)]
    names_ok = names == ["micro", "micro", "minimal", "minimal", "short", "short", "normal", "normal", "long", "long", "wide", "wide"]
    ok = gone_ok and rest_ok and wide_ok and default_ok and names_ok
    return ok, (f"Short off removes {len(gone)} Sets (all Short-range base/Exit: {gone_ok}), the other {len(o_sig)} unchanged: {rest_ok}; Wide on adds {len(added)} "
                f"(steps 33-50 only, the rest unchanged: {wide_ok}); default steps 2..32: {default_ok}; names {names_ok}")


def low_sl_ratios_build_only_distinct_stops():
    """setSlLow adds SL ratios 0.2 / 0.3 / 0.4 x TP. Every low-ratio Set's stop is max(minimum stop, r x TP) and differs from the stop of every other Set
    at its pack, direction and step (a floored copy is never built); every Set of the shipped grid is unchanged; each low ratio builds Sets; ratios snap
    0.2 / 0.3 / 0.4 below 0.45."""
    kw = dict(setStepMax=32, stratShortRange=True)
    ref = _book(**kw)
    low = _book(setSlLow=True, **kw)
    r_sig, l_sig = _sig(ref), _sig(low)
    same = not (set(r_sig) - set(l_sig)) and all(l_sig[i] == r_sig[i] for i in r_sig)
    floor = sl_floor_frac(low.cost_pct, low.sl_min_steps)
    lows = [st for st in low.by_idx if st.kind == "base" and st.sl_ratio < 0.5]
    stop_ok = all(abs(lane_fracs(st, low.cost_pct)[0] - max(floor, step_tp_pct(st.step, low.cost_pct) * st.sl_ratio)) < 1e-12 for st in lows)
    by_cell = {}
    for st in low.by_idx:
        if st.kind == "base":
            by_cell.setdefault((st.pack, st.side, st.step), []).append((round(lane_fracs(st, low.cost_pct)[0], 12), st.sl_ratio))
    copies = [(k, v) for k, v in by_cell.items() for stop, r in v if r < 0.5 and sum(1 for s2, _ in v if s2 == stop) > 1]
    lows_x = [st for st in low.by_idx if st.kind == "exit" and st.sl_ratio < 0.5]
    exit_stops = [round(lane_fracs(st, low.cost_pct)[0], 12) for st in low.by_idx if st.kind == "exit" and st.pack == "general" and st.side == 0]
    exit_dup = len(exit_stops) - len(set(exit_stops))
    ref_exit = [round(lane_fracs(st, ref.cost_pct)[0], 12) for st in ref.by_idx if st.kind == "exit" and st.pack == "general" and st.side == 0]
    exit_ok = exit_dup == len(ref_exit) - len(set(ref_exit))     # low ratios add no Exit Set whose stop another Exit Set already has
    each = {r: sum(1 for st in lows if abs(st.sl_ratio - r) < 1e-9) for r in (0.2, 0.3, 0.4)}
    snap_ok = [snap_sl_ratio(x) for x in (0.2, 0.33, 0.36, 0.44, 0.45, 0.1)] == [0.2, 0.3, 0.4, 0.4, 0.5, 0.2]
    ok = same and stop_ok and not copies and all(each.values()) and exit_ok and bool(lows_x) and snap_ok
    return ok, (f"shipped Sets unchanged={same}; {len(lows)} low-ratio base Sets {each}, stops = max(floor, r x TP): {stop_ok}; floored copies {len(copies)}; "
                f"{len(lows_x)} low-ratio Exit Sets, no repeated stop added: {exit_ok}; snap {snap_ok}")


def trailing_families_are_their_own_sets():
    """trailSlRatios builds each arm / give pair again per ratio r with the initial stop at max(minimum stop, r x arm), as its own Set (":ts<r>"), and
    leaves the shipped trailing Sets (stop TRAIL_SL_RATIO x the mid-step TP) unchanged; trailStepMax raises the largest arm (3.0 % at 30); an unknown
    ratio is dropped; a family Set's lane uses the same stop in the replay lane (SetLane) and in lane_fracs (live)."""
    kw = dict(setStepMax=32)
    ref = _book(**kw)
    fam = _book(trailSlRatios=[1.0, 2.0, 0.77], **kw)
    wide = _book(trailStepMax=30, **kw)
    r_sig, f_sig = _sig(ref), _sig(fam)
    same = all(f_sig.get(i) == r_sig[i] for i in r_sig)
    floor = sl_floor_frac(fam.cost_pct, fam.sl_min_steps)
    fams = [st for st in fam.by_idx if st.kind == "trail" and st.trail_sl > 0]
    ratios = sorted({st.trail_sl for st in fams})
    stop_ok = all(abs(lane_fracs(st, fam.cost_pct)[0] - max(floor, st.trail_arm / 100.0 * st.trail_sl)) < 1e-12 for st in fams)
    lane_ok = all(abs(SetLane(st, "X").sl_base - st.trail_arm / 100.0 * st.trail_sl) < 1e-15 for st in fams)
    id_ok = all(st.id == make_trail_id(st.pack, st.trail_key, st.side, st.trail_sl) and st.id.endswith(f":ts{st.trail_sl:.2f}") for st in fams)
    limit_ok = all(st.trail_arm * st.trail_sl <= 3.0 + 1e-9 for st in fams)
    arms = max(st.trail_arm for st in wide.by_idx if st.kind == "trail")
    ok = same and ratios == [1.0, 2.0] and stop_ok and lane_ok and id_ok and limit_ok and abs(arms - 3.0) < 1e-9 and len(fams) > 0
    return ok, (f"shipped Sets unchanged={same}; families {ratios} ({len(fams)} Sets; 0.77 dropped), stop = max(floor, r x arm): {stop_ok}, replay lane the same: "
                f"{lane_ok}; ids {id_ok}; stop within the 3 % limit {limit_ok}; trailStepMax 30 -> largest arm {arms} %")


def trailing_ranges_and_an_entrys_thin_trailing_range():
    """trailRanges (the arm's step in the TP bands Micro .. Wide) removes only the trailing Sets of a range switched off; every other Set keeps its id and
    figures. An indicationConfigs entry's trailStepMin / trailStepMax builds only those arms for its packs (a thin trailing range), the other packs keep the
    whole grid; every trailing Set of every category (general, indications, Short Range, an extra pack, a config entry) is built in both directions."""
    from position_cost import tp_range_of
    kw = dict(setStepMax=32, stratShortRange=True, stratTrend=True)
    ref = _book(**kw)
    cut = _book(trailRanges={"long": False, "micro": False}, **kw)
    r_sig, c_sig = _sig(ref), _sig(cut)
    gone = [i for i in r_sig if i not in c_sig]
    gone_ok = bool(gone) and all(ref.sets[i].kind == "trail" and tp_range_of(round(ref.sets[i].trail_arm / TP_UNIT_PCT)) in ("long", "micro") for i in gone)
    kept_ok = all(c_sig[i] == r_sig[i] for i in c_sig)
    left = {tp_range_of(round(st.trail_arm / TP_UNIT_PCT)) for st in cut.by_idx if st.kind == "trail"}
    ent = _book(indicationConfigs=[{"families": ["donch"], "ranges": [8], "timeframes": [1], "stepMin": 3, "stepMax": 10, "trailStepMin": 4, "trailStepMax": 6}], **kw)
    xp = [p for p in ent.packs if p.startswith("extra.")]
    x_arms = sorted({st.trail_arm for st in ent.by_idx if st.kind == "trail" and st.pack in xp})
    g_arms = sorted({st.trail_arm for st in ent.by_idx if st.kind == "trail" and st.pack == "general"})
    plain = _book(indicationConfigs=[{"families": ["donch"], "ranges": [8], "timeframes": [1], "stepMin": 3, "stepMax": 10}], **kw)
    p_arms = sorted({st.trail_arm for st in plain.by_idx if st.kind == "trail" and st.pack in xp})
    cats = {}
    for st in ent.by_idx:
        if st.kind == "trail":
            cats.setdefault(st.pack, set()).add(st.side)
    every = all(cats.get(p) == ({1, -1} if ent.direction_split else {0}) for p in ent.packs)
    ok = (gone_ok and kept_ok and "long" not in left and "micro" not in left and x_arms == [0.4, 0.5, 0.6] and max(g_arms) >= 2.2
          and max(p_arms) == 1.0 and every)
    return ok, (f"ranges long+micro off: {len(gone)} trailing Sets removed, all of those ranges {gone_ok}, the rest unchanged {kept_ok}, left {sorted(left)}; "
                f"entry arms {x_arms} (general up to {max(g_arms)}, entry without a trailing range up to {max(p_arms)}); trailing in every pack and direction {every} ({len(cats)} packs)")


def long_and_wide_ranges_stop_at_sl_ratio_one_and_the_minimum_step_stays_two():
    """slMaxByRange (owner 2026-11-03): the shipped overlays build no base or Exit Set of the Long or Wide TP range above SL ratio 1.0, every other range
    and every trailing Set is unchanged, and {} builds every ratio again; the minimum TP step is 2 and step-adapt is off in every shipped overlay (on x02
    it had raised the minimum to 11), so losing live fills never move it."""
    import json as _j, os as _o
    from common import ROOT
    from position_cost import tp_range_of
    kw = dict(setStepMax=32, tpRanges={"wide": True})
    cap = _book(slMaxByRange={"long": 1.0, "wide": 1.0}, **kw)
    free = _book(**kw)          # the fixture pins {}
    over = [st.id for st in cap.by_idx if st.kind in ("base", "exit") and tp_range_of(st.step) in ("long", "wide") and st.sl_ratio > 1.0 + 1e-9]
    c_sig, f_sig = _sig(cap), _sig(free)
    gone = [i for i in f_sig if i not in c_sig]
    gone_ok = bool(gone) and all(free.sets[i].kind in ("base", "exit") and tp_range_of(free.sets[i].step) in ("long", "wide") and free.sets[i].sl_ratio > 1.0 for i in gone)
    kept_ok = all(c_sig[i] == f_sig[i] for i in c_sig)
    files = [_o.path.join(PULSE, f"overlay-{v}-{c}.json") for v in ("bingx", "bybit") for c in ("x01", "x02")] + \
            [_o.path.join(ROOT, "presets", f) for f in ("secure-minimal.json", "secure-minimal-expanded-research.json")]
    ship = {_o.path.basename(f): (lambda d: (d.get("slMaxByRange"), d.get("setMinStep"), d.get("setStepAdapt")))(_j.load(open(f))) for f in files}
    ship_ok = all(v == ({"long": 1.0, "wide": 1.0}, 2, False) for v in ship.values())
    plain = SetBook()
    plain.load({"histEnabled": True})
    eng_ok = plain.step_adapt is False and plain.sl_max_by_range == {"long": 1.0, "wide": 1.0} and plain.min_step == 2
    ok = not over and gone_ok and kept_ok and ship_ok and eng_ok
    return ok, (f"capped grid: {len(over)} Long/Wide Sets over 1.0, {len(gone)} removed (all of them Long/Wide over 1.0 {gone_ok}), the rest unchanged {kept_ok}; "
                f"shipped (slMaxByRange, setMinStep, setStepAdapt) {set(map(str, ship.values()))}; engine default adapt {plain.step_adapt} min step {plain.min_step}")


def shipped_overlays_switch_off_the_indications_exit_sets():
    """Measured 2026-11-03 (executed PF, three windows): indications Exit Sets off 1.529 / 0.908 / 0.983 against 1.36 / 0.879 / 0.953 shipped; every
    shipped overlay and preset carries exitSetsByPack {"indications": false} and keeps the other packs' Exit Sets."""
    import json as _j, os as _o
    from common import ROOT
    files = [_o.path.join(PULSE, f"overlay-{v}-{c}.json") for v in ("bingx", "bybit") for c in ("x01", "x02")] + \
            [_o.path.join(ROOT, "presets", f) for f in ("secure-minimal.json", "secure-minimal-expanded-research.json")]
    got = {_o.path.basename(f): _j.load(open(f)).get("exitSetsByPack") for f in files}
    ok = all(v == {"indications": False} for v in got.values())
    return ok, f"{got}"


def exit_sets_switch_per_pack():
    """exitSetsByPack {pack: false} removes exactly that pack's Exit Sets (no-TP Sets) and nothing else; missing packs keep theirs; exitIgnoreTp off still
    removes every Exit Set."""
    kw = dict(setStepMax=32, stratShortRange=True)
    ref, off, none_ = _book(**kw), _book(exitSetsByPack={"indications": False}, **kw), _book(exitIgnoreTp=False, **kw)
    r_sig, o_sig = _sig(ref), _sig(off)
    gone = set(r_sig) - set(o_sig)
    ok = (bool(gone) and all(ref.sets[i].kind == "exit" and ref.sets[i].pack == "indications" for i in gone) and not (set(o_sig) - set(r_sig))
          and all(o_sig[i] == r_sig[i] for i in o_sig) and any(st.kind == "exit" and st.pack == "general" for st in off.by_idx)
          and not any(st.kind == "exit" for st in none_.by_idx))
    return ok, f"indications Exit off removes {len(gone)} Sets (all its Exit Sets), the other {len(o_sig)} unchanged; exitIgnoreTp off leaves no Exit Set"


def trail_min_lock_removes_only_pairs_that_lock_nothing():
    """trailMinLockPct (owner 2026-11-05): a trailing pair whose arm minus give is under it builds no Set; every other Set keeps its id and tape key, and 0 (the default) is the earlier grid."""
    base = _book()
    off = _book(trailMinLockPct=0)
    lock = _book(trailMinLockPct=0.3)
    huge = _book(trailMinLockPct=99)                      # bounded at the 3 % distance limit: it cannot invent a pair
    def trails(b):
        return {st.id: (st.trail_arm, st.trail_give) for st in b.by_idx if st.kind == "trail"}
    tb, tl = trails(base), trails(lock)
    gone = {i for i in tb if i not in tl}
    ok = (trails(off) == tb and len(off.by_idx) == len(base.by_idx)              # 0 = unchanged
          and bool(gone) and all(tb[i][0] - tb[i][1] < 0.3 - 1e-9 for i in gone)    # only pairs under the lock leave
          and all(tl[i][0] - tl[i][1] >= 0.3 - 1e-9 for i in tl)                   # every kept pair locks at least 0.3 %
          and set(tl) <= set(tb) and all(tl[i] == tb[i] for i in tl)
          and len(lock.by_idx) == len(base.by_idx) - len(gone)                      # base / Exit Sets untouched
          and not any(st.kind == "trail" for st in huge.by_idx)                     # a lock beyond every pair leaves no trailing Set, nothing is invented
          and lock.trail_min_lock == 0.3 and off.trail_min_lock == 0.0)
    return ok, f"pairs {len(tb)} -> {len(tl)} at lock 0.3 % ({len(gone)} removed, all with arm - give < 0.3); 0 keeps {len(trails(off))}; 99 keeps {len(trails(huge))}"


def x01_runs_the_full_grid_the_replay_measured_best():
    """Owner 2026-11-05 asked to switch off the groups that read badly in isolation (Micro range, Short Range, five SL ratios, trailing pairs that lock under 0.2 %). The counterfactual
    replay of 2026-11-06 (C / B / A, executed positions, `trade_sim.py`) showed removing them LOWERS the result - pooled PF 0.701 on 464 orders (net -61.7 %) with them switched off
    against 0.895 on 538 orders (net -20.1 %) with all four back, and every window worse (PF C 1.07 / B 0.54 / A 0.38 against 1.14 / 0.77 / 0.59): a group that loses in isolation
    also takes signals that would otherwise go to worse Sets. The shipped x01 grid therefore keeps all of them: Micro range, Short Range, all 13 SL ratios and every trailing pair (only the Normal range stays off, owner 2026-11-04). The trend signal
    pack is OFF: the same counterfactual with trend on reads pooled PF 0.895 on 538 orders (net -20.1) against 1.032 on 516 (net +5.6) with it off, worse on B and A (0.77 / 0.59 against 0.97 / 0.98)."""
    import json as _j, os as _o
    from common import PULSE
    b = SetBook()
    b.load(_j.load(open(_o.path.join(PULSE, "overlay-bingx-x01.json"))))
    base = [st for st in b.by_idx if st.kind in ("base", "exit")]
    ratios = {round(float(st.sl_ratio), 2) for st in base}
    locks = [st.trail_arm - st.trail_give for st in b.by_idx if st.kind == "trail"]
    ok = (any(st.kind != "trail" and st.step <= 3 for st in b.by_idx) and {"shortrange", "general", "indications"} <= {st.pack for st in b.by_idx} and "trend" not in {st.pack for st in b.by_idx}
          and len(ratios) == 13 and locks and min(locks) < 0.1 and not any(13 <= st.step <= 20 for st in base if st.kind != "trail") and b.trail_min_lock == 0.0)
    return ok, f"x01 {len(b.by_idx)} Sets, packs {sorted({st.pack for st in b.by_idx})}, micro Sets {any(st.kind != 'trail' and st.step <= 3 for st in b.by_idx)}, SL ratios {len(ratios)}, smallest trailing lock {min(locks):.2f}, Normal-range Sets {sum(1 for st in base if 13 <= st.step <= 20)}"


CHECKS = [
    ("grid.tp-steps-2-32", tp_steps),
    ("grid.sl-ratios-0.5-3.5", sl_ratios),
    ("grid.default-grid-follows-its-rules", default_grid_counts),
    ("grid.ids-unique-contiguous", ids_unique),
    ("grid.trail-give-le-arm-and-floor", trail_rules_default),
    ("grid.trail-min-step-clamp", trail_min_step_clamp),
    ("grid.trail-grid-pure", trail_grid_pure),
    ("grid.x01-runs-the-full-grid-the-replay-measured-best", x01_runs_the_full_grid_the_replay_measured_best),
    ("grid.trail-min-lock-removes-only-pairs-that-lock-nothing", trail_min_lock_removes_only_pairs_that_lock_nothing),
    ("grid.trail-distance-limit", trail_distance_limit),
    ("grid.trail-limit-no-unsafe-fallback", trail_limit_has_no_unsafe_fallback),
    ("grid.trail-limit-replay-levels", trail_limit_matches_replay_levels),
    ("grid.setstep-clamp", setstep_clamp),
    ("units.tp-is-k-tenths-of-a-percent", tp_is_k_tenths_of_a_percent),
    ("units.cost-and-net", cost_units),
    ("units.sl-snap", sl_snap),
    ("units.set-id-format", set_id_format),
    ("grid.tp-ranges-switch-only-their-own-steps", tp_ranges_switch_only_their_own_steps),
    ("grid.low-sl-ratios-build-only-distinct-stops", low_sl_ratios_build_only_distinct_stops),
    ("grid.trailing-families-are-their-own-sets", trailing_families_are_their_own_sets),
    ("grid.trailing-ranges-and-an-entrys-thin-trailing-range", trailing_ranges_and_an_entrys_thin_trailing_range),
    ("grid.long-and-wide-ranges-stop-at-sl-ratio-one-and-the-minimum-step-stays-two", long_and_wide_ranges_stop_at_sl_ratio_one_and_the_minimum_step_stays_two),
    ("grid.exit-sets-switch-per-pack", exit_sets_switch_per_pack),
    ("grid.shipped-overlays-switch-off-the-indications-exit-sets", shipped_overlays_switch_off_the_indications_exit_sets),
]
