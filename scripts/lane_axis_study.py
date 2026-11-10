#!/usr/bin/env python3
"""Lane order and Axis on executed positions: the same walk-forward execution (stage_eval.execute: the engine's gate, ranking and Axis per Set, one
position per symbol) once per arm, each arm reported whole and per lane, so "with" is always read next to "without" on the same data.

Arms: laneOrder pos-neg (shipped) / neg-pos / neg / pos, each without Axis and with the overlay's Axis rules. laneOrder acts at entry time only (it changes
which valid Set a signal opens under, never a tape), so every arm reads one shadow cache. At most three days of 1m history; the development window (the
first two thirds) unless --sealed.

usage: lane_axis_study.py --data DIR --cache FILE [--symbols 20] [--jobs 3] [--patch JSON] [--sealed] [--orders pos-neg,neg-pos,neg,pos] [--out FILE]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))


def figures(nets):
    g = sum(x for x in nets if x > 0)
    l_ = -sum(x for x in nets if x < 0)
    peak = cum = dd = 0.0
    for x in nets:
        cum += x
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {"n": len(nets), "pf": round(g / l_, 3) if l_ > 0 else (99.0 if g > 0 else None), "netPct": round(100 * (g - l_), 2), "maxDdPct": round(100 * dd, 2)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--patch", default="")
    ap.add_argument("--sealed", action="store_true")
    ap.add_argument("--orders", default="pos-neg,neg-pos,neg,pos")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    if a.patch:
        os.environ["STAGE_OVERLAY_PATCH"] = a.patch
    import stage_eval as sg  # noqa: E402
    import replay_cache  # noqa: E402
    import set_engine as se  # noqa: E402
    from coord_engine import Coordinator  # noqa: E402
    t0 = time.time()
    book = sg.make_book()
    names = sorted(f[:-5] for f in os.listdir(a.data) if f.endswith("-USDT.json"))[:a.symbols]
    overlay = sg.replay_overlay()
    manifest = replay_cache.fingerprint(a.data, names, overlay, sg.PULSE, sg.__file__)      # the lane order (set per arm below) is not part of the cache key
    _n, bars, times = sg.load_bars(a.data, a.symbols)
    if (max(times[s][-1] for s in names) - min(times[s][0] for s in names)) > 3 * 86400:
        raise ValueError("more than three days of market history: not allowed for a simulation")
    if os.path.exists(a.cache):
        shadow = replay_cache.load(a.cache, manifest)
    else:
        shadow = sg.build(names, bars, times, a.jobs)
        replay_cache.save(a.cache, shadow, manifest)
    shadow = sg.build_hold_auto(names, bars, times, shadow)
    tapes = sg.Tapes(book, names, times, shadow)
    start = max(times[s][0] for s in names) + book.lookback * 60
    end = min(times[s][-1] for s in names)
    hours = [float(h) for h in range(((int(start) + 3599) // 3600) * 3600, int(end), 3600)]
    split = hours[len(hours) * 2 // 3]
    lo, hi = (split, hours[-1] + 3600) if a.sealed else (hours[0], split)
    run_hours = [h for h in hours if lo <= h < hi]
    n_chk, bad = sg.self_check(book, tapes, run_hours, samples=40)
    print(f"symbols={len(names)} sets={len(book.by_idx)} hours={len(run_hours)} ({'sealed' if a.sealed else 'development'}) "
          f"{time.strftime('%m-%d %H:%M', time.gmtime(lo))}..{time.strftime('%m-%d %H:%M', time.gmtime(hi))} UTC self-check {n_chk - bad}/{n_chk} [{time.time() - t0:.0f}s]", flush=True)
    if bad:
        return 1
    coord = Coordinator()
    coord.load({}, overlay)
    out = {"window": {"from": lo, "to": hi, "sealed": bool(a.sealed)}, "arms": {}}
    for order in [x.strip() for x in a.orders.split(",") if x.strip()]:
        book.lane_order = se.LANE_ORDERS[order]
        for axis_label, axis_coord in (("no Axis", None), ("Axis", coord)):
            t1 = time.time()
            rows = sg.execute(book, names, bars, times, shadow, tapes, run_hours, refresh_s=0, axis_coord=axis_coord)
            trades = sorted((r for r in rows if "net" in r and lo <= r["t_in"] < hi), key=lambda r: r["t"])
            valid = [r["valid"] for r in rows if "hour" in r]
            res = {"all": figures([r["net"] for r in trades]), "pos": figures([r["net"] for r in trades if r["lane"] != "neg"]),
                   "neg": figures([r["net"] for r in trades if r["lane"] == "neg"]),
                   "byKind": {k: figures([r["net"] for r in trades if r["kind"] == k]) for k in ("base", "exit", "trail")},
                   "validSetsPerHour": round(sum(valid) / len(valid), 1) if valid else 0.0}
            out["arms"][f"{order} | {axis_label}"] = res
            print(f"{order:8s} {axis_label:8s} all {json.dumps(res['all'])}  pos {json.dumps(res['pos'])}  neg {json.dumps(res['neg'])}  valid Sets/h {res['validSetsPerHour']}  [{time.time() - t1:.0f}s]", flush=True)
            if a.out:
                json.dump(out, open(a.out, "w"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
