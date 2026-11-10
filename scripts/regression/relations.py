"""Relation lanes (server/pulse/relation_lanes.py): last-N lanes of the post-Base orders by relation, next to the per-configuration lanes.

Every check here reads the lanes against something that does not share their code path: a plain reading of the rules (dict and list loops, each
verdict recomputed from the rows closed before it), the walk-forward simulator, or the same replay with the lanes off."""
from __future__ import annotations

import json
import math
import os
import random
import sys

from common import PULSE, ROOT, SMALL, T0, overlay, synth_bars

import relation_lanes as rl
import set_engine as se

LANES = {"enabled": True, "mainN": 6, "realN": 3, "minPf": 1.05}


def _overlay(lanes=LANES, **extra):
    return overlay(**{**SMALL, "holdEnabled": False, "directionMode": "split", "setMinPf": 1.02, "setGateWindow": 10, "setGateShortN": 0,
                      "mainGateN": 6, "mainShortEnabled": False, "realGateN": 3, "histLookbackBars": 600, "histLookbackAuto": False,
                      "stratShortRange": False, "positionCostPct": 0.03,       # a low fixture cost: on random walks at 0.12 % no post-Base lane passes at all
                      **({"relationLanes": lanes} if lanes is not None else {}), **extra})


def _book(n_syms: int = 3, bars: int = 900, lanes=LANES, **extra):
    b = se.SetBook()
    b.fixture_overlay = _overlay(lanes, **extra)
    b.load(dict(b.fixture_overlay))
    for i in range(n_syms):
        s = f"S{i}-USDT"
        b.bars[s] = synth_bars(7 + i, bars, drift=0.002 if i % 2 == 0 else -0.002, vol=0.002)
        b.bar_t[s] = T0 - 60
    return b


def _plain_lane(rows, main_n, real_n, floor, full=False):
    """A relation lane read plainly from its member orders (exit t, entry t, key, net): for every entry its Main verdict is recomputed from scratch on
    the orders closed strictly before it; an observation is one entry (the mean net of its closed orders), ordered by its latest close, then its key."""
    def observations(subset, before=None):
        by = {}
        for exit_t, _entry_t, key, net in subset:
            if before is not None and not exit_t < before:
                continue
            g = by.setdefault(key, [0.0, 0, -math.inf])
            g[0] += net
            g[1] += 1
            g[2] = max(g[2], exit_t)
        return [g[0] / g[1] for _key, g in sorted(by.items(), key=lambda kv: (kv[1][2], kv[0]))]

    def window(values, n):
        w = values[-n:] if n else []
        gp = sum(v for v in w if v > 1e-12)
        gl = -sum(v for v in w if v < -1e-12)
        pf = gp / gl if gl > 0 else (99.0 if gp > 0 else (1.0 if w else 0.0))
        return len(w), gp, gl, (len(w) >= (n if full else 1) and pf + 1e-9 >= floor)

    opened = {}
    for _x, entry_t, key, _n in rows:
        opened[key] = min(opened.get(key, math.inf), entry_t)
    flagged = {key: window(observations(rows, before=t), main_n)[3] for key, t in opened.items()}
    mh, mg, ml, main_ok = window(observations(rows), main_n)
    real_rows = [r for r in rows if flagged[r[2]]]
    out = {"entries": len(opened), "orders": len(rows), "mainHave": mh, "mainGp": mg, "mainGl": ml, "mainOk": main_ok,
           "realEntries": len({r[2] for r in real_rows}), "ok": main_ok}
    if real_n:
        rh, rg, rl_, real_ok = window(observations(real_rows), real_n)
        out.update(realHave=rh, realGp=rg, realGl=rl_, realOk=real_ok, ok=main_ok and real_ok)
    return out, flagged


def _same(a, b):
    for k, v in b.items():
        if isinstance(v, float):
            if abs(a[k] - v) > 1e-9:
                return False
        elif a[k] != v:
            return False
    return True


