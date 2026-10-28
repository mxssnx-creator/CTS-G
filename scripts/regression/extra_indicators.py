"""Extra indicator suite: hand-computed values, windowing, output domain, signal fixtures and the engine wiring.

The fixtures are built so that one indicator must fire in one direction; the mirrored fixture must fire the other
way, so a check cannot pass by returning a constant.
"""
from __future__ import annotations

from common import SMALL, T0, overlay, synth_bars  # noqa: F401
import indication_extra as ix
from set_engine import SetBook, indication_signal


def _rows(closes, vol=1.0, spread=0.05):
    out, prev = [], closes[0]
    for c in closes:
        o = prev
        out.append([o, max(o, c) + spread, min(o, c) - spread, c, vol])
        prev = c
    return out


def hand_computed_rsi_and_atr():
    rs = ix._rsi_series([10.0, 12.0, 11.0, 13.0], 2)
    rows = [[10, 11, 9, 10, 1], [10, 12, 10, 11, 1], [11, 11.5, 10.5, 11, 1]]
    tr = ix._true_ranges(rows)            # 2, max(2, |12-10|, |10-10|)=2, max(1, |11.5-11|, |10.5-11|)=1
    ok = (abs(rs[2] - 66.6667) < 1e-3 and abs(rs[3] - 85.7143) < 1e-3 and tr == [2.0, 2.0, 1.0]
          and abs(ix._atr(rows, 3) - 5.0 / 3.0) < 1e-12)
    return ok, f"rsi2={rs[2]:.4f},{rs[3]:.4f} tr={tr} atr={ix._atr(rows, 3):.4f}"


def only_the_tail_is_read():
    bars = synth_bars(11, 900, drift=0.0003)
    bad = []
    for name, fn in ix.INDICATORS.items():
        w = ix.window_of(name)
        for m in range(w + 5, 900, 37):
            if fn(bars[:m]) != fn(bars[m - w : m]):
                bad.append((name, m))
                break
    return not bad, f"indicators={len(ix.INDICATORS)} reading beyond the window: {bad}"


def outputs_stay_in_their_domain():
    bad, fired = [], {}
    for seed in (3, 4, 5):
        bars = synth_bars(seed, 700, drift=0.0005 if seed % 2 else -0.0005)
        for i in range(120, 700, 3):
            for name, fn in ix.INDICATORS.items():
                s = fn(bars[max(0, i - max(150, ix.window_of(name) - 1)) : i + 1])
                if s is None:
                    continue
                fired[name] = fired.get(name, 0) + 1
                if s[0] not in (1, -1) or not (0.5 <= s[1] <= 0.95):
                    bad.append((name, s))
    return not bad, f"fired={sum(fired.values())} across {len(fired)} indicators; out of domain={bad[:3]}"


def fixtures_fire_both_ways():
    flat = [100.0] * 40
    res = {}
    up = _rows(flat + [100.0 + 0.8])
    for r in up[:-1]:
        r[4] = 1.0
    up[-1][4] = 3.0
    dn = _rows(flat + [100.0 - 0.8])
    for r in dn[:-1]:
        r[4] = 1.0
    dn[-1][4] = 3.0
    res["donchian_break"] = (ix.donchian_break(up), ix.donchian_break(dn))
    base = [100.0 + 0.02 * i for i in range(70)]
    drop = [base[-1] - 0.15, base[-1] - 0.40]
    res["rsi2_reversion"] = (ix.rsi2_reversion(_rows(base + drop, 0.01)),
                             ix.rsi2_reversion(_rows([300.0 - x for x in base + drop], 0.01)))
    # engulfing: three down bars, then a large bar that engulfs
    d3 = [[100.4, 100.45, 100.0, 100.1, 1], [100.1, 100.15, 99.8, 99.85, 1], [99.85, 99.9, 99.5, 99.55, 1]]
    lead = _rows([100.0 + 0.0 * i for i in range(30)])
    long_fx = lead + d3 + [[99.4, 100.6, 99.35, 100.55, 2]]
    u3 = [[99.6, 99.9, 99.55, 99.85, 1], [99.85, 100.2, 99.8, 100.15, 1], [100.15, 100.5, 100.1, 100.45, 1]]
    short_fx = lead + u3 + [[100.6, 100.65, 99.4, 99.45, 2]]
    res["engulfing"] = (ix.engulfing(long_fx), ix.engulfing(short_fx))
    # streak: six rising closes on falling volume -> fade short; six falling -> long
    rise = _rows([100 + 0.1 * i for i in range(30)] + [103 + 0.1 * i for i in range(7)])
    for k, r in enumerate(rise[-6:]):
        r[4] = 6.0 - k
    fall = _rows([100 - 0.1 * i for i in range(30)] + [97 - 0.1 * i for i in range(7)])
    for k, r in enumerate(fall[-6:]):
        r[4] = 6.0 - k
    res["streak_exhaustion"] = (ix.streak_exhaustion(fall), ix.streak_exhaustion(rise))
    bad = {k: v for k, v in res.items() if not (v[0] and v[0][0] == 1 and v[1] and v[1][0] == -1)}
    return not bad, f"checked={list(res)} wrong={ {k: bad[k] for k in bad} }"


