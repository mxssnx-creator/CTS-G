#!/usr/bin/env python3
"""Block coordinations on executed parents: does any way of adding to a parent make the same parents better than leaving them alone?

Two phases, so thousands of coordinations cost seconds once the parents exist:

  parents  one walk-forward execution per window (stage_eval.execute with the overlay's Axis, as live: the engine's gate and rank, one position per
           symbol). Every executed parent is stored with what an add could have seen: each bar of its life that made a new favourable extreme (time,
           move in PositionCosts), whether the parent's own Set and lane were still valid and its Axis allowed at that bar (the engine's precondition
           of an add), and the Set's floor there.
  sweep    the Block v2 rules (block_rules, the module the engine runs) over those parents in time order, for every coordination of a grid: relations
           (each alone, pairs, all-must-agree or any-agrees), count tables (steps, ratios, how many counts only observe), minimum observations, the
           PF threshold ratio, and filters on which parents may be added to (Set type, lane, TP range) and on the room left to the target.
  explore  the unconditional result of an add at each level, by Set type, lane and TP range: what an add is worth before any evidence rule.

Every figure compares the SAME parents without and with the adds, volume weighted as the engine sizes them (a parent 1, an add its ratio x the
add's entry / the base entry). A coordination "works" on a window when the weighted PF with adds is at or above the PF without and the adds' own net
is not negative. Choose on development windows; confirm the finalists once on windows no rule was chosen on (--confirm). At most three days of 1m
history per window.

usage: block_coordination_study.py parents --data DIR --cache FILE --out parents.pkl [--window dev|sealed|all] [--patch JSON] [--jobs 2]
       block_coordination_study.py explore --parents a.pkl [b.pkl ...]
       block_coordination_study.py sweep --parents a.pkl [b.pkl ...] [--grid quick|full] [--confirm c.pkl ...] [--top 25] [--out FILE]
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import pickle
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))

RELATIONS = ("symbol", "indication", "set", "step")


def pf_of(g: float, l_: float) -> Optional[float]:
    return round(g / l_, 4) if l_ > 0 else (99.0 if g > 0 else None)


# ---- phase 1: the executed parents of one window -------------------------------------------------------------------------------------------------------

def build_parents(a) -> int:
    os.environ["STAGE_OVERLAY_PATCH"] = a.patch or "{}"
    import numpy as np
    import stage_eval as sg
    import replay_cache
    import set_engine as se
    import stack_eval as sk
    from coord_engine import Coordinator
    from position_cost import cost_as_frac, tp_range_of
    t0 = time.time()
    overlay = sg.replay_overlay()
    names = sorted(f[:-5] for f in os.listdir(a.data) if f.endswith("-USDT.json"))[:a.symbols]
    manifest = replay_cache.fingerprint(a.data, names, overlay, sg.PULSE, sg.__file__)
    names, bars, times = sg.load_bars(a.data, a.symbols)
    if (max(times[s][-1] for s in names) - min(times[s][0] for s in names)) > 3 * 86400:
        raise ValueError("more than three days of market history: not allowed for a simulation")
    if os.path.exists(a.cache):
        shadow = replay_cache.load(a.cache, manifest)
    else:
        shadow = sg.build(names, bars, times, a.jobs)
        replay_cache.save(a.cache, shadow, manifest)
    shadow = sg.build_hold_auto(names, bars, times, shadow)
    book = sg.make_book()
    tapes = sg.Tapes(book, names, times, shadow)
    cf = cost_as_frac(book.cost_pct)
    start = max(times[s][0] for s in names) + book.lookback * 60
    end = min(times[s][-1] for s in names)
    hours = [float(h) for h in range(((int(start) + 3599) // 3600) * 3600, int(end), 3600)]
    if not hours:
        raise ValueError("no complete report hour remains after the pre-history")
    split = hours[len(hours) * 2 // 3]
    lo, hi = {"dev": (hours[0], split), "sealed": (split, hours[-1] + 3600), "all": (hours[0], hours[-1] + 3600)}[a.window]
    run_hours = [h for h in hours if lo <= h < hi]
    n_chk, bad = sg.self_check(book, tapes, run_hours, samples=40)
    print(f"symbols={len(names)} Sets={len(book.by_idx)} hours={len(run_hours)} ({a.window}) "
          f"{time.strftime('%m-%d %H:%M', time.gmtime(lo))}..{time.strftime('%m-%d %H:%M', time.gmtime(hi))} UTC self-check {n_chk - bad}/{n_chk} [{time.time() - t0:.0f}s]", flush=True)
    if bad:
        return 1
    coord = Coordinator()
    coord.load({}, overlay)
    rows = sg.execute(book, names, bars, times, shadow, tapes, run_hours, refresh_s=0, axis_coord=coord)
    executed = sorted((r for r in rows if "net" in r and lo <= r["t_in"] < hi), key=lambda r: r["t_in"])
    by_id = {st.id: st for st in book.by_idx}
    auto = sg.AutoFloors(book, tapes)
    highs = {s: np.array([b[1] for b in bars[s]]) for s in names}
    lows = {s: np.array([b[2] for b in bars[s]]) for s in names}
    lanes_cache: Dict[Tuple[int, float], Tuple[str, ...]] = {}

    def allowed(st, lane: str, now: float) -> bool:
        """The engine's precondition of an add (Pulse.maybe_set_block_adds): the parent's Set still valid on the parent's lane and its Axis allowing."""
        k = (st.idx, now)
        if k not in lanes_cache:
            auto.apply(book, float(int(now // 3600) * 3600))
            lanes_cache[k] = sk.qualified_parent_lanes(book, tapes, st, now)
        return lane in lanes_cache[k] and sg.axis_judge(book, tapes, coord, st, now, lane)[0]

    out = []
    for r in executed:
        st = by_id[r["set"]]
        tr = r["tr"]
        s, side = r["symbol"], int(tr["dir"])
        entry, exit_px = float(tr["entry_px"]), float(tr["exit_px"])
        e_i, x_i = int(tr["entry_i"]), int(tr["exit_i"])
        fav = highs[s][e_i + 1:x_i] if side > 0 else lows[s][e_i + 1:x_i]
        extremes = []
        best = 0.0
        for k in range(len(fav)):
            move = (float(fav[k]) / entry - 1.0) * side / cf          # the favourable move in PositionCosts
            if move > best + 1e-12:
                best = move
                now = float(times[s][e_i + 1 + k])
                auto.apply(book, float(int(now // 3600) * 3600))
                wn = book.win(st)
                floor = float(book.floor_for(wn.real_neg_n if r["lane"] == "neg" else wn.real_n, st.pack, st))
                extremes.append((now, move, allowed(st, r["lane"], now), floor))
        honor = se.lane_honors_tp(st.kind)
        honor = bool(getattr(book, "hist_honor_tp", True)) if honor is None else honor
        sl_f, tp_f = book.lane_fracs(st)
        out.append({"t_in": float(r["t_in"]), "t": float(r["t"]), "net": float(r["net"]), "symbol": s, "set": st.id, "pack": st.pack, "kind": st.kind,
                    "lane": r["lane"], "side": side, "step": int(st.step), "range": "trail" if st.kind == "trail" else tp_range_of(int(st.step)),
                    "entry": entry, "exit": exit_px, "reason": r["reason"], "honor_tp": honor, "tp_costs": float(tp_f) / cf, "sl_costs": float(sl_f) / cf,
                    "extremes": extremes})
    meta = {"data": os.path.abspath(a.data), "window": a.window, "from": lo, "to": hi, "hours": len(run_hours), "cost_frac": cf, "patch": json.loads(a.patch or "{}"),
            "symbols": len(names), "sets": len(book.by_idx)}
    with open(a.out, "wb") as f:
        pickle.dump({"meta": meta, "parents": out}, f)
    g = sum(p["net"] for p in out if p["net"] > 0)
    l_ = -sum(p["net"] for p in out if p["net"] < 0)
    print(f"{len(out)} parents, PF {pf_of(g, l_)}, net {100 * (g - l_):.2f} %, {sum(1 for p in out if p['extremes'])} with a favourable move -> {a.out} [{time.time() - t0:.0f}s]", flush=True)
    return 0


def load_parents(paths: Sequence[str]) -> List[Dict[str, Any]]:
    out = []
    for p in paths:
        with open(p, "rb") as f:
            d = pickle.load(f)
        d["name"] = os.path.basename(p).rsplit(".", 1)[0]
        out.append(d)
    return out


# ---- phase 2: one coordination on one window's parents -------------------------------------------------------------------------------------------------

def first_reach(extremes, level: float):
    for e in extremes:
        if e[1] + 1e-12 >= level:
            return e
    return None


def simulate(window: Dict[str, Any], cand: Dict[str, Any], state: Optional[Dict[str, Any]] = None,
             capture: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The engine's Block rules over a window's parents, with decisions before tied closes, as stack_eval.block_sim2.
    cand: block_rules overlay keys (blockCounts, blockRelations, ...) plus the coordination's own switches:
      mode       "all" (every selected relation must agree, the engine's rule) or "any" (one agreeing relation is enough)
      kinds / lanes / ranges   the parents an add may go to (Set type, lane, TP range); observations are still collected from every parent
      room       an add on a fixed-target parent needs at least this many PositionCosts left to its target (0: only positive room, the engine's rule)
      share      an add on a fixed-target parent only while its level is at most this share of the target distance (0: no rule)
      net_gate   an add needs every selected relation's cumulative parent result to be at or above zero
      guard      the book's own executed adds must not be in deficit: while their cumulative net is below zero no add is sent; the adds that would
                 have been sent are followed on paper, and adding resumes once those paper adds have earned the deficit back (then both restart at zero)
    state: a dict carried from one window to the next (the evidence ledgers and the guard), as the live book carries them; None = a fresh book."""
    import block_rules as br
    # the three add-removing rules are the engine's own (block_rules): the study's short keys are their overlay keys
    cand = {**cand, **({"blockTargetShare": cand["share"]} if "share" in cand else {}), **({"blockGuard": bool(cand["guard"])} if "guard" in cand else {}),
            **({"blockKinds": cand["kinds"]} if cand.get("kinds") else {})}
    cfg = br.clean_cfg(cand)
    cf = float(window["meta"]["cost_frac"])
    levels = cfg.levels()
    mode = cand.get("mode", "all")
    lanes, ranges = cand.get("lanes"), cand.get("ranges")
    room, net_gate = float(cand.get("room", 0.0)), bool(cand.get("net_gate", False))
    ps = window["parents"]
    ev: List[Tuple[float, int, int, int]] = []
    meta: List[Dict[str, Any]] = []
    for i, r in enumerate(ps):
        hits = []
        for n, lv in enumerate(levels, start=1):
            e = first_reach(r["extremes"], lv)
            if e is None:
                break
            hits.append((n, e[0], lv, e[2], e[3]))
        rk = br.relation_keys(cfg, r["set"], r["lane"], "LONG" if r["side"] > 0 else "SHORT", r["symbol"], r["pack"], 0 if r["kind"] == "trail" else r["step"])
        eligible = br.kind_allowed(cfg, r["kind"]) and (lanes is None or r["lane"] in lanes) and (ranges is None or r["range"] in ranges)
        meta.append({"hits": hits, "rk": rk, "fired": {}, "eligible": eligible})
        ev.append((r["t"], 0, i, 0))
        ev.append((r["t_in"], 1, i, 0))
        for n, now_t, _lv, _ok, _fl in hits:
            ev.append((now_t, 2, i, n))
    ev.sort(key=lambda event: (event[0], {1: 0, 2: 1, 0: 2}[event[1]], event[2], event[3]))
    state = state if state is not None else {}
    ledgers: Dict[str, Dict[str, Any]] = state.setdefault("ledgers", {})
    gs = state.setdefault("guard", br.new_guard())      # executed adds' cumulative net and, while in deficit, the paper adds' net since

    def led(k):
        return ledgers.setdefault(k, {"paper": {}, "executed": {}, "life": br.new_life()})
    bg = bl = wg = wl = ag = al = 0.0
    adds = 0
    for t, order, i, n in ev:
        m, r = meta[i], ps[i]
        if order == 1:
            continue
        if order == 2:
            if not m["eligible"] or not cfg.active(n):
                continue
            _n, _t, lv, ok, floor = m["hits"][n - 1]
            if not ok:
                continue
            books = [led(k) for k in m["rk"]]
            if any(int(b["life"].get("pause", 0)) > 0 for b in books):
                continue
            if net_gate and any(float(b["life"].get("net", 0.0)) < 0 for b in books):
                continue
            fired_ratio = sum(cfg.target_ratio(k) for k in m["fired"])
            if mode == "any":
                passed = any(br.count_passes(cfg, n, [b["paper"].get(str(n))], [b["executed"].get(str(n))], floor, fired_ratio) for b in books)
            else:
                passed = br.count_passes(cfg, n, [b["paper"].get(str(n)) for b in books], [b["executed"].get(str(n)) for b in books], floor, fired_ratio)
            if not passed:
                continue
            lvl = r["entry"] * (1 + r["side"] * lv * cf)
            if r["honor_tp"]:
                target = r["entry"] * (1 + r["side"] * r["tp_costs"] * cf)
                if not br.target_covers_cost(lvl, target, r["side"], cf):        # the engine's rule: positive net room at the target
                    continue
                if room > 0 and (target / lvl - 1) * r["side"] + 1e-12 < room * cf:   # the coordination's own: at least `room` PositionCosts to the target
                    continue
                if not br.share_allows(cfg, r["entry"], lvl, target, r["side"]):       # the level at most its share of the way to the target
                    continue
            add_net_ = (r["exit"] - lvl) / lvl * r["side"] - cf
            if br.guard_holds(cfg, gs):
                m.setdefault("shadow", {})[n] = add_net_          # in deficit: followed on paper, not sent
            else:
                m["fired"][n] = add_net_
            continue
        # close: the parent, its adds, then the evidence and the lifecycle
        net = r["net"]
        if net > 0:
            bg += net
            wg += net
        elif net < 0:
            bl -= net
            wl -= net
        add_part = 0.0
        for n, add_net in m["fired"].items():
            lv = m["hits"][n - 1][2]
            w = br.base_return_contribution(1.0, r["entry"] * (1 + r["side"] * lv * cf), cfg.target_ratio(n), r["entry"])
            v = add_net * w
            adds += 1
            add_part += v
            if v > 0:
                wg += v
                ag += v
            elif v < 0:
                wl -= v
                al -= v
        shadow_part = 0.0
        for n, add_net in (m.get("shadow") or {}).items():
            lv = m["hits"][n - 1][2]
            shadow_part += add_net * br.base_return_contribution(1.0, r["entry"] * (1 + r["side"] * lv * cf), cfg.target_ratio(n), r["entry"])
        gs.update(br.guard_after_close(cfg, gs, add_part, shadow_part))      # the paper adds earning the deficit back lets adding resume
        if capture is not None:
            capture.append({"parent": i, "t": t, "without": net,
                            "with": net + add_part, "adds": len(m["fired"])})
        highest = max(m["fired"]) if m["fired"] else 0
        for k in m["rk"]:
            b = led(k)
            for hn, _now, lv, _ok, _fl in m["hits"]:
                lvl = r["entry"] * (1 + r["side"] * lv * cf)
                br.add_obs(b["paper"].setdefault(str(hn), br.new_record()), (r["exit"] / lvl - 1) * r["side"] - cf)
            for hn, add_net in m["fired"].items():
                br.add_obs(b["executed"].setdefault(str(hn), br.new_record()), add_net)
            b["life"] = br.life_after_close(cfg, b["life"], net + add_part, highest)
    return {"parents": len(ps), "adds": adds, "pfWithout": pf_of(bg, bl), "pfWith": pf_of(wg, wl), "netWithout": 100 * (bg - bl), "netWith": 100 * (wg - wl),
            "addsNet": 100 * (ag - al), "addsPf": pf_of(ag, al), "g": (bg, bl, wg, wl, ag, al)}


def counts_table(n: int, step: float, ratio: float) -> List[Dict[str, Any]]:
    return [{"step": step, "ratio": ratio, "on": True} for _ in range(n)]


def grid(kind: str) -> List[Dict[str, Any]]:
    """The coordinations swept. quick: a few hundred around the shipped table; full: every relation subset and mode with the count tables, evidence rules
    and parent filters crossed."""
    rel_sets = [[r] for r in RELATIONS] + [["symbol", "step"], ["symbol", "indication"], ["indication", "step"], ["set", "symbol"], list(RELATIONS)]
    tables = [(3, 3.0), (3, 2.0), (3, 1.0), (2, 4.0), (2, 6.0), (4, 1.5), (4, 2.0), (6, 1.0), (1, 2.0), (1, 4.0), (1, 6.0)]
    ratios = [1.0, 0.5] if kind == "full" else [1.0]
    skips = [0, 1, 2] if kind == "full" else [0, 1]
    min_obs = [1, 3, 5, 10] if kind == "full" else [1, 3]
    pf_ratios = [0.8, 0.2, 2.0] if kind == "full" else [0.8]
    filters: List[Dict[str, Any]] = [{}, {"kinds": ["trail"]}, {"kinds": ["exit"]}, {"kinds": ["base"]}, {"kinds": ["trail", "exit"]}, {"lanes": ["pos"]}, {"lanes": ["neg"]},
                                     {"ranges": ["long", "wide"]}, {"ranges": ["trail", "long", "wide"]}, {"kinds": ["trail"], "lanes": ["neg"]}, {"kinds": ["trail"], "lanes": ["pos"]}]
    extras: List[Dict[str, Any]] = [{}, {"net_gate": True}, {"room": 3.0}, {"room": 6.0}] if kind == "full" else [{}, {"net_gate": True}]
    out = []
    for rel, (n, step), ratio, skip, mo, pr, flt, ex in itertools.product(rel_sets, tables, ratios, skips, min_obs, pf_ratios, filters, extras):
        if skip >= n:
            continue
        for mode in (("all", "any") if len(rel) > 1 else ("all",)):
            out.append({"blockCounts": counts_table(n, step, ratio), "blockRelations": rel, "blockSkipFirst": skip, "blockMinObservations": mo,
                        "blockProfitFactorRatio": pr, "blockStackType": "additive", "blockCarry": False, "mode": mode, **flt, **ex})
    return out


def structure_grid() -> List[Dict[str, Any]]:
    """Every structural setting of Block v2 crossed, no parent filter: relations and their mode, count tables, volume ratios, Additive and Shared
    stacking, how many counts only observe, minimum observations and the PF threshold ratio."""
    rel_sets = [[r] for r in RELATIONS] + [["symbol", "step"], ["symbol", "indication"], ["indication", "step"], ["set", "symbol"], list(RELATIONS)]
    tables = [(1, 2.0), (1, 4.0), (2, 2.0), (2, 4.0), (3, 1.0), (3, 2.0), (3, 3.0), (4, 2.0), (6, 1.0), (6, 2.0), (12, 1.0)]
    stacks = [("additive", 0.25, 1.0), ("additive", 0.5, 1.0), ("additive", 1.0, 1.0), ("shared", 1.0, 1.0), ("shared", 1.0, 2.0)]
    out = []
    for rel, (n, step), (stack, ratio, total), skip, mo, pr in itertools.product(rel_sets, tables, stacks, [0, 1, 2], [1, 5, 10], [0.2, 0.8, 2.0, 5.0]):
        if skip >= n:
            continue
        for mode in (("all", "any") if len(rel) > 1 else ("all",)):
            out.append({"blockCounts": counts_table(n, step, ratio), "blockRelations": rel, "blockSkipFirst": skip, "blockMinObservations": mo,
                        "blockProfitFactorRatio": pr, "blockStackType": stack, "blockSharedTotal": total, "blockCarry": False, "mode": mode})
    return out


def label(c: Dict[str, Any]) -> str:
    t = c["blockCounts"]
    parts = [("+".join(c["blockRelations"]) + ("/any" if c.get("mode") == "any" and len(c["blockRelations"]) > 1 else "")),
             f"{len(t)}x{t[0]['step']:g}c r{t[0]['ratio']:g} skip{c['blockSkipFirst']}" + (f" shared{c.get('blockSharedTotal', 1):g}" if c.get("blockStackType") == "shared" else ""), f"obs{c['blockMinObservations']} pfr{c['blockProfitFactorRatio']:g}"]
    for k in ("kinds", "lanes", "ranges"):
        if c.get(k):
            parts.append(k[0] + ":" + ",".join(c[k]))
    if c.get("net_gate"):
        parts.append("netgate")
    if c.get("room"):
        parts.append(f"room{c['room']:g}")
    if c.get("share"):
        parts.append(f"share{c['share']:g}")
    if c.get("guard"):
        parts.append("guard")
    return " ".join(parts)


def works(res: Dict[str, Any]) -> bool:
    """With adds at or above without, and the adds' own net not negative (a window without an add neither helps nor hurts)."""
    if not res["adds"]:
        return True
    return (res["pfWith"] or 0.0) + 1e-9 >= (res["pfWithout"] or 0.0) and res["addsNet"] >= -1e-9


def sweep(a) -> int:
    dev = load_parents(a.parents)
    confirm = load_parents(a.confirm or [])
    cands = structure_grid() if a.grid == "structure" else grid(a.grid)
    shipped = {"blockCounts": counts_table(3, 3.0, 1.0), "blockRelations": ["symbol", "step"], "blockSkipFirst": 1, "blockMinObservations": 1,
               "blockProfitFactorRatio": 0.8, "blockStackType": "additive", "blockCarry": False, "mode": "all"}
    t0 = time.time()
    print(f"{len(cands)} coordinations on {len(dev)} development windows: " + ", ".join(f"{w['name']} ({len(w['parents'])} parents)" for w in dev), flush=True)
    rows = []
    for k, c in enumerate([shipped] + cands):
        per = [simulate(w, c) for w in dev]
        g = [sum(r["g"][j] for r in per) for j in range(6)]
        rows.append({"cand": c, "label": ("SHIPPED " if k == 0 else "") + label(c), "per": per, "adds": sum(r["adds"] for r in per),
                     "pooledWithout": pf_of(g[0], g[1]), "pooledWith": pf_of(g[2], g[3]), "addsNet": 100 * (g[4] - g[5]), "addsPf": pf_of(g[4], g[5]),
                     "worksOn": sum(1 for r in per if works(r)), "windowsWithAdds": sum(1 for r in per if r["adds"])})
    print(f"swept [{time.time() - t0:.0f}s]", flush=True)
    n_w = len(dev)
    with_adds = [r for r in rows if r["adds"] > 0]
    ok_all = [r for r in with_adds if r["worksOn"] == n_w and r["windowsWithAdds"] >= max(1, n_w - 1) and r["adds"] >= a.min_adds]
    print(f"\ncoordinations with at least one add: {len(with_adds)} of {len(rows)}; adds' net positive pooled: {sum(1 for r in with_adds if r['addsNet'] > 0)}; "
          f"at or above 'without' on every development window with adds on at least {max(1, n_w - 1)} of them and {a.min_adds}+ adds: {len(ok_all)}")

    def show(r):
        cells = "  ".join(f"{(x['pfWith'] or 0):.3f}/{(x['pfWithout'] or 0):.3f} a{x['adds']:<3d}{x['addsNet']:+6.1f}" for x in r["per"])
        print(f"  {r['label'][:74]:74s} | {cells} | pooled {r['pooledWith']}/{r['pooledWithout']} adds {r['adds']} net {r['addsNet']:+.1f} PF {r['addsPf']}")
    print("\nwindow columns: PF with / PF without, a = adds, the adds' net in % of one base position")
    print("shipped:")
    show(rows[0])
    finalists = sorted(ok_all, key=lambda r: -r["addsNet"])[:a.top]
    print(f"\ntop {len(finalists)} by the adds' pooled net among those that work on every development window:")
    for r in finalists:
        show(r)
    if not finalists:
        print("  none. Best by the adds' pooled net regardless:")
        for r in sorted(with_adds, key=lambda r: -r["addsNet"])[:a.top]:
            show(r)
    out = {"development": [w["meta"] for w in dev], "candidates": len(rows), "withAdds": len(with_adds), "worksEverywhere": len(ok_all),
           "shipped": {k: v for k, v in rows[0].items() if k != "cand"}, "finalists": [{k: v for k, v in r.items()} for r in finalists]}
    if confirm and finalists:
        print(f"\nconfirmation on {len(confirm)} windows no coordination was chosen on: " + ", ".join(f"{w['name']} ({len(w['parents'])} parents)" for w in confirm))
        held = 0
        for r in finalists:
            per = [simulate(w, r["cand"]) for w in confirm]
            g = [sum(x["g"][j] for x in per) for j in range(6)]
            r["confirm"] = {"per": per, "adds": sum(x["adds"] for x in per), "pooledWithout": pf_of(g[0], g[1]), "pooledWith": pf_of(g[2], g[3]),
                            "addsNet": 100 * (g[4] - g[5]), "worksOn": sum(1 for x in per if works(x))}
            ok = r["confirm"]["worksOn"] == len(confirm) and r["confirm"]["adds"] > 0 and r["confirm"]["addsNet"] > 0
            held += ok
            cells = "  ".join(f"{(x['pfWith'] or 0):.3f}/{(x['pfWithout'] or 0):.3f} a{x['adds']:<3d}{x['addsNet']:+6.1f}" for x in per)
            print(f"  {'HOLDS ' if ok else 'fails '}{r['label'][:68]:68s} | {cells} | adds {r['confirm']['adds']} net {r['confirm']['addsNet']:+.1f}")
        shipped_c = [simulate(w, shipped) for w in confirm]
        print("  shipped on the confirmation windows: " + "  ".join(f"{(x['pfWith'] or 0):.3f}/{(x['pfWithout'] or 0):.3f} a{x['adds']} {x['addsNet']:+.1f}" for x in shipped_c))
        print(f"\n{held} of {len(finalists)} finalists hold on the confirmation windows (at or above 'without' on each, adds' net positive)")
        out["confirmation"] = [w["meta"] for w in confirm]
        out["held"] = held
    if a.out:
        def clean(o):
            if isinstance(o, dict):
                return {k: clean(v) for k, v in o.items() if k != "g"}
            if isinstance(o, (list, tuple)):
                return [clean(x) for x in o]
            return o
        with open(a.out, "w") as f:
            json.dump(clean(out), f, indent=1)
    return 0


def explore(a) -> int:
    """What an add is worth before any evidence rule: every parent that reached a level, an add there to the parent's exit (one PositionCost)."""
    wins = load_parents(a.parents)
    levels = [1, 2, 3, 4, 6, 8, 10, 14]
    for dim in ("all", "kind", "lane", "range", "reason"):
        print(f"\nby {dim}: per level (PositionCosts in favour) the adds' PF / count / mean net %, pooled over " + ", ".join(w["name"] for w in wins))
        groups: Dict[str, Dict[int, List[float]]] = {}
        for w in wins:
            cf = float(w["meta"]["cost_frac"])
            for r in w["parents"]:
                key = "all" if dim == "all" else str(r[dim]).split(":")[0]
                for lv in levels:
                    e = first_reach(r["extremes"], lv)
                    if e is None:
                        break
                    lvl = r["entry"] * (1 + r["side"] * lv * cf)
                    groups.setdefault(key, {}).setdefault(lv, []).append((r["exit"] / lvl - 1) * r["side"] - cf)
        for key in sorted(groups):
            cells = []
            for lv in levels:
                v = groups[key].get(lv, [])
                if not v:
                    cells.append("      -      ")
                    continue
                g, l_ = sum(x for x in v if x > 0), -sum(x for x in v if x < 0)
                cells.append(f"{(pf_of(g, l_) or 0):5.2f}/{len(v):3d}/{100 * sum(v) / len(v):+5.2f}")
            print(f"  {key[:10]:10s} " + "  ".join(f"L{lv}: {c}" for lv, c in zip(levels, cells)))
    print("\nper window, every parent, level 3 and 6:")
    for w in wins:
        cf = float(w["meta"]["cost_frac"])
        for lv in (3, 6):
            v = []
            for r in w["parents"]:
                e = first_reach(r["extremes"], lv)
                if e is not None:
                    lvl = r["entry"] * (1 + r["side"] * lv * cf)
                    v.append((r["exit"] / lvl - 1) * r["side"] - cf)
            g, l_ = sum(x for x in v if x > 0), -sum(x for x in v if x < 0)
            print(f"  {w['name']:14s} L{lv}: PF {pf_of(g, l_)} on {len(v)} adds, net {100 * sum(v):+.2f} %")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("parents")
    p.add_argument("--data", required=True)
    p.add_argument("--cache", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--window", choices=("dev", "sealed", "all"), default="dev")
    p.add_argument("--patch", default="")
    p.add_argument("--symbols", type=int, default=20)
    p.add_argument("--jobs", type=int, default=2)
    e = sub.add_parser("explore")
    e.add_argument("--parents", nargs="+", required=True)
    s = sub.add_parser("sweep")
    s.add_argument("--parents", nargs="+", required=True)
    s.add_argument("--confirm", nargs="*", default=[])
    s.add_argument("--grid", choices=("quick", "full", "structure"), default="quick")
    s.add_argument("--top", type=int, default=25)
    s.add_argument("--min-adds", type=int, default=20)
    s.add_argument("--out", default="")
    a = ap.parse_args()
    return {"parents": build_parents, "explore": explore, "sweep": sweep}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