def lane_state_equals_a_plain_reading_on_random_orders():
    """relation_lanes.lane_state (one time-ordered walk, entries judged in bursts, a move-to-end order) against the plain reading on random member orders:
    several members per entry, shared close instants, entries stamped at a close instant, every window size and both window rules. The Real Set's
    membership (the trace) is compared entry by entry."""
    rnd = random.Random(11)
    cases = bad = 0
    for trial in range(120):
        n_entries = rnd.randint(1, 30)
        rows, t = [], 1000.0
        for e in range(n_entries):
            t += rnd.choice([0.0, 60.0, 60.0, 120.0, 600.0])
            for _member in range(rnd.randint(1, 4)):
                rows.append((t + 60.0 * rnd.randint(1, 12), t, ("S%d" % rnd.randint(0, 2), t), rnd.choice([-0.004, -0.002, -0.001, 0.0, 0.002, 0.003, 0.006])))
        rnd.shuffle(rows)                                             # the order the rows arrive in must not matter
        for main_n, real_n, floor, full in ((1, 1, 1.05, False), (3, 2, 1.1, False), (6, 3, 1.02, False), (5, 0, 1.25, False), (4, 4, 1.1, True)):
            cfg = rl.config_of({"enabled": True, "mainN": main_n, "realN": real_n, "minPf": floor, "fullWindow": full})
            trace = {}
            got = rl.lane_state([(x, e, k, n, 1) for x, e, k, n in rows], cfg, trace)
            want, flagged = _plain_lane(rows, main_n, real_n, floor, full)
            cases += 1
            if not _same(got, want) or trace != flagged:
                bad += 1
    return bad == 0 and cases == 600, f"{cases - bad}/{cases} random lanes equal the plain reading (figures and Real-Set membership)"


def one_entry_counts_once_and_later_closes_never_move_an_earlier_entry():
    """(1) A member Set copied k times (the same orders) leaves every lane figure where it was: an entry is one observation however many Sets took it.
    (2) Causality: orders that close after an instant T never change the Main verdict of an entry opened up to T (the Relation Real Set holds no hindsight)."""
    rnd = random.Random(5)
    rows, t = [], 0.0
    for _e in range(40):
        t += 120.0
        rows.append((t + 60.0 * rnd.randint(1, 20), t, ("S", t), rnd.choice([-0.003, 0.002, 0.004])))
    cfg = rl.config_of({"enabled": True, "mainN": 5, "realN": 3, "minPf": 1.1})
    one = rl.lane_state([(x, e, k, n, 1) for x, e, k, n in rows], cfg)
    acc = rl.Accumulator()
    cell = rl.cell_of("p", 1, "base", 4, 4)
    for _copy in range(7):
        acc.add(cell, [(x, e, k[0], n) for x, e, k, n in rows])
    seven = acc.lanes(cfg)["rel:p:L:indication"]
    copies_ok = seven["orders"] == 7 * one["orders"] and seven["sets"] == 7 and _same(seven, {k: v for k, v in one.items() if k != "orders"})
    causal_bad = 0
    for cut in (600.0, 1500.0, 2400.0, 3300.0):
        before, full = {}, {}
        rl.lane_state([(x, e, k, n, 1) for x, e, k, n in rows if x < cut], cfg, before)
        rl.lane_state([(x, e, k, n, 1) for x, e, k, n in rows], cfg, full)
        # an entry seen at the cut was judged on the closes before its own entry; whatever closes later, that verdict stands
        causal_bad += sum(1 for k, v in before.items() if full[k] != v)
    return copies_ok and causal_bad == 0, f"seven copies of a member: {seven['entries']} entries, Main PF {seven['mainPf']} (one copy {one['mainPf']}); causality violations {causal_bad}"


def _capture(b):
    """Run the book's replay and keep what each Set's chain was built from (the engine's own tape rows) for an independent reading."""
    seen = {}
    inner = b.set_processed

    def spy(st, full, now=None, relations=None):
        seen[st.id] = [tuple(r) for r in full]
        return inner(st, full, now=now, relations=relations)
    b.set_processed = spy
    b.replay_all(now=T0)
    return seen