def a_failing_indicator_is_isolated():
    saved = dict(ix.INDICATORS)
    try:
        ix.INDICATORS["boom"] = lambda rows: 1 / 0
        ix.INDICATORS["yes"] = lambda rows: (1, 0.7)
        out = ix.extra_votes([[1, 1, 1, 1, 1]] * 5, ["boom", "yes", "unknown"])
    finally:
        ix.INDICATORS.clear()
        ix.INDICATORS.update(saved)
    return out == [(1, 0.7, "yes")], f"votes={out}"


def the_overlay_list_reaches_the_vote_and_default_is_off():
    b = SetBook()
    b.load(overlay())
    off = b.ind_settings["extraIndicators"]
    b.load(overlay(extraIndicators=["donchian_break", "nope", "donchian_break"]))
    on = b.ind_settings["extraIndicators"]
    flat = _rows([100.0] * 120)
    flat[-1] = [100.0, 100.9, 100.0, 100.85, 3.0]
    s_on = indication_signal(flat, dict(b.ind_settings), T0)
    s_off = indication_signal(flat, dict(b.ind_settings, extraIndicators=[]), T0)
    ok = off == [] and on == ["donchian_break"] and "donchian_break" in s_on[2] and "donchian_break" not in s_off[2]
    return ok, f"default={off} list={on} on={s_on} off={s_off}"


CHECKS = [
    ("extra.hand-computed-rsi-and-atr", hand_computed_rsi_and_atr),
    ("extra.only-the-tail-is-read", only_the_tail_is_read),
    ("extra.outputs-stay-in-domain", outputs_stay_in_their_domain),
    ("extra.fixtures-fire-both-ways", fixtures_fire_both_ways),
    ("extra.failing-indicator-isolated", a_failing_indicator_is_isolated),
    ("extra.overlay-list-reaches-vote", the_overlay_list_reaches_the_vote_and_default_is_off),
]


def _short_book(**extra):
    b = SetBook()
    b.load(overlay(stratShortRange=True, stratIndications=False, stratGeneral=False, **extra))
    return b


def short_range_grid_is_steps_2_to_6():
    from set_engine import SHORT_PACK
    b = _short_book()
    base = [s for s in b.by_idx if s.pack == SHORT_PACK and s.kind == "base"]
    steps = sorted({s.step for s in base})
    ratios = sorted({s.sl_ratio for s in base})
    off = SetBook()
    off.load(overlay())
    wide = _short_book(shortRangeStepMin=3, shortRangeStepMax=5)
    ok = (steps == [2, 3, 4, 5, 6] and ratios == [0.5 + 0.25 * i for i in range(13)] and len(base) == 65
          and SHORT_PACK not in off.packs
          and sorted({s.step for s in wide.by_idx if s.kind == "base"}) == [3, 4, 5])
    return ok, f"steps={steps} ratios={len(ratios)} base Sets={len(base)} off by default={SHORT_PACK not in off.packs}"


def short_range_signal_cancels_opposite_votes():
    import indication_extra as ixm
    from set_engine import short_range_signal
    saved = dict(ixm.INDICATORS)
    try:
        ixm.INDICATORS["lr_fade"] = lambda r: (1, 0.7)
        ixm.INDICATORS["lr_break"] = lambda r: (-1, 0.65)
        ixm.INDICATORS["lr_nr7"] = lambda r: (1, 0.9)
        both = short_range_signal([[1, 1, 1, 1, 1]] * 3, {"shortRangeVariants": ["lr_fade", "lr_break"]})
        same = short_range_signal([[1, 1, 1, 1, 1]] * 3, {"shortRangeVariants": ["lr_fade", "lr_nr7"]})
        none = short_range_signal([[1, 1, 1, 1, 1]] * 3, {"shortRangeVariants": ["unknown"]})
    finally:
        ixm.INDICATORS.clear()
        ixm.INDICATORS.update(saved)
    ok = both[0] == 0 and same == (1, 0.9, "lr_fade+lr_nr7") and none[0] == 0
    return ok, f"opposite={both} same={same} none={none}"


def minimum_stop_and_trail_follow_cost_plus_steps():
    import position_cost as pc
    from set_engine import SetLane
    saved = dict(pc._MIN_STEPS)
    try:
        pc.configure_min_steps(1, 2)
        floor = pc.sl_floor_frac(0.12)
        sl, tp, _ = pc.resolve_sl_tp(base_sl=0.0, base_tp=0.0036, sl_min=0.0001, sl_max=0.05, tp_min=0.0001, tp_max=0.05,
                                      cost_pct=0.12, sl_to_tp=0.3, bind_sl_to_tp=True)
        pc.configure_min_steps(3, 4)
        floor3, trail3 = pc.sl_floor_frac(0.12), pc.trail_floor_steps()
        pc.configure_min_steps(99, 99)           # clamped, never unbounded
        clamped = dict(pc._MIN_STEPS)
    finally:
        pc._MIN_STEPS.clear()
        pc._MIN_STEPS.update(saved)
    ok = (abs(floor - 0.0024) < 1e-12 and abs(sl - 0.0024) < 1e-12 and abs(floor3 - 0.0048) < 1e-12 and trail3 == 5
          and clamped == {"sl": 5, "trail": 7})
    return ok, f"floor={floor} resolved sl={sl} (ratio 0.3 x TP 0.36% = 0.108% is under it) floor(3)={floor3} trail(4)={trail3} clamp={clamped}"


