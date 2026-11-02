"""Minimum PF per TP step (minPfByStep): the manual formula, one floor for every window and stage, and the Auto Mode learned from earlier orders.

The learned floor is read against a plain re-derivation (loops over the raw rows, no prefix sums, no sorting), the no-lookahead rule by changing every
result at or after the learning boundary, and the 'a copy counts once' rule by duplicating a Set's tape."""
from __future__ import annotations

import random

from common import SMALL, T0, overlay
import set_engine as se


def _book(**extra):
    b = se.SetBook()
    b.load(overlay(**{**SMALL, "setStepMax": 6, "stratShortRange": False, "stratGeneral": False, "minPfByStep": True, **extra}))
    return b


def manual_floor_is_one_plus_step_times_ratio():
    table = [(0.2, 0.2, 1.04), (0.25, 0.2, 1.04), (0.29, 0.2, 1.04), (0.3, 0.2, 1.06), (3.2, 0.2, 1.64), (3.5, 0.2, 1.70), (9.0, 0.2, 1.70),
             (0.1, 0.2, 1.04), (0.2, 1.1, 1.22), (3.5, 1.1, 4.85), (1.0, 0.1, 1.10)]
    got = [round(se.manual_step_floor(tp, ratio), 6) for tp, ratio, _ in table]
    want = [w for _, _, w in table]
    ratios = {raw: se.step_pf_ratio(raw) for raw in (0.05, 0.1, 0.2, 0.33, 1.1, 1.5, "x", None)}
    nan_ok = se.step_pf_ratio(float("nan")) == 0.2 and se.step_pf_ratio(float("inf")) == 0.2
    grid_ok = all(abs(se.step_pf_ratio(0.1 + 0.05 * i) - round(0.1 + 0.05 * i, 2)) < 1e-9 for i in range(21))
    ok = got == want and ratios == {0.05: 0.1, 0.1: 0.1, 0.2: 0.2, 0.33: 0.35, 1.1: 1.1, 1.5: 1.1, "x": 0.2, None: 0.2} and grid_ok and nan_ok
    return ok, f"floors {got}; ratio snaps/clamps to 0.1..1.1 step 0.05: {ratios}"


def one_floor_for_every_window_and_stage():
    b = _book()
    sets = [s for s in b.by_idx if s.kind == "base" and s.sl_ratio == 1.0]
    by_step = {s.step: s for s in sets}
    floors = {k: b.floor_for(50, s.pack, s) for k, s in by_step.items()}
    same = all(len({b.floor_for(n, s.pack, s) for n in (1, 5, 10, 25, 50, 200)}) == 1 for s in sets)
    params_same = all(len({b.stage_params(s.pack, s).floor_for(n) for n in (1, 5, 10, 25, 50, 200)}) == 1
                      and b.stage_params(s.pack, s).floor == b.floor_for(50, s.pack, s) and not b.stage_params(s.pack, s).size_floors for s in sets)
    want = {k: se.manual_step_floor(k * 0.1, 0.2) for k in by_step}
    off = _book(minPfByStep=False, minPfBySize=True)
    classes = [off.floor_for(n, "indications", next(iter(off.by_idx))) for n in (5, 15, 25, 50)]
    ratio = _book(minPfStepRatio=0.5)
    r_floor = ratio.floor_for(50, "indications", next(s for s in ratio.by_idx if s.kind == "base" and s.step == 2))
    ok = floors == want and same and params_same and classes == [1.05, 1.15, 1.25, 1.5] and abs(r_floor - 1.1) < 1e-9
    return ok, f"floors by step {floors}; one value over windows 1..200 and in StageParams; size classes back when off {classes}; ratio 0.5 at 0.2 % = {r_floor}"


# ---- the Auto Mode against a plain reading --------------------------------------------------------------------------------------------------------

def _rows(seed, n, signal=True):
    """One Set's tape: (exit t, symbol, side, net pnl, gross pnl_pct, hold_s, reason, set_id). After a strong window (PF over the last 10 closes at
    least 1.6) the next result is likely to win; otherwise it slightly loses -- so a high floor should be learned."""
    r = random.Random(seed)
    rows, nets, t = [], [], T0 - 4_000_000.0
    for _ in range(n):
        t += 600.0 + r.random() * 600.0
        hold = 300.0
        w = nets[-10:]
        gp, gl = sum(x for x in w if x > 0), -sum(x for x in w if x < 0)
        pf = (gp / gl) if gl > 0 else (99.0 if gp > 0 else 0.0)
        p_win = 0.8 if (signal and len(w) == 10 and pf >= 1.6) else 0.35
        net = (0.004 if r.random() < p_win else -0.004) + r.gauss(0, 0.0004)
        nets.append(net)
        rows.append((t, "S0-USDT", "LONG", net, net + 0.0012, hold, "sl", ""))
    return rows