def engine_lanes_equal_a_plain_reading_of_the_post_base_orders():
    """The lanes SetBook publishes after a replay against a plain reading of the same pass: each Set's Main Set from stage_chain on the tape rows the engine
    scored (the orders it opened while its own Base windows passed, inside the lookback), grouped by the relation keys spelled out here, each lane read by
    the plain rule. Every lane, every figure; the fixture must hold passing and failing lanes, lanes with a Real Set, and entries shared by several Sets."""
    b = _book()
    seen = _capture(b)
    cf = b.cost_pct / 100.0
    cutoff = T0 - b.lookback * 60.0
    by = {}
    members = {}
    for st in b.by_idx:
        full = seen[st.id]
        ts = [r[0] for r in full]
        ents = [r[0] - r[5] for r in full]
        nets = [r[4] - cf for r in full]
        ch = se.stage_chain(ts, ents, nets, b.stage_params(st.pack, st), window_start=cutoff)
        side = "L" if st.side > 0 else "S" if st.side < 0 else "B"
        rng = "trail" if st.kind == "trail" else se.tp_range_of(st.step)
        step = ("arm%d" % round(st.trail_arm * 10000)) if st.kind == "trail" else "st%d" % st.step
        ids = [f"rel:*:{side}:overall", f"rel:{st.pack}:{side}:indication", f"rel:{st.pack}:{side}:type:{st.kind}", f"rel:{st.pack}:{side}:range:{rng}",
               f"rel:{st.pack}:{side}:step:{step}"]
        for lane_id in ids:
            members[lane_id] = members.get(lane_id, 0) + 1
            by.setdefault(lane_id, []).extend((ts[k], ents[k], (full[k][1], ents[k]), nets[k]) for k in ch["main"])
    got = b.relation_state
    bad = [lane_id for lane_id in set(by) | set(got)
           if lane_id not in got or lane_id not in by or got[lane_id]["sets"] != members[lane_id]
           or not _same(got[lane_id], _plain_lane(by[lane_id], LANES["mainN"], LANES["realN"], LANES["minPf"])[0])]
    passing = sum(1 for v in got.values() if v["ok"])
    shared = sum(1 for v in got.values() if v["orders"] > v["entries"])
    with_real = sum(1 for v in got.values() if v["realEntries"])
    ok = not bad and len(got) >= 20 and 0 < passing < len(got) and shared > 0 and with_real > 0 and b.relation_at == T0 and b.progress.errors == 0
    return ok, f"{len(got)} lanes ({passing} passing, {with_real} with a Real Set, {shared} with entries shared by several Sets); differing: {bad[:3]}"


def relation_lanes_never_change_a_set_verdict_and_are_off_by_default():
    """The lanes read the chains; they must not move one. The same replay with the lanes on and off gives every Set the same verdict, lane, windows and
    tape signature. Off (the default, and an overlay without the key): no lane is built, every multiple is 1, the report says so."""
    on, off = _book(), _book(lanes=None)
    on.replay_all(now=T0)
    off.replay_all(now=T0)

    def view(b):
        return [(st.id, b.gate_reason(st), b.lane_of(st), st.gate_n, round(st.gate_pf, 9), st.proc_total, st.exe_total, st.neg_total, st.tape_sig,
                 json.dumps(st.stage, sort_keys=True, default=str)) for st in b.by_idx]
    same = view(on) == view(off)
    st = off.by_idx[0]
    rep = off.relation_report()
    off_ok = (not off.relation_cfg.enabled and off.relation_state == {} and off.relation_mult(st, "pos") == (1.0, []) and rep["lanes"] == 0
              and rep["config"]["enabled"] is False and "relation lanes" not in off.progress.detail)
    explicit = _book(lanes={"enabled": False, "mainN": 6})
    explicit.replay_all(now=T0)
    shipped = {name: json.load(open(os.path.join(PULSE, name))).get("relationLanes") for name in sorted(os.listdir(PULSE)) if name.startswith("overlay-") and name.endswith(".json")}
    shipped_ok = all(v is None or (isinstance(v, dict) and (v.get("enabled") is True) == rl.config_of(v).enabled) for v in shipped.values())
    return same and off_ok and explicit.relation_state == {} and shipped_ok and "relation lanes pass" in on.progress.detail, \
        f"{len(on.by_idx)} Sets identical with the lanes on and off: {same}; off builds {len(off.relation_state)} lanes; shipped overlays {shipped}"