CHECKS += [
    ("extra.short-range-grid-steps-2-6", short_range_grid_is_steps_2_to_6),
    ("extra.short-range-signal-cancels-opposites", short_range_signal_cancels_opposite_votes),
    ("extra.minimum-stop-and-trail-follow-cost-plus-steps", minimum_stop_and_trail_follow_cost_plus_steps),
]


# ---- Low-Range family ---------------------------------------------------------------------------------------------


def _mk(closes, vol=1.0, sp=0.02):
    out, prev = [], closes[0]
    for c in closes:
        out.append([prev, max(prev, c) + sp, min(prev, c) - sp, c, vol])
        prev = c
    return out


def _tight_after_wide():
    wide = [100 + 0.6 * ((i % 10) - 5) / 5 for i in range(80)]
    tight = [100 + 0.15 * ((i % 10) - 5) / 5 for i in range(40)]
    return wide + tight


def low_range_state_hand_values():
    base = _tight_after_wide()
    rows = _mk(base + [99.76], sp=0.1)
    st = ix.low_range_state(rows, lag=1)
    box = rows[-25:-1]
    hi, lo = max(r[1] for r in box), min(r[2] for r in box)
    ok = (st is not None and abs(st["hi"] - hi) < 1e-9 and abs(st["lo"] - lo) < 1e-9
          and abs(st["width_pct"] - (hi - lo) / lo * 100.0) < 1e-9 and abs(st["pos"] - (99.76 - lo) / (hi - lo)) < 1e-9
          and 0.0 <= st["bw_rank"] <= 1.0 and ix.low_range_state(rows[:20]) is None)
    return ok, f"hi={st and st['hi']} lo={st and st['lo']} width%={st and round(st['width_pct'], 4)} pos={st and round(st['pos'], 3)}"


def low_range_fade_both_ways():
    base = _tight_after_wide()
    lo = min(r[2] for r in _mk(base, sp=0.1)[-24:])
    hi = max(r[1] for r in _mk(base, sp=0.1)[-24:])
    long_fx = _mk(base + [lo + 0.01], sp=0.1)
    short_fx = _mk([200 - x for x in base] + [200 - (lo + 0.01)], sp=0.1)
    mid = _mk(base + [100.0], sp=0.1)
    a, b, c = ix.lr_fade(long_fx), ix.lr_fade(short_fx), ix.lr_fade(mid)
    # a close that pokes through the box edge widens the bandwidth itself: the regime is read one bar back, so it still fades
    shrink = [100 + (0.4 - 0.3 * i / 120) * (((i % 10) - 5) / 5) for i in range(120)]    # ever tighter: the bandwidth is at its low
    lo2 = min(r[2] for r in _mk(shrink, sp=0.1)[-24:])
    poke = ix.lr_fade(_mk(shrink + [lo2 - 0.3], sp=0.1))
    return (a and a[0] == 1 and b and b[0] == -1 and c is None and poke and poke[0] == 1,
            f"edge long={a} edge short={b} mid={c} poke through={poke}")


def low_range_break_and_trendbreak_both_ways():
    up = [100 + 0.05 * i for i in range(90)]
    box = [up[-1] + 0.01 * ((-1) ** i) for i in range(30)]
    rows = _mk(up + box)
    brk_up = rows + [[box[-1], box[-1] + 0.5, box[-1] - 0.01, box[-1] + 0.45, 4.0]]
    brk_dn = rows + [[box[-1], box[-1] + 0.01, box[-1] - 0.5, box[-1] - 0.45, 4.0]]
    weak = rows + [[box[-1], box[-1] + 0.5, box[-1] - 0.01, box[-1] + 0.45, 0.5]]      # no volume: no break
    dtr = [200 - x for x in up]
    dbox = [200 - x for x in box]
    drows = _mk(dtr + dbox)
    d_dn = drows + [[dbox[-1], dbox[-1] + 0.01, dbox[-1] - 0.5, dbox[-1] - 0.45, 4.0]]
    d_up = drows + [[dbox[-1], dbox[-1] + 0.5, dbox[-1] - 0.01, dbox[-1] + 0.45, 4.0]]
    r = {"break up": ix.lr_break(brk_up), "break down": ix.lr_break(brk_dn), "break weak": ix.lr_break(weak),
         "trend up": ix.lr_trendbreak(brk_up), "trend: down break in an uptrend": ix.lr_trendbreak(brk_dn),
         "trend down": ix.lr_trendbreak(d_dn), "trend: up break in a downtrend": ix.lr_trendbreak(d_up)}
    ok = (r["break up"][0] == 1 and r["break down"][0] == -1 and r["break weak"] is None and r["trend up"][0] == 1
          and r["trend: down break in an uptrend"] is None and r["trend down"][0] == -1 and r["trend: up break in a downtrend"] is None)
    return ok, f"{r}"


