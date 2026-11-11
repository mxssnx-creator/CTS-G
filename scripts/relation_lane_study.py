#!/usr/bin/env python3
"""Relation lanes measured on executed positions (walk-forward, at most three days of 1m history).

The relation lanes (server/pulse/relation_lanes.py) change no entry: they only weight an executed position by 1 + the ratios of its
Set's relation lanes (overall / indication / type / range / step) that pass their own last-N windows on the post-Base orders. So one walk-forward
execution (stage_eval.execute, the engine's gate and ranking, one position per symbol) gives the positions, and every arm (windows,
floor, ratios) is read on those same positions:

  unit PF           gains over losses, every position at volume 1 (what the engine executes with the lanes off)
  weighted PF       the same with each position at its multiple (USDT-like; never mix it with the unit PF)
  raised / base     how many positions were raised, and the unit PF of the raised positions next to the others: a ratio only helps
                    when the raised positions are the better ones
  net per volume    weighted net / total volume against the unit net per position: an edge, not just more size
  max drawdown      of the cumulative weighted net, next to the unit one

The lanes are rebuilt every --every seconds (the engine rebuilds them in each scoring pass) from each Set's Main-Set orders closed
before that instant inside the lookback; a sample of instants is compared with stage_eval.relation_lanes_at (the engine's own path).
Choose on the development window (the first two thirds); --sealed reads the last third once, for the finalists.

usage: relation_lane_study.py --data DIR --cache shadow.pkl [--symbols 20] [--patch JSON] [--axis-current] [--sealed] [--arms JSON] [--out FILE]
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))

DEFAULT_ARMS = [
    {"name": "shipped", "relationLanes": {"enabled": True}},
    {"name": "main10-real5", "relationLanes": {"enabled": True, "mainN": 10, "realN": 5}},
    {"name": "main50-real25", "relationLanes": {"enabled": True, "mainN": 50, "realN": 25}},
    {"name": "main25-noreal", "relationLanes": {"enabled": True, "mainN": 25, "realN": 0}},
    {"name": "floor1.02", "relationLanes": {"enabled": True, "minPf": 1.02}},
    {"name": "floor1.25", "relationLanes": {"enabled": True, "minPf": 1.25}},
    {"name": "floor1.5", "relationLanes": {"enabled": True, "minPf": 1.5}},
]
SINGLE = ("overall", "indication", "type", "range", "step")


def pf_of(g, l_):
    return round(g / l_, 3) if l_ > 0 else (99.0 if g > 0 else None)


def figures(pairs):
    """pairs: (net, volume). PF, net, count, volume and the largest peak-to-trough of the cumulative weighted net."""
    g = sum(n * v for n, v in pairs if n > 0)
    l_ = -sum(n * v for n, v in pairs if n < 0)
    peak = cum = dd = 0.0
    for n, v in pairs:
        cum += n * v
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    vol = sum(v for _n, v in pairs)
    return {"n": len(pairs), "pf": pf_of(g, l_), "netPct": round(100 * (g - l_), 3), "volume": round(vol, 3),
            "netPerVolumePct": round(100 * (g - l_) / vol, 4) if vol > 0 else None, "maxDdPct": round(100 * dd, 3)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--patch", default="", help="overlay patch (JSON) applied to the x01 overlay")
    ap.add_argument("--axis-current", action="store_true", help="apply the overlay's Axis rules to every entry, as live")
    ap.add_argument("--sealed", action="store_true", help="read the sealed window (finalists only, once)")
    ap.add_argument("--every", type=int, default=600, help="seconds between two rebuilds of the relation lanes")
    ap.add_argument("--arms", default="", help="JSON list of {name, relationLanes} read on the same positions")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    if a.patch:
        os.environ["STAGE_OVERLAY_PATCH"] = a.patch
    import stage_eval as sg  # noqa: E402  (reads STAGE_OVERLAY_PATCH)
    import replay_cache  # noqa: E402
    import relation_lanes as rl  # noqa: E402
    import set_engine as se  # noqa: E402
    t0 = time.time()
    book = sg.make_book()
    names = sorted(f[:-5] for f in os.listdir(a.data) if f.endswith("-USDT.json"))[:a.symbols]
    overlay = sg.replay_overlay()
    manifest = replay_cache.fingerprint(a.data, names, overlay, sg.PULSE, sg.__file__)
    _names, bars, times = sg.load_bars(a.data, a.symbols)
    if (max(times[s][-1] for s in names) - min(times[s][0] for s in names)) > 3 * 86400:
        raise ValueError("more than three days of market history: not allowed for a simulation")
    if os.path.exists(a.cache):
        shadow = replay_cache.load(a.cache, manifest)
    else:
        shadow = sg.build(names, bars, times, a.jobs)
        replay_cache.save(a.cache, shadow, manifest)
    shadow = sg.build_hold_auto(names, bars, times, shadow)
    tapes = sg.Tapes(book, names, times, shadow)
    lb = book.lookback * 60.0
    start = max(times[s][0] for s in names) + lb
    end = min(times[s][-1] for s in names)
    hours = [float(h) for h in range(((int(start) + 3599) // 3600) * 3600, int(end), 3600)]
    if not hours:
        raise ValueError("no complete report hour remains after the pre-history")
    split = hours[len(hours) * 2 // 3]
    lo, hi = (split, hours[-1] + 3600) if a.sealed else (hours[0], split)
    run_hours = [h for h in hours if lo <= h < hi]
    n_chk, bad = sg.self_check(book, tapes, run_hours, samples=40)
    print(f"symbols={len(names)} sets={len(book.by_idx)} hours={len(run_hours)} ({'sealed' if a.sealed else 'development'}) "
          f"{time.strftime('%m-%d %H:%M', time.gmtime(lo))}..{time.strftime('%m-%d %H:%M', time.gmtime(hi))} UTC "
          f"self-check {n_chk - bad}/{n_chk} gate+rank match the engine [{time.time() - t0:.0f}s]", flush=True)
    if bad:
        return 1
    coordinator = None
    if a.axis_current:
        from coord_engine import Coordinator
        coordinator = Coordinator()
        coordinator.load({}, overlay)
    rows = sg.execute(book, names, bars, times, shadow, tapes, run_hours, refresh_s=0, axis_coord=coordinator)
    trades = sorted((r for r in rows if "net" in r and lo <= r["t_in"] < hi), key=lambda r: r["t_in"])
    by_id = {st.id: st for st in book.by_idx}
    print(f"executed {len(trades)} positions [{time.time() - t0:.0f}s]", flush=True)

    # each Set's Main-Set orders over the whole data, once. With the manual floors (minPfStepAuto off) a Set's chain does not depend on the
    # instant it is read at; with learned floors it does, so each rebuild then reads the chain at its own instant (the engine's path, slower).
    static = not getattr(book, "pf_step_auto", False)
    main_rows = {}
    if static:
        for st in book.by_idx:
            tx, te, pp = tapes.by_set[st.idx]
            nets = [x - tapes.cf for x in pp]
            ch = se.stage_chain(list(tx), list(te), nets, book.stage_params(st.pack, st))
            sym = tapes.symbols_by_set[st.idx]
            main_rows[st.idx] = [(tx[k], te[k], sym[k], nets[k]) for k in ch["main"]]
        print(f"Main Sets: {sum(len(v) for v in main_rows.values())} orders of {sum(1 for v in main_rows.values() if v)} Sets [{time.time() - t0:.0f}s]", flush=True)
    auto = sg.AutoFloors(book, tapes)

    def accumulator(now: float) -> "rl.Accumulator":
        acc = rl.Accumulator()
        for st in book.by_idx:
            if static:
                rws = main_rows[st.idx]
                i0 = bisect.bisect_left(rws, (now - lb,))
                i1 = bisect.bisect_left(rws, (now,))
                acc.add(book.relation_cell(st), rws[i0:i1])
            else:
                acc.add(book.relation_cell(st), sg.relation_rows(book, tapes, st, now))
        return acc

    arms = json.loads(a.arms) if a.arms else DEFAULT_ARMS
    cfgs = {arm["name"]: rl.config_of(arm["relationLanes"]) for arm in arms}
    # per arm: per relation, was the trade's lane passing at its entry (the newest rebuild at or before it)
    marks = {name: [] for name in cfgs}
    every = max(60, int(a.every))
    k = 0
    checked = mism = 0
    lane_counts = {name: [0, 0] for name in cfgs}
    t = float(run_hours[0])
    rebuilds = 0
    while t < hi and k < len(trades):
        nxt = t + every
        if not static:
            auto.apply(book, float(int(t) // 3600 * 3600))
        batch = []
        while k < len(trades) and trades[k]["t_in"] < nxt:
            batch.append(trades[k])
            k += 1
        if batch:
            acc = accumulator(t)
            rebuilds += 1
            for name, cfg in cfgs.items():
                lanes = acc.lanes(cfg)
                lane_counts[name][0] += len(lanes)
                lane_counts[name][1] += sum(1 for v in lanes.values() if v["ok"])
                if static and rebuilds % 12 == 1 and name == next(iter(cfgs)):
                    ref = sg.relation_lanes_at(book, tapes, t, cfg)           # the engine's path at this instant
                    checked += 1
                    mism += ref != lanes
                for r in batch:
                    ids = rl.keys_of(book.relation_cell(by_id[r["set"]]))
                    marks[name].append({rel: bool(lanes.get(ids[rel], {}).get("ok")) for rel in rl.RELATIONS})
        t = nxt
    print(f"relation lanes rebuilt {rebuilds} times; engine-path comparison {checked - mism}/{checked} equal [{time.time() - t0:.0f}s]", flush=True)
    if mism:
        print("the study's lanes differ from the engine's path: the figures below would not be the engine's")
        return 1

    unit = figures([(r["net"], 1.0) for r in trades])
    out = {"window": {"from": lo, "to": hi, "sealed": bool(a.sealed), "symbols": len(names), "sets": len(book.by_idx)},
           "unit": unit, "lanes": {"pos": sum(r["lane"] != "neg" for r in trades), "neg": sum(r["lane"] == "neg" for r in trades)}, "arms": []}
    print(f"\nunit (lanes off): PF {unit['pf']}  n {unit['n']}  net {unit['netPct']} %  max dd {unit['maxDdPct']} %  "
          f"(positive lane {out['lanes']['pos']}, negative {out['lanes']['neg']})")
    for arm in arms:
        name, cfg = arm["name"], cfgs[arm["name"]]
        mk = marks[name]
        pairs, raised, base = [], [], []
        for r, m in zip(trades, mk):
            mult = 1.0
            if r["lane"] != "neg":
                mult = min(cfg.max_mult, 1.0 + sum(cfg.ratio(rel) for rel in cfg.on if cfg.ratio(rel) > 0 and m[rel]))
            pairs.append((r["net"], mult))
            (raised if mult > 1.0 + 1e-9 else base).append((r["net"], 1.0))
        w, fr, fb = figures(pairs), figures(raised), figures(base)
        singles = {}
        for rel in SINGLE:
            yes = figures([(r["net"], 1.0) for r, m in zip(trades, mk) if r["lane"] != "neg" and m[rel]])
            no = figures([(r["net"], 1.0) for r, m in zip(trades, mk) if r["lane"] != "neg" and not m[rel]])
            singles[rel] = {"passing": yes, "failing": no}
        row = {"name": name, "config": cfg.as_dict(), "weighted": w, "raisedUnit": fr, "baseUnit": fb, "byRelation": singles,
               "lanesBuilt": lane_counts[name][0], "lanesPassing": lane_counts[name][1]}
        out["arms"].append(row)
        print(f"\n{name}: mainN {cfg.main_n} realN {cfg.real_n} floor {cfg.min_pf} ratios {dict(cfg.ratios)} max {cfg.max_mult}"
              f"  lanes passing {lane_counts[name][1]}/{lane_counts[name][0]}")
        print(f"  weighted PF {w['pf']}  net {w['netPct']} %  volume {w['volume']}  net/volume {w['netPerVolumePct']} % (unit {unit['netPerVolumePct']} %)  max dd {w['maxDdPct']} %")
        print(f"  raised {fr['n']} positions unit PF {fr['pf']} net {fr['netPct']} %   |   not raised {fb['n']} unit PF {fb['pf']} net {fb['netPct']} %")
        for rel in SINGLE:
            y, n_ = singles[rel]["passing"], singles[rel]["failing"]
            print(f"    {rel:8s} lane passing: {y['n']:4d} positions PF {y['pf']} net {y['netPct']} %   failing: {n_['n']:4d} PF {n_['pf']} net {n_['netPct']} %")
    if a.out:
        with open(a.out, "w") as f:
            json.dump(out, f, indent=1)
    print(f"\ndone [{time.time() - t0:.0f}s]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
