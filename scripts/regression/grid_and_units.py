"""Functional suite: the Set grid (every combination is a Set) and the cost and price units."""
from __future__ import annotations

from common import COST, Skip, overlay, PULSE  # noqa: F401  (PULSE sets sys.path)
from position_cost import (
    SL_RATIOS, TP_STEP_MAX, TP_STEP_MIN, TRAIL_STEP_DEFAULT, TRAIL_STEP_SETTING_MAX,
    TRAIL_STEP_SETTING_MIN, cost_as_frac, net_pnl_pct, snap_sl_ratio,
)
from risk_variants import trail_grid
from set_engine import SetBook, make_set_id, step_tp_pct


def _book(**extra):
    b = SetBook()
    b.load(overlay(**extra))
    return b


def tp_steps():
    steps = list(range(TP_STEP_MIN, TP_STEP_MAX + 1))
    ok = steps[0] == 2 and steps[-1] == 30 and len(steps) == 29
    return ok, f"steps={steps[0]}..{steps[-1]} n={len(steps)}"


def sl_ratios():
    ok = len(SL_RATIOS) == 13 and SL_RATIOS[0] == 0.5 and SL_RATIOS[-1] == 3.5
    ok = ok and all(abs((SL_RATIOS[i + 1] - SL_RATIOS[i]) - 0.25) < 1e-9 for i in range(12))
    return ok, f"n={len(SL_RATIOS)} {SL_RATIOS[0]}..{SL_RATIOS[-1]}"


def default_grid_counts():
    sb = _book()
    kinds = {}
    for st in sb.by_idx:
        kinds[st.kind] = kinds.get(st.kind, 0) + 1
    ok = (len(sb.packs) == 2 and kinds.get("base") == 754 and kinds.get("trail") == 420
          and len(sb.by_idx) == 1174 and sb.steps == list(range(2, 31)) and len(sb.sl_ratios) == 13)
    return ok, f"sets={len(sb.by_idx)} base={kinds.get('base')} trail={kinds.get('trail')} packs={len(sb.packs)}"


def ids_unique():
    sb = _book()
    ids = [st.id for st in sb.by_idx]
    idx = [st.idx for st in sb.by_idx]
    ok = len(set(ids)) == len(ids) and idx == list(range(len(idx)))
    return ok, f"unique={len(set(ids))} of {len(ids)} indexes_contiguous={idx == list(range(len(idx)))}"


def trail_rules_default():
    sb = _book()
    trails = [st for st in sb.by_idx if st.kind == "trail"]
    min_arm = min(st.trail_arm for st in trails)
    bad_give = [st.id for st in trails if st.trail_give > st.trail_arm + 1e-9]
    ok = not bad_give and min_arm >= 3 * COST - 1e-9 and abs(max(st.trail_arm for st in trails) - 22 * COST) < 1e-9
    return ok, f"min_arm={min_arm:.3f}% max_arm={max(st.trail_arm for st in trails):.2f}% give_over_arm={len(bad_give)}"


def trail_min_step_clamp():
    lo = _book(trailMinStep=1)
    hi = _book(trailMinStep=99)
    ok = (lo.trail_min_step == TRAIL_STEP_SETTING_MIN and hi.trail_min_step == TRAIL_STEP_SETTING_MAX
          and abs(min(st.trail_arm for st in lo.by_idx if st.kind == "trail") - 2 * COST) < 1e-9)
    return ok, f"clamp(1)={lo.trail_min_step} clamp(99)={hi.trail_min_step} default={TRAIL_STEP_DEFAULT}"


def trail_grid_pure():
    g = trail_grid(3, COST)
    ok = len(g) == 210 and all(give <= arm + 1e-9 and give >= 3 * COST - 1e-9 for _, arm, give in g)
    ok = ok and len({k for k, _, _ in g}) == 210
    return ok, f"n={len(g)} unique_keys={len({k for k, _, _ in g})}"


def setstep_clamp():
    lo = _book(setMinStep=0)
    hi = _book(setStepMax=99)
    small = _book(setMinStep=9, setStepMax=3)
    ok = lo.steps[0] == 2 and hi.steps[-1] == 30 and small.steps and small.steps[0] <= small.steps[-1]
    return ok, f"min0->{lo.steps[0]} max99->{hi.steps[-1]} min9max3->{small.steps}"


def tp_is_k_times_cost():
    bad = [k for k in range(2, 31) if abs(step_tp_pct(k, COST) - k * COST / 100.0) > 1e-12]
    net_min = (2 - 1) * COST
    ok = not bad and net_min > 0
    return ok, f"k=2..30 bad={bad} net_at_min_tp={net_min:.2f}%"


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
    ("grid.tp-steps-2-30", tp_steps),
    ("grid.sl-ratios-0.5-3.5", sl_ratios),
    ("grid.default-1174-sets", default_grid_counts),
    ("grid.ids-unique-contiguous", ids_unique),
    ("grid.trail-give-le-arm-min3", trail_rules_default),
    ("grid.trail-min-step-clamp", trail_min_step_clamp),
    ("grid.trail-grid-pure", trail_grid_pure),
    ("grid.setstep-clamp", setstep_clamp),
    ("units.tp-equals-k-times-cost", tp_is_k_times_cost),
    ("units.cost-and-net", cost_units),
    ("units.sl-snap", sl_snap),
    ("units.set-id-format", set_id_format),
]
