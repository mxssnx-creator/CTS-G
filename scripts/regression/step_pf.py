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
    table = [(0.2, 0.2, 1.04), (0.25, 0.2, 1.04), (0.29, 0.2, 1.04), (0.3, 0.2, 1.06), (3.2, 0.2, 1.64), (3.5, 0.2, 1.70), (9.0, 0.2, 1.70), (5.0, 0.2, 1.70), (4.2, 0.2, 1.70), (1.5, 1.5, 3.25),
             (0.1, 0.2, 1.04), (0.2, 1.1, 1.22), (3.5, 1.1, 4.85), (1.0, 0.1, 1.10), (0.5, 0.2, 1.10)]
    got = [round(se.manual_step_floor(tp, ratio), 6) for tp, ratio, _ in table]
    want = [w for _, _, w in table]
    ratios = {raw: se.step_pf_ratio(raw) for raw in (0.05, 0.1, 0.2, 0.33, 1.1, 1.5, 1.8, "x", None)}
    nan_ok = se.step_pf_ratio(float("nan")) == 0.2 and se.step_pf_ratio(float("inf")) == 0.2
    grid_ok = all(abs(se.step_pf_ratio(0.1 + 0.05 * i) - round(0.1 + 0.05 * i, 2)) < 1e-9 for i in range(29))
    ok = got == want and ratios == {0.05: 0.1, 0.1: 0.1, 0.2: 0.2, 0.33: 0.35, 1.1: 1.1, 1.5: 1.5, 1.8: 1.5, "x": 0.2, None: 0.2} and grid_ok and nan_ok
    return ok, f"floors {got}; ratio snaps/clamps to 0.1..1.5 step 0.05: {ratios}"


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
    ok = floors == want and same and params_same and classes == [1.02, 1.15, 1.25, 1.5] and abs(r_floor - 1.1) < 1e-9
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


def _oracle(rows_by_set, cutoff, ratio, bucket, n_win, n_min, cf=0.0012, tol=0.05):
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
    # the objective without the manual ratio: of the candidates with n_min admitted orders, the lowest floor within the tolerance of the best PF
    rows = []
    for f in se.STEP_PF_CANDIDATES:
        pf, n = delivered(f)
        if n >= n_min:
            rows.append((f, pf, n))
    if not rows:
        return se.manual_step_floor(bucket * 0.1, ratio), 0, len(pairs)
    best_pf = max(r[1] for r in rows)
    pick = [r for r in rows if r[1] + 1e-12 >= best_pf * (1.0 - tol)][0]          # candidates ascend: the first is the lowest
    return pick[0], pick[2], len(pairs)


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
    got = b.step_floor_auto.get((3, 0))
    row = next(r for r in b.step_floor_report if r["step"] == 3)
    manual = se.manual_step_floor(0.3, 0.2)
    # the manual ratio never moves an Auto choice that has data
    b2 = _book(minPfStepAutoMin=100, minPfStepRatio=1.0)
    sets2 = [s for s in b2.by_idx if s.kind == "base" and s.step == 3][:3]
    _learn(b2, {s2.id: tapes[s1.id] for s1, s2 in zip(bucket_sets, sets2)}, boundary)
    ok = ((got if got is not None else manual) == want_f and row["source"] == "step" and row["admitted"] == want_n and row["orders"] == want_pairs
          and b.step_floor(bucket_sets[0]) == want_f and b.floor_for(50, bucket_sets[0].pack, bucket_sets[0]) == want_f
          and b2.step_floor_auto.get((3, 0)) == got)
    return ok, f"learned {got} (plain reading {want_f}, manual {manual}; with ratio 1.0 still {b2.step_floor_auto.get((3, 0))}); admitted {row['admitted']}/{row['orders']} orders, delivered PF {row['pfAdmitted']} vs all {row['pfAll']}"


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
          and (bad.pf_step_ratio, bad.pf_step_auto_n, bad.pf_step_auto_min) == (0.2, 50, 1)
          and not off.pf_by_step and not off.pf_step_auto
          and on._evaluation_config != _book()._evaluation_config)
    return ok, f"defaults on/auto/ratio {d.pf_by_step}/{d.pf_step_auto}/{d.pf_step_ratio}/N {d.pf_step_auto_n}; bad values -> {bad.pf_step_ratio}/{bad.pf_step_auto_n}/{bad.pf_step_auto_min}; replay key moves with the settings"