def _oracle(rows_by_set, cutoff, ratio, bucket, n_win, n_min, cf=0.0012):
    """Plain reading: loops only. Returns (floor, admitted, pf_all) for one step bucket."""
    pairs = []
    seen = set()
    for rows in rows_by_set:
        rows = [r for r in rows if r[0] < cutoff]
        sig = tuple((round(r[0], 3), r[1], round(r[4], 10)) for r in rows)
        if len(rows) <= n_win or sig in seen:
            continue
        seen.add(sig)
        nets = [r[4] - cf for r in rows]
        for k, r in enumerate(rows):
            prior = [j for j in range(len(rows)) if rows[j][0] < rows[k][0] - r[5]]
            if len(prior) < n_win:
                continue
            w = [nets[j] for j in prior[-n_win:]]
            gp, gl = sum(x for x in w if x > 0), -sum(x for x in w if x < 0)
            pf = gp / gl if gl > 0 else (99.0 if gp > 0 else None)
            if pf is not None:
                pairs.append((pf, nets[k]))

    def delivered(f):
        sel = [v for p, v in pairs if p >= f - 1e-12]
        gp, gl = sum(v for v in sel if v > 0), -sum(v for v in sel if v < 0)
        return (gp / gl if gl > 0 else (99.0 if gp > 0 else 0.0)), len(sel)
    manual = se.manual_step_floor(bucket * 0.1, ratio)
    ref_pf, ref_n = delivered(manual)
    best = (manual, ref_pf, ref_n)
    for f in se.STEP_PF_CANDIDATES:
        pf, n = delivered(f)
        if n >= n_min and pf > best[1] + 1e-12 and pf >= ref_pf * se.STEP_PF_MARGIN and (ref_n >= n_min or best[0] == manual):
            best = (f, pf, n)
    return best[0], best[2], len(pairs)


BOUNDARY = T0 - 3_300_000.0       # inside the synthetic tapes (they run from T0 - 4.0 M s to about T0 - 2.65 M s): rows exist on both sides


def _learn(b, tapes, boundary):
    """Run the engine's learning pass on injected tapes (set id -> rows)."""
    real = b._tape_rows
    b._tape_rows = lambda key, order, cutoff, until=None: [r for r in tapes.get(key, ()) if cutoff <= r[0] <= (float("inf") if until is None else until)]
    try:
        b._learn_step_floors(["S0-USDT"], boundary)
    finally:
        b._tape_rows = real


def auto_floor_matches_a_plain_reading_and_learns_from_a_real_relation():
    b = _book(minPfStepAutoMin=100)
    bucket_sets = [s for s in b.by_idx if s.kind == "base" and s.step == 3][:3]
    tapes = {s.id: _rows(100 + i, 1500) for i, s in enumerate(bucket_sets)}
    boundary = BOUNDARY
    _learn(b, tapes, boundary)
    want_f, want_n, want_pairs = _oracle(tapes.values(), boundary, 0.2, 3, b.pf_step_auto_n, 100)
    got = b.step_floor_auto.get(3)
    row = next(r for r in b.step_floor_report if r["step"] == 3)
    manual = se.manual_step_floor(0.3, 0.2)
    ok = ((got if got is not None else manual) == want_f and want_f > manual and row["admitted"] == want_n and row["orders"] == want_pairs
          and b.step_floor(bucket_sets[0]) == want_f and b.floor_for(50, bucket_sets[0].pack, bucket_sets[0]) == want_f)
    return ok, f"learned {got} (plain reading {want_f}, manual {manual}); admitted {row['admitted']}/{row['orders']} orders, delivered PF {row['pfAdmitted']} vs all {row['pfAll']}"