def narrow_range_bar_and_inside_bar_both_ways():
    base = [[100, 100.25, 99.75, 100, 1]] * 30
    nr = [[100, 100.25, 99.75, 100.1, 1]] * 6 + [[100.05, 100.1, 100.0, 100.05, 1]]
    up = base + nr + [[100.05, 100.4, 100.05, 100.35, 1]]
    dn = base + nr + [[100.05, 100.05, 99.7, 99.72, 1]]
    not_narrow = base + [[100, 100.25, 99.75, 100.1, 1]] * 7 + [[100.05, 100.4, 100.05, 100.35, 1]]
    box = _mk([100 + 0.02 * ((-1) ** i) for i in range(100)], sp=0.1)
    mother, inner = [100.0, 100.3, 99.8, 100.0, 1], [100.0, 100.2, 99.9, 100.0, 1]
    i_up = box + [mother, inner, [100.0, 100.5, 100.0, 100.4, 1]]
    i_dn = box + [mother, inner, [100.0, 100.0, 99.5, 99.6, 1]]
    not_inside = box + [mother, [100.0, 100.4, 99.9, 100.0, 1], [100.0, 100.5, 100.0, 100.4, 1]]
    r = {"nr7 up": ix.lr_nr7(up), "nr7 down": ix.lr_nr7(dn), "nr7 not narrow": ix.lr_nr7(not_narrow),
         "inside up": ix.lr_inside(i_up), "inside down": ix.lr_inside(i_dn), "not inside": ix.lr_inside(not_inside)}
    ok = (r["nr7 up"][0] == 1 and r["nr7 down"][0] == -1 and r["nr7 not narrow"] is None
          and r["inside up"][0] == 1 and r["inside down"][0] == -1 and r["not inside"] is None)
    return ok, f"{r}"


def low_range_levels_add_the_position_cost():
    rows = _mk(_tight_after_wide() + [99.76], sp=0.1)
    lv = ix.low_range_levels(rows, 0.12, step=3)
    narrow = _mk([100.0] * 120, sp=0.0001)            # a box far narrower than the cost: the overall minimum holds
    lv2 = ix.low_range_levels(narrow, 0.12, step=2)
    ok = (lv is not None and abs(lv["sl_pct"] - (lv["range_pct"] + 0.12)) < 1e-9 and abs(lv["tp_pct"] - (3 * 0.12 + 0.12)) < 1e-9
          and lv2 is not None and abs(lv2["sl_pct"] - 2 * 0.12) < 1e-9 and abs(lv2["tp_pct"] - 3 * 0.12) < 1e-9)
    return ok, f"levels={lv} floor case={lv2}"


CHECKS += [
    ("extra.low-range-state-hand-values", low_range_state_hand_values),
    ("extra.low-range-fade-both-ways", low_range_fade_both_ways),
    ("extra.low-range-break-and-trendbreak-both-ways", low_range_break_and_trendbreak_both_ways),
    ("extra.nr7-and-inside-bar-both-ways", narrow_range_bar_and_inside_bar_both_ways),
    ("extra.low-range-levels-add-the-position-cost", low_range_levels_add_the_position_cost),
]


# ---- trend, breakout and prior-move relations ------------------------------------------------------------------------


def _mirror(rows, m=100.0):
    return [[2 * m - o, 2 * m - l, 2 * m - h, 2 * m - c, v] for o, h, l, c, v in rows]


def _relation_fixtures():
    fx = {}
    rise = [100 + 0.1 * i for i in range(80)]
    fx["htf_align"] = (_mk(rise + [rise[-1] - 0.15, rise[-1] + 0.05], sp=0.05), 1)
    fx["half_hour_continuation"] = (_mk(rise + [rise[-1] + 0.1], sp=0.05), 1)
    drop = [100.0 - 0.06 * i for i in range(16)]
    fx["quarter_reversal"] = (_mk([100.0] * 60 + drop + [drop[-1] + 0.11, drop[-1] + 0.22, drop[-1] + 0.33], sp=0.05), 1)
    base = [100 + 0.01 * i for i in range(60)]
    box = [base[-1] + 0.01 * ((-1) ** i) for i in range(30)]
    ref = max(r[1] for r in _mk(base + box, sp=0.05)[-32:])
    fx["breakout_retest"] = (_mk(base + box + [ref + 0.5, ref + 0.4, ref + 0.3, ref + 0.1, ref + 0.02, ref + 0.05, ref + 0.2], sp=0.05), 1)
    fl = _mk([100 + 0.03 * ((-1) ** i) for i in range(60)], sp=0.05)
    hi = max(r[1] for r in fl[-32:])
    fx["failed_breakout"] = (fl + [[100.0, hi + 0.3, 99.95, hi - 0.05, 1.0], [hi - 0.05, hi - 0.04, 99.7, 99.75, 1.0]], -1)
    quiet = [100 + 0.01 * ((-1) ** i) for i in range(75)]
    loud = [quiet[-1] + 0.3 * k for k in range(1, 6)]
    fx["vol_expansion_trend"] = (_mk(quiet, sp=0.02) + _mk(loud, sp=0.2), 1)
    fx["trend_breakout_combo"] = (_mk([100 + 0.15 * i for i in range(120)], sp=0.05), 1)
    b0 = [100.0 + 0.01 * ((-1) ** i) for i in range(40)]
    up = [100 + 0.1 * i for i in range(61)]
    bx = [up[-1] + 0.01 * ((-1) ** i) for i in range(24)]
    fx["lr_prevmove_break"] = (_mk(b0 + up + bx, sp=0.05) + [[bx[-1], bx[-1] + 0.6, bx[-1] - 0.02, bx[-1] + 0.5, 4.0]], 1)
    dn = [100 - 0.1 * i for i in range(61)]
    bx2 = [dn[-1] + 0.01 * ((-1) ** i) for i in range(24)]
    fx["lr_prevmove_reverse"] = (_mk(b0 + dn + bx2, sp=0.05) + [[bx2[-1], bx2[-1] + 0.6, bx2[-1] - 0.02, bx2[-1] + 0.5, 4.0]], 1)
    alt = [100 + 0.1 * ((-1) ** i) for i in range(119)]
    fx["mean_stretch_revert"] = (_mk(alt, sp=0.05) + [[98.9, 99.25, 98.85, 99.0, 1.0]], 1)
    return fx