def a_half_percent_step_at_ratio_point_two_asks_exactly_1_1():
    """The owner's example: TP step 0.5 % at ratio 0.2 -> 1 (neutral) + 0.5 x 0.2 = 1.10. The gate sits exactly there for the Set that has that step,
    and a neighbour step asks its own number (0.4 % -> 1.08, 0.6 % -> 1.12)."""
    b = _book(stagesEnabled=False, setGateShortN=0, setGateMinTrades=0, setMinStep=4, setStepMax=6)
    by = {s.step: s for s in b.by_idx if s.kind == "base" and s.sl_ratio == 1.0}
    st = by[5]
    st.active, st.locked, st.last_error, st.gate_n, st.max_dd_s = True, False, "", 50, 0.0
    verdicts = {}
    for pf in (1.0999, 1.10, 1.1001):
        st.gate_pf = pf
        verdicts[pf] = bool(b.is_eligible(st))
    floors = {k: round(b.step_floor(v), 6) for k, v in sorted(by.items())}
    ok = (st.tp_pct == 0.005 and floors == {4: 1.08, 5: 1.1, 6: 1.12} and se.manual_step_floor(0.5, 0.2) == 1.1
          and verdicts == {1.0999: False, 1.1: True, 1.1001: True} and b.floor_for(50, st.pack, st) == 1.1)
    return ok, f"step 5 = {st.tp_pct * 100:.1f} % TP: floors by step {floors}; gate PF 1.0999 / 1.10 / 1.1001 -> {verdicts}"



def auto_floor_takes_the_lowest_floor_near_the_best_and_falls_back_step_pooled_manual():
    """learn_step_floor on pairs whose delivered PF rises to a plateau: the lowest floor within the tolerance of the best is chosen (more Sets and
    orders at nearly the best PF), not the single best; a step without enough admitted orders takes the floor its direction's pooled pairs give; a
    direction without any takes the manual floor."""
    import random
    r = random.Random(7)
    pairs = []
    for _ in range(4000):
        pf_at_entry = r.uniform(0.5, 3.0)
        edge = 0.0006 if pf_at_entry >= 1.3 else -0.0006              # orders of Sets above 1.3 at entry win on average, the others lose
        pairs.append((pf_at_entry, edge + r.gauss(0, 0.002)))
    got = se.learn_step_floor(pairs, 300, 0.05)
    rows = []
    for f in se.STEP_PF_CANDIDATES:
        sel = [v for p, v in pairs if p >= f - 1e-12]
        if len(sel) >= 300:
            g, l_ = sum(v for v in sel if v > 0), -sum(v for v in sel if v < 0)
            rows.append((f, g / l_))
    best = max(p for _, p in rows)
    want = min(f for f, p in rows if p >= best * 0.95 - 1e-12)
    single_best = max(rows, key=lambda x: x[1])[0]
    none = se.learn_step_floor(pairs[:100], 300, 0.05)
    b = _book(minPfStepAutoMin=100)
    sets3 = [s for s in b.by_idx if s.kind == "base" and s.step == 3][:3]
    _learn(b, {s.id: _rows(100 + i, 1500) for i, s in enumerate(sets3)}, BOUNDARY)
    src = {r_["step"]: r_["source"] for r_ in b.step_floor_report}
    pooled_ok = src.get(3) == "step" and all(v == "pooled" for k, v in src.items() if k != 3) and all(b.step_floor_auto.get((k, 0)) is not None for k in src)
    c = _book(minPfStepAutoMin=100)
    _learn(c, {}, BOUNDARY)
    manual_ok = all(r_["source"] == "manual" for r_ in c.step_floor_report) and not c.step_floor_auto
    ok = got is not None and got["floor"] == want and want <= single_best and none is None and pooled_ok and manual_ok
    return ok, (f"chosen {got and got['floor']} (lowest within 5 % of the best PF {best:.3f}; the single best floor {single_best}); 100 pairs -> {none}; "
                f"sources with step 3 only: step 3 {src.get(3)}, others pooled={pooled_ok}; no data -> manual={manual_ok}")