def auto_floor_never_reads_the_window_it_judges():
    b = _book(minPfStepAutoMin=100)
    sets = [s for s in b.by_idx if s.kind == "base" and s.step == 3][:3]
    boundary = BOUNDARY
    tapes = {s.id: _rows(100 + i, 1500) for i, s in enumerate(sets)}
    after = sum(1 for rows in tapes.values() for r in rows if r[0] >= boundary)
    before = sum(1 for rows in tapes.values() for r in rows if r[0] < boundary)
    _learn(b, tapes, boundary)
    base = (dict(b.step_floor_auto), [dict(r) for r in b.step_floor_report])
    # every result at or after the boundary is replaced by the opposite outcome: the learned floors must not move
    flipped = {k: [(r[0], r[1], r[2], -r[3], -r[4], r[5], r[6], r[7]) if r[0] >= boundary else r for r in rows] for k, rows in tapes.items()}
    _learn(b, flipped, boundary)
    same = (dict(b.step_floor_auto), [dict(r) for r in b.step_floor_report]) == base
    # and the boundary matters: moving it later (more evidence) is allowed to change the floor, so the check is not vacuous
    _learn(b, flipped, boundary + 300_000.0)
    moved = [dict(r) for r in b.step_floor_report] != base[1]
    return same and moved and after > 300 and before > 300, (f"{after} results at or after the boundary flipped (and {before} before it kept): the floors are unchanged ({same}); "
                                                            f"a later boundary reads more orders and does change the report ({moved})")


def copies_count_once_and_thin_evidence_keeps_the_manual_floor():
    b = _book(minPfStepAutoMin=100)
    sets = [s for s in b.by_idx if s.kind == "base" and s.step == 3][:4]
    boundary = BOUNDARY
    one = _rows(7, 1500)
    _learn(b, {sets[0].id: one}, boundary)
    single = next(r for r in b.step_floor_report if r["step"] == 3)
    _learn(b, {s.id: one for s in sets}, boundary)                    # four Sets with the same orders
    copies = next(r for r in b.step_floor_report if r["step"] == 3)
    thin = _book(minPfStepAutoMin=100000)                             # more admitted orders than any tape has
    _learn(thin, {sets[0].id: one}, boundary)
    t_row = next(r for r in thin.step_floor_report if r["step"] == 3)
    off = _book(minPfStepAuto=False)
    _learn(off, {sets[0].id: one}, boundary)
    ok = (single["orders"] == copies["orders"] and single["admitted"] == copies["admitted"] and single["orders"] > 0
          and t_row["auto"] is None and t_row["used"] == t_row["manual"] and not off.step_floor_auto and off.step_floor_report == [])
    return ok, f"1 Set {single['orders']} pairs, 4 identical Sets {copies['orders']} pairs; too few admitted orders -> manual {t_row['used']}; auto off -> nothing learned"


def settings_reach_the_engine_and_the_replay_key():
    d = se.SetBook()
    d.load({})
    on = _book(minPfStepRatio=0.45, minPfStepAutoN=12, minPfStepAutoMin=500)
    bad = _book(minPfStepRatio="x", minPfStepAutoN=999, minPfStepAutoMin=1)
    off = _book(minPfByStep=False, minPfStepAuto=False)
    ok = (d.pf_by_step and d.pf_step_auto and d.pf_step_ratio == 0.2 and d.pf_step_auto_n == 10
          and (on.pf_step_ratio, on.pf_step_auto_n, on.pf_step_auto_min) == (0.45, 12, 500)
          and (bad.pf_step_ratio, bad.pf_step_auto_n, bad.pf_step_auto_min) == (0.2, 50, 30)
          and not off.pf_by_step and not off.pf_step_auto
          and on._evaluation_config != _book()._evaluation_config)
    return ok, f"defaults on/auto/ratio {d.pf_by_step}/{d.pf_step_auto}/{d.pf_step_ratio}/N {d.pf_step_auto_n}; bad values -> {bad.pf_step_ratio}/{bad.pf_step_auto_n}/{bad.pf_step_auto_min}; replay key moves with the settings"


CHECKS = [("step-pf." + fn.__name__.replace("_", "-"), fn) for fn in (
    manual_floor_is_one_plus_step_times_ratio, one_floor_for_every_window_and_stage,
    auto_floor_matches_a_plain_reading_and_learns_from_a_real_relation, auto_floor_never_reads_the_window_it_judges,
    copies_count_once_and_thin_evidence_keeps_the_manual_floor, settings_reach_the_engine_and_the_replay_key)]