def the_multiple_follows_the_passing_lanes_of_the_sets_own_relations():
    """SetBook.relation_mult: 1 + the ratios of the Set's own relation lanes that pass, at most maxMult; a lane of another direction, indication, type, range or
    step never raises it; the Real Negative lane is never raised; a ratio or ceiling change applies at once, a change of what the lanes are (windows, floor,
    relations) drops them until the next pass; a rebuilt grid drops them too."""
    b = _book(lanes={**LANES, "relations": {"overall": 0.25, "indication": 0.5, "type": 0.75, "range": 0.1, "step": 0.2}, "maxMult": 5})
    b.replay_all(now=T0)
    cfg = b.relation_cfg
    bad = []
    seen_mults = set()
    for st in b.by_idx:
        ids = rl.keys_of(b.relation_cell(st))
        want = 1.0 + sum(cfg.ratio(r) for r in rl.RELATIONS if b.relation_state.get(ids[r], {}).get("ok"))
        got, why = b.relation_mult(st, "pos")
        seen_mults.add(round(got, 4))
        if abs(got - want) > 1e-12 or why != [r for r in rl.RELATIONS if b.relation_state.get(ids[r], {}).get("ok")] or b.relation_mult(st, "neg") != (1.0, []):
            bad.append(st.id)
    raised = next(st for st in b.by_idx if b.relation_mult(st, "pos")[0] > 1.0)
    before = b.relation_mult(raised, "pos")[0]
    ov = _overlay(None)
    b.load({**ov, "relationLanes": {**LANES, "relations": {"overall": 0.25, "indication": 0.5, "type": 0.75, "range": 0.1, "step": 0.2}, "maxMult": 1.1}})
    capped = b.relation_mult(raised, "pos")[0]                         # the ceiling alone changed: the held lanes still answer, capped
    b.load({**ov, "relationLanes": {**LANES, "mainN": 7}})
    dropped = (b.relation_state == {} and b.relation_mult(raised, "pos") == (1.0, []))     # other windows: the old lanes are not these lanes
    return not bad and len(seen_mults) >= 3 and before > 1.1 and abs(capped - 1.1) < 1e-12 and dropped, \
        f"multiples seen {sorted(seen_mults)}; mismatching Sets {bad[:3]}; ceiling 1.1 -> {capped}; lanes dropped after a window change: {dropped}"


def configured_variants_are_one_indication_and_every_set_belongs_to_five_lanes():
    """A configured indication entry's variants (its Last-N profiles and symbols: packs "<name>~cfg...") pool into the lanes of that indication; trailing
    Sets are keyed by their arm and the "trail" range; every Set belongs to exactly one lane per relation and the ids never collide with a Set id."""
    a = rl.cell_of("extra.momchg_n5_t1~cfg0a1b", 1, "base", 7, 7)
    c = rl.cell_of("extra.momchg_n5_t1~cfg9f9f", 1, "base", 7, 7)
    tr = rl.cell_of("general", -1, "trail", 9, 3, 0.003)
    b = _book(lanes=LANES)
    ids_ok = True
    for st in b.by_idx:
        ids = rl.keys_of(b.relation_cell(st))
        ids_ok = ids_ok and len(set(ids.values())) == 5 and set(ids) == set(rl.RELATIONS) and not any(i in b.sets for i in ids.values()) and all(i.startswith("rel:") for i in ids.values())
    return (a == c and rl.keys_of(a)["indication"] == "rel:extra.momchg_n5_t1:L:indication" and rl.keys_of(a)["overall"] == "rel:*:L:overall"
            and tr[3] == "trail" and tr[4] == "arm30" and rl.keys_of(tr)["step"] == "rel:general:S:step:arm30" and ids_ok), f"cells {a} {tr}; five distinct lane ids per Set: {ids_ok}"