def relation_indicators_fire_both_ways_and_stay_quiet_on_a_flat_tape():
    flat = _mk([100.0 + 0.02 * ((-1) ** i) for i in range(160)], sp=0.05)
    bad = {}
    for name, (rows, sign) in _relation_fixtures().items():
        fn = ix.INDICATORS[name]
        a, b, c = fn(rows), fn(_mirror(rows)), fn(flat)
        if not (a and a[0] == sign and b and b[0] == -sign and c is None):
            bad[name] = (a, b, c)
    return not bad, f"checked={len(_relation_fixtures())} wrong={bad}"


def prior_move_relation_separates_continuation_from_reversal():
    fx = _relation_fixtures()
    up_after_up, up_after_down = fx["lr_prevmove_break"][0], fx["lr_prevmove_reverse"][0]
    r = {"up/up break": ix.lr_prevmove_break(up_after_up), "up/up reverse": ix.lr_prevmove_reverse(up_after_up),
         "up after down break": ix.lr_prevmove_break(up_after_down), "up after down reverse": ix.lr_prevmove_reverse(up_after_down)}
    ok = r["up/up break"] and r["up/up reverse"] is None and r["up after down break"] is None and r["up after down reverse"]
    return bool(ok), f"{r}"


CHECKS += [
    ("extra.relation-indicators-both-ways-and-quiet-when-flat", relation_indicators_fire_both_ways_and_stay_quiet_on_a_flat_tape),
    ("extra.prior-move-continuation-vs-reversal", prior_move_relation_separates_continuation_from_reversal),
]


# ---- trend and breakout families -----------------------------------------------------------------------------------------

def _drift_tape(trend, n=300, seed=1, noise=0.03):
    import random
    r = random.Random(seed)
    out, p = [], 100.0
    for _ in range(n):
        o = p
        p = p * (1 + trend) + r.uniform(-noise, noise) * 0.1
        out.append([o, max(o, p) + 0.02, min(o, p) - 0.02, p, 10.0])
    return out


def _break_tape(sgn):
    t = _drift_tape(0.0, noise=0.5)
    p = t[-1][3]
    for _ in range(15):
        o = p
        p = p * (1 + sgn * 0.0012)
        t.append([o, max(o, p) + 0.01, min(o, p) - 0.01, p, 40.0])
    return t


def family_is_registered_in_every_range_and_timeframe():
    names = [f"{fam}_n{n}_t{tf}" for fam in ("trend", "brk") for n in ix.FAMILY_RANGES for tf in ix.FAMILY_TFS]
    combos = [f"{fam}_c{n}" for fam in ("trend", "brk", "tb") for n in ix.FAMILY_RANGES]
    missing = [x for x in names + combos if x not in ix.INDICATORS or ix.window_of(x) != ix.FAMILY_WINDOW]
    ok = not missing and ix.FAMILY_RANGES == (6, 10, 20, 40) and ix.FAMILY_TFS == (1, 5, 15)
    return ok, f"{len(names)} single + {len(combos)} combined; missing/other window: {missing}"


def hand_computed_fit_and_aggregate():
    slope, r2 = ix._fit([1.0, 2.0, 3.0, 4.0, 5.0])
    s2, r22 = ix._fit([1.0, 3.0, 2.0, 4.0, 3.0])             # mean 2.6, slope 0.5, r2 = 0.5^2*10 / 5.2 = 0.4808
    rows = [[float(i), float(i) + 2, float(i) - 1, float(i) + 1, 2.0] for i in range(10)]
    a = ix._agg(rows, 5)                                      # two 5-bar blocks, aligned to the newest row
    ok = (abs(slope - 1.0) < 1e-12 and abs(r2 - 1.0) < 1e-12 and abs(s2 - 0.5) < 1e-12 and abs(r22 - 0.5 ** 2 * 10 / 5.2) < 1e-9
          and len(a) == 2 and a[1] == [5.0, 11.0, 4.0, 10.0, 10.0] and a[0] == [0.0, 6.0, -1.0, 5.0, 10.0])
    return ok, f"slope={slope} r2={r2} noisy slope={s2:.3f} r2={r22:.4f} agg={a}"