def the_simulator_learns_the_engines_floors():
    """stage_eval.AutoFloors (the walk-forward simulators) and SetBook._learn_step_floors (the engine) learn the same floor for every (step, direction)
    from the same orders closed before the same hour: the simulators no longer run on the manual floors while the engine runs Auto."""
    import json as _json
    import os as _os
    import stage_eval as sg
    from common import SMALL, T0, synth_bars
    names = ["S0-USDT", "S1-USDT"]
    bars = {s_: synth_bars(41 + i, 1500, drift=0.0005 if i % 2 == 0 else -0.0005, vol=0.003) for i, s_ in enumerate(names)}
    times = {s_: [int(T0) + 60 * k for k in range(len(bars[s_]))] for s_ in names}
    patch = {**SMALL, "holdEnabled": False, "histLookbackBars": 600, "histLookbackAuto": False, "stratGeneral": True, "stratIndications": True,
             "minPfByStep": True, "minPfStepAuto": True, "minPfStepAutoMin": 30, "minPfStepAutoN": 5, "directionMode": "split", "pauseMode": "off",
             "setScratchEnabled": True}
    saved = _os.environ.get("STAGE_OVERLAY_PATCH")
    try:
        _os.environ["STAGE_OVERLAY_PATCH"] = _json.dumps(patch)
        shadow = {}
        for s_ in names:
            _, warm, dirs, confs, out = sg._symbol_job((s_, bars[s_], times[s_][0]))
            shadow[s_] = (warm, dirs, confs, out)
        book = sg.make_book()
        tapes = sg.Tapes(book, names, times, shadow)
        hour = float(int((times[names[0]][0] + 1100 * 60) // 3600) * 3600)
        auto = sg.AutoFloors(book, tapes)
        auto.apply(book, hour)
        sim = dict(book.step_floor_auto)
        # the engine on the same tapes: the simulator's orders handed to the learning pass as the replay's rows
        rows = {}
        for st in book.by_idx:
            tx, te, pp = tapes.by_set[st.idx]
            rows[st.id] = [(float(a), symbol, "LONG", 0.0, float(x), float(a - b), "x", st.id)
                           for a, b, x, symbol in zip(tx, te, pp, tapes.symbols_by_set[st.idx])]
        real = book._tape_rows
        book._tape_rows = lambda key, order, cutoff, until=None: [r for r in rows.get(key, ()) if cutoff <= r[0] <= (float("inf") if until is None else until)]
        try:
            book._learn_step_floors(names, hour - book.lookback * se.BAR_S)
        finally:
            book._tape_rows = real
        eng = dict(book.step_floor_auto)
    finally:
        if saved is None:
            _os.environ.pop("STAGE_OVERLAY_PATCH", None)
        else:
            _os.environ["STAGE_OVERLAY_PATCH"] = saved
    ok = bool(sim) and sim == eng and len(set(sim.values())) >= 1
    diff = {k: (sim.get(k), eng.get(k)) for k in set(sim) | set(eng) if sim.get(k) != eng.get(k)}
    return ok, f"{len(sim)} (step, direction) floors learned by the simulator, {len(eng)} by the engine at the same hour; differences {diff or 'none'}; values {sorted(set(sim.values()))}"

def auto_floor_never_under_its_lower_bound():
    """minPfStepAutoLow bounds the Auto floor from below: on pairs where every floor delivers the same PF the rule picks its lowest candidate, which is
    1.00 by default and the first candidate at or above the bound otherwise (live x02 2026-11-03: Auto sat at 1.00, under MIN_PF_FLOOR, for most steps)."""
    rnd = random.Random(5)
    pairs = [(1.0 + rnd.random(), rnd.choice((0.01, -0.01))) for _ in range(2000)]
    got = {}
    for low in (None, 1.05, 1.10):
        b = se.SetBook()
        b.load(overlay(**SMALL, **({} if low is None else {"minPfStepAutoLow": low})))
        r = se.learn_step_floor(pairs, 300, 0.05, b.step_auto_candidates())
        got[low] = r["floor"] if r else None
    ok = got[None] is not None and got[None] <= got[1.05] and got[1.05] is not None and got[1.05] >= 1.05 and got[1.10] is not None and got[1.10] >= 1.10
    return ok, f"Auto floor by lower bound: {got}"


def min_pf_by_range_replaces_the_step_floor_for_its_range_only():
    """Owner 2026-11-04 ("lower optimal min PF for micro, minimal, short"): minPfByRange {range: floor} is the one floor of every Set of that TP range in every
    window and stage; the other ranges keep their per-step floor (manual here); a bad value is dropped; bounded to 1.0 .. 2.0; absent = unchanged."""
    from position_cost import tp_range_of
    plain = _book(setStepMax=32, minPfStepAuto=False)
    ranged = _book(setStepMax=32, minPfStepAuto=False, minPfByRange={"micro": 1.02, "minimal": 1.03, "short": 1.04, "bogus": 1.5, "normal": "x", "long": 0})
    sets = [x for x in ranged.by_idx if x.kind == "base" and x.sl_ratio == 1.0]
    bad, seen = [], set()
    for x in sets:
        rng = tp_range_of(x.step)
        seen.add(rng)
        want = {"micro": 1.02, "minimal": 1.03, "short": 1.04}.get(rng)
        got = ranged.floor_for(50, x.pack, x)
        plain_floor = plain.floor_for(50, x.pack, x)
        windows = {ranged.floor_for(n, x.pack, x) for n in (1, 5, 25, 50)} | {ranged.stage_params(x.pack, x).floor_for(n) for n in (1, 5, 25, 50)}
        if want is not None and (got != want or len(windows) != 1):
            bad.append((x.id, rng, got, want))
        if want is None and (got != plain_floor or got != se.manual_step_floor(x.step * 0.1, 0.2)):
            bad.append((x.id, rng, got, plain_floor))
    clamp = se.pf_range_floors_of({"micro": 0.5, "short": 7, "wide": float("nan"), "normal": None, "Long": "1.3", "x": 1.2})
    ok = not bad and {"micro", "minimal", "short", "normal", "long"} <= seen and clamp == {"micro": 1.0, "short": 2.0, "long": 1.3}
    return ok, f"{len(sets)} Sets over ranges {sorted(seen)}; range floors applied and others untouched{(' bad ' + str(bad[:3])) if bad else ''}; bounds {clamp}"


def shipped_micro_floor_is_positive_and_shared_by_every_stage():
    import json
    from pathlib import Path
    from common import PULSE
    failures, checked = [], 0
    for path in Path(PULSE).glob('overlay-*.json'):
        raw = json.loads(path.read_text())
        book = se.SetBook(); book.load(raw)
        micro_sets = [st for st in book.by_idx if book.step_bucket(st) in (2, 3) and not se.is_signal_pack(st.pack)]
        if raw.get('minPfByRange', {}).get('micro') != 1.01 or not micro_sets:
            failures.append(path.name + ': missing Micro')
        for st in micro_sets:
            got = {book.floor_for(n, st.pack, st) for n in (1,5,10,20,50)}
            got |= {book.stage_params(st.pack, st).floor_for(n) for n in (1,5,10,20,50)}
            if got != {1.01}: failures.append((path.name, st.id, got))
            checked += 1
    return checked > 0 and not failures, f'{checked} Micro sets; all stage floors = 1.01; failures={failures[:3]}'


def min_pf_step_cap_bounds_every_step_floor_and_the_auto_candidates():
    """Owner 2026-11-04 ("min PF from 1.4 to 1.27"): minPfStepAutoHigh caps the floor a TP step may ask - the manual 1 + TP% x ratio (1.64 at 3.2 % uncapped), the
    learned Auto floor (never chosen above it, no learned value above it is returned) - in every window and stage; 2.0 (default) = unchanged; a value under the
    Auto lower bound is raised to it; a range floor (minPfByRange) is an explicit choice and is not capped."""
    capped = _book(setStepMax=32, minPfStepAuto=False, minPfStepAutoHigh=1.27)
    plain = _book(setStepMax=32, minPfStepAuto=False)
    sets = [x for x in capped.by_idx if x.kind == "base" and x.sl_ratio == 1.0]
    bad = []
    for x in sets:
        c, p = capped.floor_for(50, x.pack, x), plain.floor_for(50, x.pack, x)
        if abs(c - min(p, 1.27)) > 1e-9 or len({capped.floor_for(n, x.pack, x) for n in (1, 5, 25, 50)}) != 1:
            bad.append((x.id, c, p))
    auto = _book(setStepMax=32, minPfStepAutoHigh=1.27)
    auto.step_floor_auto = {(x.step, x.side): 1.9 for x in sets}
    learned = {auto.floor_for(50, x.pack, x) for x in sets}
    cands = auto.step_auto_candidates()
    low_first = _book(minPfStepAutoLow=1.5, minPfStepAutoHigh=1.2)
    ranged = _book(setStepMax=32, minPfStepAutoHigh=1.27, minPfByRange={"short": 1.5})
    r_floor = {ranged.floor_for(50, x.pack, x) for x in ranged.by_idx if x.kind == "base" and x.step in (8, 9)}
    ok = (not bad and learned == {1.27} and max(cands) <= 1.27 + 1e-9 and min(cands) == 1.0 and low_first.pf_step_auto_high >= low_first.pf_step_auto_low
          and r_floor == {1.5} and plain.pf_step_auto_high == 2.0)
    return ok, f"{len(sets)} Sets: manual floors capped at 1.27 (bad {bad[:2]}), learned 1.9 -> {sorted(learned)}, candidates {min(cands)}..{max(cands)}, range floor {sorted(r_floor)}, default cap {plain.pf_step_auto_high}"



def auto_wide_steps_use_the_last_table_bucket_but_keep_policy_identity():
    from dataclasses import replace
    b = _book(minPfStepAuto=True)
    base = next(st for st in b.by_idx if st.kind == "base")
    b.step_floor_auto = {(35, base.side): 1.18}
    for step in (35, 36, 42, 50):
        st = replace(base, step=step, tp_pct=step * .001)
        assert b.step_bucket(st) == step and b.auto_step_bucket(st) == 35
        assert {b.floor_for(n, st.pack, st) for n in (1, 5, 50, 200)} == {1.18}
        assert b.stage_params(st.pack, st).floor == 1.18
    b.pf_range_floor = {"wide": 1.31}
    st = replace(base, step=50, tp_pct=.05)
    assert b.step_floor(st) == 1.31
    b.pack_ranges[st.pack] = {"lanePolicy": {"minPfByStep": {"50": 1.44}}}
    assert b.floor_for(5, st.pack, st) == 1.44
    return True, "steps 35..50 share Auto's final bucket; actual step/range/policy identity is preserved"


def auto_replay_matches_native_boundary_and_complete_tape_identity():
    from types import SimpleNamespace
    import stage_eval as sg
    b = _book(minPfStepAutoMin=200, histLookbackBars=600)
    b.by_idx = [st for st in b.by_idx if st.kind == "base" and st.step == 3][:2]
    assert len(b.by_idx) == 2
    a, c = b.by_idx
    ts = [T0 + k * 300 for k in range(130)]
    gross = [.0052] * len(ts)
    now = ((ts[-1] + b.lookback * 60) // 3600 + 1) * 3600
    boundary = now - b.lookback * 60
    for same_symbol, same_entry in ((False, True), (True, False), (True, True)):
        symbols = ["A", "A" if same_symbol else "B"]
        entries = [[t - (60 if i == 0 or same_entry else 120) for t in ts] for i in range(2)]
        tapes = SimpleNamespace(cf=.0012,
            by_set={st.idx:(ts, entries[i], gross) for i, st in enumerate(b.by_idx)},
            symbols_by_set={st.idx:[symbols[i]]*len(ts) for i, st in enumerate(b.by_idx)})
        rows = {st.id:[(t,symbols[i],"LONG",.004,g,t-e,"tp",st.id) for t,e,g in zip(ts,entries[i],gross)]
                for i, st in enumerate(b.by_idx)}
        _learn(b, rows, boundary); expected = dict(b.step_floor_auto)
        assert bool(expected) == (not same_symbol or not same_entry), "only actual copied events deduplicate"
        auto = sg.AutoFloors(b,tapes); auto.apply(b,now)
        assert b.step_floor_auto == expected
    # Trades inside the evaluated lookback cannot teach the floor being used to judge them.
    b.pf_step_auto_min = 1
    in_window = [boundary + k * 60 for k in range(50)]
    tapes = SimpleNamespace(cf=.0012,
        by_set={st.idx:(in_window,[t-30 for t in in_window],[.0052]*len(in_window)) for st in b.by_idx},
        symbols_by_set={st.idx:["A"]*len(in_window) for st in b.by_idx})
    auto = sg.AutoFloors(b,tapes); auto.apply(b,now)
    assert not b.step_floor_auto, "simulation must not learn from the judged window"
    return True, "native/replay match with distinct symbols, distinct entries, exact copies and lookback embargo"



def ratio_midpoints_match_desk_half_up_normalization():
    for k in range(28):
        value=.125+.05*k; expected=round(.15+.05*k,2)
        assert se.step_pf_ratio(value) == expected, (value,se.step_pf_ratio(value),expected)
    assert se.step_pf_ratio(1e300)==1.5 and se.step_pf_ratio(-1e300)==.1
    return True,"28 midpoint ratios snap half-up identically to desk; extreme finite inputs remain bounded"


CHECKS = [("step-pf." + fn.__name__.replace("_", "-"), fn) for fn in (
    ratio_midpoints_match_desk_half_up_normalization,
    auto_wide_steps_use_the_last_table_bucket_but_keep_policy_identity,
    auto_replay_matches_native_boundary_and_complete_tape_identity,
    shipped_micro_floor_is_positive_and_shared_by_every_stage,
    manual_floor_is_one_plus_step_times_ratio, min_pf_step_cap_bounds_every_step_floor_and_the_auto_candidates, min_pf_by_range_replaces_the_step_floor_for_its_range_only, one_floor_for_every_window_and_stage, auto_floor_never_under_its_lower_bound,
    auto_floor_matches_a_plain_reading_and_learns_from_a_real_relation, auto_floor_never_reads_the_window_it_judges,
    copies_count_once_and_thin_evidence_keeps_the_manual_floor, settings_reach_the_engine_and_the_replay_key,
    a_half_percent_step_at_ratio_point_two_asks_exactly_1_1,
    auto_floor_takes_the_lowest_floor_near_the_best_and_falls_back_step_pooled_manual, the_simulator_learns_the_engines_floors)]