def the_simulator_builds_the_engines_lanes():
    """scripts/stage_eval.relation_lanes_at (the walk-forward simulator's tapes and chain) gives the lanes the engine publishes after its own replay of the
    same bars at the same instant: every lane and figure. The study and the engine therefore read one definition."""
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import stage_eval as sg
    b = _book()
    b.replay_all(now=T0)
    names = sorted(b.bars)
    bars = {s: b.bars[s] for s in names}
    times = {s: [int(T0) - 60 * (len(bars[s]) - k) for k in range(len(bars[s]))] for s in names}
    saved = sg.make_book

    def make():
        m = se.SetBook()
        m.load(dict(b.fixture_overlay))
        return m
    try:
        sg.make_book = make
        shadow = {}
        for s in names:
            _, warm, dirs, confs, out = sg._symbol_job((s, bars[s], times[s][0]))
            shadow[s] = (warm, dirs, confs, out)
    finally:
        sg.make_book = saved
    book2 = make()
    tapes = sg.Tapes(book2, names, times, shadow)
    sim = sg.relation_lanes_at(book2, tapes, T0)
    eng = b.relation_state
    bad = [k for k in set(sim) | set(eng) if k not in sim or k not in eng or not _same(sim[k], eng[k])]
    return not bad and len(eng) >= 20 and any(v["ok"] for v in eng.values()), f"{len(eng)} engine lanes, {len(sim)} simulator lanes, differing {bad[:3]}"


def progress_follows_every_stage_and_the_relation_lanes():
    """The progress history holds one point per scoring pass with every stage on its own (Sets passing Base / Main / Real / Real Negative: count, share, PF of
    their own window) and, with the lanes on, the relation lanes built and passing per relation; with the lanes off the point carries no relation figures.
    The counts are the snapshot's own."""
    b = _book()
    b.replay_all(now=T0)
    snap = b.snapshot()
    point = b.pf_history[-1]
    stages = snap["pfBase"]["stages"]
    rel = snap["relations"]
    stages_ok = all(point["stages"][k]["sets"] == stages[k]["sets"] and point["stages"][k]["pct"] == stages[k]["pct"] for k in ("base", "main", "real", "realNeg", "valid"))
    rel_ok = (point["relations"]["lanes"] == rel["lanes"] == len(b.relation_state) and point["relations"]["passing"] == rel["passing"]
              and all(point["relations"]["by"][r] == [rel["byRelation"][r]["ok"], rel["byRelation"][r]["lanes"]] for r in rl.RELATIONS)
              and sum(v["lanes"] for v in rel["byRelation"].values()) == rel["lanes"] and len(rel["rows"]) == rel["lanes"] and rel["rowsOmitted"] == 0
              and rel["rows"] == sorted(rel["rows"], key=lambda r: (not r["ok"], rl.RELATIONS.index(r["relation"]), -r["entries"], r["id"])))
    off = _book(lanes=None)
    off.replay_all(now=T0)
    return stages_ok and rel_ok and "relations" not in off.pf_history[-1] and "stages" in off.pf_history[-1], \
        f"point stages {point['stages']}; relations {point['relations']}"