def trend_family_fires_both_ways_and_is_quiet_on_noise():
    up, dn = _drift_tape(0.0006), _drift_tape(-0.0006)
    flat = _drift_tape(0.0, noise=1.0)
    res = {nm: (ix.INDICATORS[nm](up), ix.INDICATORS[nm](dn)) for nm in ("trend_n10_t1", "trend_n6_t1")}
    bad = {k: v for k, v in res.items() if not (v[0] and v[0][0] == 1 and v[1] and v[1][0] == -1)}
    loud = [n for n, f in ix.FAMILY.items() if n.startswith("trend") and f(flat)]
    return not bad and not loud, f"fires={ {k: v for k, v in res.items()} } wrong={bad} signals on noise={loud}"


def breakout_family_fires_both_ways_and_needs_the_close_and_volume():
    up, dn = _break_tape(1), _break_tape(-1)
    res = {nm: (ix.INDICATORS[nm](up), ix.INDICATORS[nm](dn)) for nm in ("brk_n6_t5", "brk_n10_t15", "brk_c10")}
    bad = {k: v for k, v in res.items() if not (v[0] and v[0][0] == 1 and v[1] and v[1][0] == -1)}
    # the same breakout without volume support, and a bar that closes back inside its range, give no signal
    weak = _break_tape(1)
    for r in weak[-15:]:
        r[4] = 1.0
    fade = _break_tape(1)
    fade[-1] = [fade[-1][0], fade[-1][1] + 3.0, fade[-1][2], fade[-1][0] + 0.001, 40.0]      # long upper wick, close near the open
    quiet = ix.INDICATORS["brk_n10_t15"](weak) is None
    return not bad and quiet, f"fires={res} wrong={bad} no-volume breakout quiet={quiet}"


def combined_needs_agreement_across_timeframes():
    same = ix._combine([(1, 0.7), (1, 0.8), None])
    split = ix._combine([(1, 0.7), (-1, 0.8), (1, 0.7)])
    lone = ix._combine([(1, 0.9), None, None])
    three = ix._combine([(-1, 0.7), (-1, 0.7), (-1, 0.7)])
    ok = (same and same[0] == 1 and 0.8 < same[1] <= 0.95) and split is None and lone is None and three and three[0] == -1
    return bool(ok), f"agree={same} split={split} lone={lone} three={three}"


def family_wires_into_the_set_vote_by_name():
    b = SetBook()
    b.load(overlay(extraIndicators=["trend_c10", "brk_n10_t5", "brk_n10_t5", "trend_nope"]))
    names = b.ind_settings["extraIndicators"]
    tape = _break_tape(1)
    on = indication_signal(tape, dict(b.ind_settings), T0)
    off = indication_signal(tape, dict(b.ind_settings, extraIndicators=[]), T0)
    return names == ["brk_n10_t5", "trend_c10"] and "brk_n10_t5" in on[2] and "brk_n10_t5" not in off[2], f"names={names} on={on} off={off}"


CHECKS += [
    ("extra.family-registered-every-range-and-timeframe", family_is_registered_in_every_range_and_timeframe),
    ("extra.family-hand-computed-fit-and-aggregate", hand_computed_fit_and_aggregate),
    ("extra.trend-family-both-ways-quiet-on-noise", trend_family_fires_both_ways_and_is_quiet_on_noise),
    ("extra.breakout-family-both-ways-needs-close-and-volume", breakout_family_fires_both_ways_and_needs_the_close_and_volume),
    ("extra.combined-needs-agreement", combined_needs_agreement_across_timeframes),
    ("extra.family-wires-into-the-set-vote", family_wires_into_the_set_vote_by_name),
]


# ---- indication library (indication_lib): every family, range and timeframe -------------------------------------------------

def _lib_noise(n=300, seed=1, vol=0.0004, drift=0.0, v=10.0, up_v=None):
    import random
    r = random.Random(seed)
    out, p = [], 100.0
    for _ in range(n):
        o = p
        p = p * (1 + drift + r.uniform(-vol, vol))
        c = p
        out.append([o, max(o, c) * 1.0002, min(o, c) * 0.9998, c, v if up_v is None or c <= o else up_v])
    return out


def _lib_tapes():
    t = {"trend": _lib_noise(drift=0.0006, vol=0.0003)}
    b = _lib_noise(280, 2, 0.0002)
    p = b[-1][3]
    for _ in range(20):
        o = p
        p = p * 1.0012
        b.append([o, p * 1.0003, o * 0.9998, p, 40.0])
    t["break"] = b
    r = _lib_noise(285, 3, 0.0004, drift=-0.0004)
    p = r[-1][3]
    for k in range(14):
        o = p
        p = p * 0.992 if k < 12 else p * 1.003
        r.append([o, max(o, p) * 1.0002, min(o, p) * 0.9998, p, 12.0])
    t["revert"] = r
    t["volume"] = _lib_noise(drift=0.0004, vol=0.0004, v=5.0, up_v=20.0)
    return t


