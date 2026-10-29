#!/usr/bin/env python3
"""Window grid on a fresh test: the Base long window x the Base short window x the ongoing (Real) last N, walked forward on stored 1m history.

Every cell is one engine configuration (an overlay patch loaded into a SetBook, so the engine's own clamps apply) evaluated hour by hour
like the live engine: shadow tapes per Set (stage_eval), the gate and the stage chain per hour (Base long + short window, Main static + Auto,
Negative lane, Real), the best valid Set per signal, positions closed by their Set's own lane. Per cell it reports the validated share and the
effective PF after all gates (in-sample: the Sets were selected on those tapes), then the executed PF with its order count (the number that
counts), net, drawdown, hours positive, both 12 h halves and the hourly orders and PF.

Grids: the "real" third (Real last N 15 / 25, Main static as shipped) and the "main" third (Main static 15 / 25, Real as shipped), because
"ongoing 15, 25" can be read either way. Cells with a short window at or over the long window are the long window itself and are skipped.
References: Base long window alone (stages off). The test window is the last --hours hour starts of the data; the pre-history of every hour is
--lookback bars (1440 = 24 h) before it. No development / sealed split: the whole window is the independent test, and the best cell chosen on
it is in-sample for that choice.

usage: window_grid.py --data DIR --cache FILE --out FILE [--long 20,30,40,50] [--short 5,10,15,20,25] [--mains 15,25] [--reals 15,25]
                      [--hours 24] [--lookback 1440] [--jobs 4]"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import pickle
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))
import hold_auto_eval as ha  # noqa: E402
import stage_eval as sg  # noqa: E402

ORDER_FLOOR = 150            # a cell with fewer executed orders is not ranked "best" (a PF on a handful of orders is noise)
G: Dict[str, Any] = {}       # the shared state the forked workers read


def base_patch(lookback: int) -> Dict[str, Any]:
    return {"holdEnabled": False, "histLookbackBars": lookback, "histLookbackAuto": False}


def cell_patch(cell: Dict[str, Any], lookback: int) -> Dict[str, Any]:
    p = base_patch(lookback)
    p["setGateWindow"] = cell["long"]
    if cell["stages"]:
        p.update({"stagesEnabled": True, "setGateShortN": cell["short"], "mainGateN": cell["main"], "realGateN": cell["real"]})
    else:
        p["stagesEnabled"] = False
    return p


def book_for(cell: Dict[str, Any]):
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(cell_patch(cell, G["lookback"]))
    return sg.make_book()


def hourly(rows: List[Dict[str, Any]], hours: List[float]) -> List[Dict[str, Any]]:
    out = {int(h): {"orders": 0, "gp": 0.0, "gl": 0.0, "valid": None} for h in hours}
    for r in rows:
        if "hour" in r and int(r["hour"]) in out:
            out[int(r["hour"])]["valid"] = round(100.0 * r["valid"] / max(1, r["total"]), 2)
        elif "net" in r:
            k = int(r["t_in"] // 3600) * 3600
            if k in out:
                o = out[k]
                o["orders"] += 1
                o["gp" if r["net"] > 0 else "gl"] += abs(r["net"])
    return [{"hour": h, "orders": v["orders"], "pf": round(v["gp"] / v["gl"], 3) if v["gl"] > 0 else None,
             "net_pct": round(100 * (v["gp"] - v["gl"]), 3), "valid_pct": v["valid"]} for h, v in sorted(out.items())]


def run_cell(cell: Dict[str, Any]) -> Dict[str, Any]:
    t0 = time.time()
    book = book_for(cell)
    names, bars, times, shadow, tapes, hours = (G[k] for k in ("names", "bars", "times", "shadow", "tapes", "hours"))
    pools = [ha.pool(book, tapes, h) for h in hours]
    tot = pools[0]["total"]
    eff = [p["pf_post"] for p in pools if p["pf_post"] is not None]
    rows = sg.execute(book, names, bars, times, shadow, tapes, hours)
    lo, mid, hi = hours[0], hours[len(hours) // 2], hours[-1] + 3600
    whole, h1, h2 = (sg.summarize(rows, a, b, False) for a, b in ((lo, hi), (lo, mid), (mid, hi)))
    trades = [r for r in rows if "net" in r]
    res = dict(cell)
    res.update({
        "short_eff": book.base_short if cell["stages"] else 0,
        "main_eff": book.main_n if cell["stages"] else None,
        "real_eff": book.realst_n if cell["stages"] else None,
        "valid_pct": round(100.0 * sum(p["valid"] for p in pools) / max(1, tot * len(pools)), 2),
        "eff_pf": round(statistics.median(eff), 3) if eff else None,
        "eff_orders_per_hour": round(sum(p["orders"] for p in pools) / len(pools)),
        "exec_pf": whole["pf"], "exec_orders": whole["orders"], "net_pct": whole["net_pct"], "max_dd_pct": whole["max_dd_pct"],
        "hours_pos": whole["hours_positive"],
        "wins": sum(1 for r in trades if r["net"] > 0), "neg_lane_orders": sum(1 for r in trades if r.get("lane") == "neg"),
        "half1": {"pf": h1["pf"], "orders": h1["orders"], "net_pct": h1["net_pct"]},
        "half2": {"pf": h2["pf"], "orders": h2["orders"], "net_pct": h2["net_pct"]},
        "hourly": hourly(rows, hours), "secs": round(time.time() - t0, 1),
    })
    return res


def enumerate_cells(longs, shorts, mains, reals, main_default, real_default) -> List[Dict[str, Any]]:
    seen: Dict[Tuple, Dict[str, Any]] = {}

    def add(L, S, M, R, stages=True):
        key = (L, S, M, R, stages)
        if key not in seen:
            seen[key] = {"long": L, "short": S, "main": M, "real": R, "stages": stages,
                         "id": f"L{L}_S{S}_M{M}_R{R}" if stages else f"L{L}_base_alone"}
        return seen[key]

    for L in longs:
        for S in shorts:
            if S >= L:
                continue                                   # the short window at or over the long window is the long window itself
            for R in reals:
                add(L, S, main_default, R)["real_third"] = True
            for M in mains:
                add(L, S, M, real_default)["main_third"] = True
    for L in longs:
        add(L, None, None, None, stages=False)
    return list(seen.values())


def best_of(cells: List[Dict[str, Any]], third: str) -> List[Dict[str, Any]]:
    pool = [c for c in cells if c["stages"] and c.get(third) and c["exec_pf"] is not None and c["exec_orders"] >= ORDER_FLOOR]
    return sorted(pool, key=lambda c: (c["exec_pf"], c["exec_orders"]), reverse=True)


def print_matrix(cells, third, vary, fixed_label, longs, shorts, values):
    for v in values:
        sel = {(c["long"], c["short"]): c for c in cells if c["stages"] and c.get(third) and c[vary] == v}
        print(f"\n{third.split('_')[0]} third: {vary} {v} ({fixed_label}) - executed PF (orders) / effective PF / valid %")
        print("  long\\short " + "".join(f"{s:>22d}" for s in shorts))
        for L in longs:
            line = f"  {L:>9d} "
            for s in shorts:
                c = sel.get((L, s))
                line += f"{'n/a':>22s}" if c is None else f"{str(c['exec_pf']) + ' (' + str(c['exec_orders']) + ') ' + str(c['eff_pf']) + ' ' + str(c['valid_pct']) + '%':>22s}"
            print(line)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--long", default="20,30,40,50")
    ap.add_argument("--short", default="5,10,15,20,25")
    ap.add_argument("--mains", default="15,25")
    ap.add_argument("--reals", default="15,25")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--lookback", type=int, default=1440)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--no-check", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(base_patch(a.lookback))
    names, bars, times = sg.load_bars(a.data, a.symbols)
    if os.path.exists(a.cache):
        shadow = pickle.load(open(a.cache, "rb"))
    else:
        shadow = sg.build(names, bars, times, a.jobs)
        pickle.dump(shadow, open(a.cache, "wb"))
    book0 = sg.make_book()
    tapes = sg.Tapes(book0, names, times, shadow)
    start = max(times[s][0] for s in names) + book0.lookback * 60
    end = min(times[s][-1] for s in names)
    all_hours = [float(h) for h in range(int(start // 3600 + 1) * 3600, int(end), 3600)]
    hours = all_hours[-a.hours:]
    if len(hours) < a.hours:
        print(f"only {len(hours)} test hours fit after a {a.lookback}-bar pre-history", file=sys.stderr)
    ov = json.load(open(sg.OVERLAY))
    main_default, real_default = int(ov.get("mainGateN", 25)), int(ov.get("realGateN", 15))
    longs, shorts = [int(x) for x in a.long.split(",")], [int(x) for x in a.short.split(",")]
    mains, reals = [int(x) for x in a.mains.split(",")], [int(x) for x in a.reals.split(",")]
    cells = enumerate_cells(longs, shorts, mains, reals, main_default, real_default)
    G.update(names=names, bars=bars, times=times, shadow=shadow, tapes=tapes, hours=hours, lookback=a.lookback)
    print(f"symbols={len(names)} bars/symbol={len(bars[names[0]])} test hours={len(hours)} "
          f"({time.strftime('%m-%d %H:%M', time.gmtime(hours[0]))} - {time.strftime('%m-%d %H:%M', time.gmtime(hours[-1] + 3600))} UTC) "
          f"pre-history {a.lookback} bars  cells={len(cells)}  prepared in {time.time() - t0:.0f}s", flush=True)
    checks = []
    if not a.no_check:
        corners = [c for c in cells if c["stages"] and (c["long"], c["short"]) in {(longs[0], shorts[0]), (longs[0], shorts[1]), (longs[-1], shorts[0]), (longs[-1], shorts[-1])}]
        for c in corners[:6]:
            bk = book_for(c)
            n_chk, bad = sg.self_check(bk, tapes, hours, samples=30)
            checks.append({"cell": c["id"], "samples": n_chk, "bad": bad})
            print(f"self-check {c['id']}: {n_chk - bad}/{n_chk} samples equal the engine's gate", flush=True)
            if bad:
                print("the direct gate differs from SetBook._score_one / gate_reason: the grid would not be the engine's", file=sys.stderr)
                return 1
    results: List[Dict[str, Any]] = []
    ctx = multiprocessing.get_context("fork")
    with ProcessPoolExecutor(max_workers=a.jobs, mp_context=ctx) as pool:
        futs = {pool.submit(run_cell, c): c for c in cells}
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            results.append(r)
            print(f"[{i}/{len(cells)}] {r['id']:<22s} exec PF {r['exec_pf']!s:>6s} ({r['exec_orders']:>4d} orders)  eff PF {r['eff_pf']!s:>6s}  "
                  f"valid {r['valid_pct']:>5}%  halves {r['half1']['pf']}/{r['half2']['pf']}  {r['secs']}s", flush=True)
    results.sort(key=lambda c: (not c["stages"], c["long"], c["short"] or 0, c["main"] or 0, c["real"] or 0))
    meta = {"symbols": names, "hours": [int(h) for h in hours], "lookback_bars": a.lookback, "prehistory_h": a.lookback / 60.0,
            "bars_per_symbol": len(bars[names[0]]), "created": int(time.time()), "order_floor": ORDER_FLOOR,
            "shipped": {"long": int(ov.get("setGateWindow", 50)), "short": int(ov.get("setGateShortN", 15)), "main": main_default, "real": real_default},
            "longs": longs, "shorts": shorts, "mains": mains, "reals": reals, "main_default": main_default, "real_default": real_default,
            "min_orders": int(ov.get("setGateMinTrades", 0)), "max_ddt_h": float(ov.get("setMaxDdtHours", 24)), "min_pf": float(ov.get("setMinPf", 1.25)),
            "cost_pct": book0.cost_pct, "sets": len(book0.by_idx), "self_check": checks, "hold": "off (as every window evaluation)"}
    json.dump({"meta": meta, "cells": results}, open(a.out, "w"), indent=1)
    print_matrix(results, "real_third", "real", f"Main static {main_default}", longs, shorts, reals)
    print_matrix(results, "main_third", "main", f"Real {real_default}", longs, shorts, mains)
    print("\nbase long window alone (stages off):")
    for c in results:
        if not c["stages"]:
            print(f"  long {c['long']}: exec PF {c['exec_pf']} ({c['exec_orders']} orders) eff PF {c['eff_pf']} valid {c['valid_pct']}% halves {c['half1']['pf']}/{c['half2']['pf']}")
    for third in ("real_third", "main_third"):
        print(f"\nbest 8 by executed PF, {third}, at least {ORDER_FLOOR} executed orders:")
        for c in best_of(results, third)[:8]:
            print(f"  long {c['long']:>2d} short {c['short']:>2d} main {c['main_eff']:>2d} real {c['real_eff']:>2d}: exec PF {c['exec_pf']} ({c['exec_orders']} orders) "
                  f"eff PF {c['eff_pf']} valid {c['valid_pct']}% halves {c['half1']['pf']}/{c['half2']['pf']} hours+ {c['hours_pos']}")
    print(f"\ndone in {time.time() - t0:.0f}s -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