def settings_agree_between_the_engine_the_desk_and_the_shipped_files():
    """relationLanes: the engine's defaults (relation_lanes.config_of), the desk's (src/lib/relation-lanes.ts) and what the shipped overlays and presets carry
    are one table; bounds hold (N 1..200, Real N 0 = no Real stage, floor at least MIN_PF_FLOOR, ratio 0..2, ceiling 1..5); only an explicit true enables."""
    from position_cost import MIN_PF_FLOOR
    d = rl.config_of({"enabled": True}).as_dict()
    ts = open(os.path.join(ROOT, "src", "lib", "relation-lanes.ts")).read()
    desk_ok = (f'RELATIONS = [{", ".join(chr(34) + r + chr(34) for r in rl.RELATIONS)}] as const' in ts
               and all(f"{r}: {{ on: true, ratio: {d['relations'][r]['ratio']:g} }}" in ts for r in rl.RELATIONS)
               and f"mainN: {d['mainN']}," in ts and f"realN: {d['realN']}," in ts and f"minPf: {d['minPf']:g}," in ts and f"maxMult: {d['maxMult']:g}," in ts
               and f"RELATION_PF_FLOOR = {MIN_PF_FLOOR:g};" in ts and f"RELATION_RATIO_MAX = {rl.RATIO_MAX:g};" in ts and f"RELATION_MULT_MAX = {rl.MULT_MAX:g};" in ts
               and all(f'{r}: "{rl.ROLE[r]}"' in ts for r in rl.RELATIONS))
    bounds = rl.config_of({"enabled": "yes", "mainN": 999, "realN": -3, "minPf": 0.5, "maxMult": 0, "relations": {"overall": -1, "step": {"on": False, "ratio": 7}}})
    bounds_ok = (not bounds.enabled and bounds.main_n == 200 and bounds.real_n == 0 and bounds.min_pf == MIN_PF_FLOOR and bounds.max_mult == 1.0
                 and bounds.ratio("overall") == 0.0 and bounds.ratio("step") == rl.RATIO_MAX and "step" not in bounds.on)
    files = [os.path.join(PULSE, n) for n in sorted(os.listdir(PULSE)) if n.startswith("overlay-") and n.endswith(".json")] + \
            [os.path.join(ROOT, "presets", n) for n in sorted(os.listdir(os.path.join(ROOT, "presets"))) if n.endswith(".json")]
    carried = {os.path.basename(f): json.load(open(f)).get("relationLanes") for f in files}
    files_ok = all(v is None or rl.config_of(v).as_dict() == {**d, "enabled": v.get("enabled") is True} for v in carried.values())
    return desk_ok and bounds_ok and files_ok, f"desk={desk_ok} bounds={bounds_ok} shipped={ {k: (v or {}).get('enabled') for k, v in carried.items()} }"


CHECKS = [
    ("relations.lane-state-equals-a-plain-reading-on-random-orders", lane_state_equals_a_plain_reading_on_random_orders),
    ("relations.one-entry-counts-once-and-later-closes-never-move-an-earlier-entry", one_entry_counts_once_and_later_closes_never_move_an_earlier_entry),
    ("relations.engine-lanes-equal-a-plain-reading-of-the-post-base-orders", engine_lanes_equal_a_plain_reading_of_the_post_base_orders),
    ("relations.lanes-never-change-a-set-verdict-and-are-off-by-default", relation_lanes_never_change_a_set_verdict_and_are_off_by_default),
    ("relations.the-multiple-follows-the-passing-lanes-of-the-sets-own-relations", the_multiple_follows_the_passing_lanes_of_the_sets_own_relations),
    ("relations.configured-variants-are-one-indication-and-every-set-belongs-to-five-lanes", configured_variants_are_one_indication_and_every_set_belongs_to_five_lanes),
    ("relations.the-simulator-builds-the-engines-lanes", the_simulator_builds_the_engines_lanes),
    ("relations.progress-follows-every-stage-and-the-relation-lanes", progress_follows_every_stage_and_the_relation_lanes),
    ("relations.settings-agree-between-the-engine-the-desk-and-the-shipped-files", settings_agree_between_the_engine_the_desk_and_the_shipped_files),
]