def _mirror(b, k=400.0):
    return [[k - x[0], k - x[2], k - x[1], k - x[3], x[4]] for x in b]


def _fires(tape, fams):
    """{family: set of directions} over ranges 8/14/21 on the 1m and 5m series."""
    import indication_lib as lib
    out = {}
    for fam in fams:
        ds = set()
        for n in (8, 14, 21):
            for tf in (1, 5):
                s = lib.LIB[f"{fam}_n{n}_t{tf}"](tape)
                if s:
                    ds.add(s[0])
        out[fam] = ds
    return out


def lib_is_registered_for_every_family_range_and_timeframe():
    import indication_lib as lib
    missing = []
    for fam in lib.FAMILIES:
        for ns in lib.RANGES.values():
            for n in ns:
                for nm in [f"{fam}_n{n}_t{tf}" for tf in lib.TFS] + [f"{fam}_c{n}"]:
                    if nm not in ix.INDICATORS or ix.window_of(nm) != lib.WINDOW:
                        missing.append(nm)
    for g in ("trend", "break", "revert", "volume", "all"):
        for r in lib.RANGES:
            if f"g_{g}_{r}" not in ix.INDICATORS:
                missing.append(f"g_{g}_{r}")
    ok = not missing and lib.RANGES == {"min": (5, 8), "med": (14, 21)} and lib.TFS == (1, 5, 15) and len(lib.FAMILIES) >= 35
    return ok, f"{len(lib.FAMILIES)} families x 4 ranges x (3 timeframes + combined) + 10 group votes; missing: {missing[:5]}"


def lib_trend_families_follow_a_trend_both_ways():
    import indication_lib as lib
    fams = [f for f, (g, _) in lib.FAMILIES.items() if g == "trend"]
    tape = _lib_tapes()["trend"]
    up, dn = _fires(tape, fams), _fires(_mirror(tape), fams)
    up_n = sorted(f for f, d in up.items() if d == {1})
    dn_n = sorted(f for f, d in dn.items() if d == {-1})
    wrong = [f for f in fams if -1 in up[f] or 1 in dn[f]]
    return len(up_n) >= 8 and up_n == dn_n and not wrong, f"long on a rising tape: {up_n}; short on its mirror: {dn_n}; opposite signals: {wrong}"


def lib_breakout_families_fire_on_a_range_break_both_ways():
    tape = _lib_tapes()["break"]
    want = ["donch", "atrx", "rvol", "maru"]
    up, dn = _fires(tape, want), _fires(_mirror(tape), want)
    ok = all(up[f] == {1} for f in want) and all(dn[f] == {-1} for f in want)
    return ok, f"up={up} mirror={dn}"


def lib_reversion_families_fade_a_plunge_both_ways():
    tape = _lib_tapes()["revert"]
    want = ["rsirev", "stoch", "mfi", "zrev"]
    up, dn = _fires(tape, want), _fires(_mirror(tape), want)
    ok = all(up[f] == {1} for f in want) and all(dn[f] == {-1} for f in want)
    return ok, f"long after a plunge: {up}; short after a spike: {dn}"


def lib_volume_families_read_flow_both_ways():
    tape = _lib_tapes()["volume"]
    want = ["obv", "cmf", "udvol", "closeloc"]
    up, dn = _fires(tape, want), _fires(_mirror(tape), want)
    ok = all(up[f] == {1} for f in want) and all(dn[f] == {-1} for f in want)
    return ok, f"up={up} mirror={dn}"


def lib_is_mirror_symmetric_and_quiet_on_a_flat_tape():
    """Mirroring the prices (low <-> high) flips every direction and keeps the confidence: no family prefers one side. A flat
    tape gives no signal from any single indicator."""
    import indication_lib as lib
    bad, fired = [], 0
    for seed in (4, 5):
        tape = _lib_noise(300, seed, 0.0009, drift=0.0002 * (1 if seed % 2 else -1))
        mir = _mirror(tape)
        for name, fn in lib.LIB.items():
            if name.startswith("g_"):
                continue
            a, b = fn(tape), fn(mir)
            if a:
                fired += 1
                if not b or b[0] != -a[0] or abs(b[1] - a[1]) > 0.06:
                    bad.append(name)
    flat = [[100.0, 100.0, 100.0, 100.0, 5.0]] * 300
    loud = [n for n, f in lib.LIB.items() if not n.startswith("g_") and f(flat)]
    rate = len(bad) / max(1, fired)
    return rate <= 0.06 and not loud, f"fired={fired} asymmetric={len(bad)} ({rate:.1%}) e.g. {sorted(set(bad))[:6]}; signals on a flat tape: {loud[:4]}"


def lib_group_votes_need_agreement():
    import indication_lib as lib
    tapes = _lib_tapes()
    up = lib.LIB["g_trend_med"](tapes["trend"])
    dn = lib.LIB["g_trend_med"](_mirror(tapes["trend"]))
    flat = lib.LIB["g_all_med"]([[100.0, 100.0, 100.0, 100.0, 5.0]] * 300)
    ok = up and up[0] == 1 and dn and dn[0] == -1 and flat is None
    return bool(ok), f"trend group on a rising tape={up}, on its mirror={dn}, on a flat tape={flat}"


CHECKS += [
    ("extra.lib-registered-every-family-range-timeframe", lib_is_registered_for_every_family_range_and_timeframe),
    ("extra.lib-trend-both-ways", lib_trend_families_follow_a_trend_both_ways),
    ("extra.lib-breakout-both-ways", lib_breakout_families_fire_on_a_range_break_both_ways),
    ("extra.lib-reversion-both-ways", lib_reversion_families_fade_a_plunge_both_ways),
    ("extra.lib-volume-both-ways", lib_volume_families_read_flow_both_ways),
    ("extra.lib-mirror-symmetric-and-flat-quiet", lib_is_mirror_symmetric_and_quiet_on_a_flat_tape),
    ("extra.lib-group-votes-need-agreement", lib_group_votes_need_agreement),
]


# ---- indicators ported from CTS-A-O (indication_ao) ----------------------------------------------------------------------

def ao_is_registered_and_windowed():
    import indication_ao as ao
    missing = []
    for fam in ao.AO_FAMILIES:
        for ns in ao.RANGES.values():
            for n in ns:
                for nm in [f"ao_{fam}_n{n}_t{tf}" for tf in ao.TFS] + [f"ao_{fam}_c{n}"]:
                    if nm not in ix.INDICATORS or ix.window_of(nm) != ao.WINDOW:
                        missing.append(nm)
    for r in ao.RANGES:
        if f"g_ao_{r}" not in ix.INDICATORS:
            missing.append(f"g_ao_{r}")
    return not missing and len(ao.AO_FAMILIES) == 12, f"{len(ao.AO_FAMILIES)} families; missing: {missing[:4]}"


def ao_follows_a_trend_and_a_plunge_both_ways():
    import indication_ao as ao
    tapes = _lib_tapes()
    trend, plunge, brk = tapes["trend"], tapes["revert"], tapes["break"]

    def fires(tape, fams, ns=(8, 14), tfs=(1, 5)):
        out = {}
        for fam in fams:
            ds = set()
            for n in ns:
                for tf in tfs:
                    s = ao.AO[f"ao_{fam}_n{n}_t{tf}"](tape)
                    if s:
                        ds.add(s[0])
            out[fam] = ds
        return out
    trend_f = ["cfltrend", "klinger", "frama", "stpb", "htfpb"]
    up, dn = fires(trend, trend_f), fires(_mirror(trend), trend_f)
    up_ok = sorted(f for f, d in up.items() if d == {1})
    dn_ok = sorted(f for f, d in dn.items() if d == {-1})
    rev_f = ["laguerre", "ultimate", "cmpx", "regime"]
    brk_up, brk_dn = fires(brk, ["cflbrk", "regime", "cmpx"]), fires(_mirror(brk), ["cflbrk", "regime", "cmpx"])
    wrong = [f for f in trend_f if -1 in up[f] or 1 in dn[f]]
    ok = "cfltrend" in up_ok and up_ok == dn_ok and not wrong and brk_up["cflbrk"] == {1} and brk_dn["cflbrk"] == {-1}
    return ok, f"trend long={up_ok} mirror short={dn_ok} opposite={wrong}; breakout up={brk_up['cflbrk']} mirror={brk_dn['cflbrk']}"


def ao_is_mirror_symmetric_and_quiet_on_a_flat_tape():
    import indication_ao as ao
    bad, fired = [], 0
    for seed in (4, 5, 6):
        tape = _lib_noise(300, seed, 0.0009, drift=0.0002 * (1 if seed % 2 else -1))
        mir = _mirror(tape)
        for name, fn in ao.AO.items():
            if name.startswith("g_"):
                continue
            a, b = fn(tape), fn(mir)
            if a:
                fired += 1
                if not b or b[0] != -a[0] or abs(b[1] - a[1]) > 0.06:
                    bad.append(name)
    flat = [[100.0, 100.0, 100.0, 100.0, 5.0]] * 300
    loud = [n for n, f in ao.AO.items() if not n.startswith("g_") and f(flat)]
    rate = len(bad) / max(1, fired)
    return rate <= 0.08 and not loud, f"fired={fired} asymmetric={len(bad)} ({rate:.1%}) e.g. {sorted(set(bad))[:5]}; flat-tape signals: {loud[:4]}"


CHECKS += [
    ("extra.ao-registered-and-windowed", ao_is_registered_and_windowed),
    ("extra.ao-both-ways", ao_follows_a_trend_and_a_plunge_both_ways),
    ("extra.ao-mirror-symmetric-and-flat-quiet", ao_is_mirror_symmetric_and_quiet_on_a_flat_tape),
]
