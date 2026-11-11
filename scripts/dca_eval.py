#!/usr/bin/env python3
"""DCA on the executed positions of the walk-forward (stage_eval), post-Base: which add-on rules give the best PF with
the lowest drawdown. Each executed position (opened under a Base-valid Set, exactly as stage_eval executes) is walked
again bar by bar with rungs added against it:

  rung k fills at the first bar that reaches entry -/+ dist_k x (the Set's SL distance), size = mult_k x the base size;
  the stop stays at the ORIGINAL level (bounded risk) unless stop_mode "avg" moves it with the average;
  TP is the original level ("orig") or average +/- the TP distance ("avg");
  time exit as the lane's, at the close.

Net per position is the sum over fills of size x (exit - fill)/fill x side - cost, in units of the base notional; PF is
gross profit over gross loss of those position nets, drawdown is the peak-to-trough of the cumulative net in percent of
one base notional. Positions are evaluated independently (a longer hold does not block later entries): an approximation
that is the same for every candidate, so candidates compare fairly with the no-DCA row.

usage: dca_eval.py --data DIR [--symbols 20] [--cache stage.pkl] [--out dca.json]
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import pickle
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))
import stage_eval as sg  # noqa: E402
import set_engine as se  # noqa: E402
from position_cost import cost_as_frac  # noqa: E402


def walk(bars, tr, fracs, cfg, rungs, tp_mode="orig", stop_mode="orig", confirm=False):
    """rungs: [(distance as a fraction of the SL distance, size multiple)]. Returns the net of the whole position."""
    cf = cost_as_frac(cfg["cost_pct"])
    side = int(tr["dir"])
    e0 = float(tr["entry_px"])
    sl_f, tp_f = fracs
    sl = e0 * (1 - side * sl_f)
    tp = e0 * (1 + side * tp_f)
    fills = [(e0, 1.0)]
    nxt = 0
    i0 = int(tr["entry_i"])
    last = min(len(bars) - 1, i0 + int(cfg["time_bars"]))
    exit_px = float(bars[last][3])
    for j in range(i0 + 1, last + 1):
        b = bars[j]
        hi, lo, cl = float(b[1]), float(b[2]), float(b[3])
        size = sum(q for _, q in fills)
        avg = sum(p * q for p, q in fills) / size
        stop = sl if stop_mode == "orig" else avg * (1 - side * sl_f)
        target = tp if tp_mode == "orig" else avg * (1 + side * tp_f)
        if (side > 0 and lo <= stop) or (side < 0 and hi >= stop):
            exit_px = stop
            break
        # rungs fill before the target is judged, at their own level, when the bar reaches them without the stop
        while nxt < len(rungs):
            d, m = rungs[nxt]
            lvl = e0 * (1 - side * d * sl_f)
            touched = (side > 0 and lo <= lvl and lvl > stop) or (side < 0 and hi >= lvl and lvl < stop)
            if touched and confirm:
                # confirmed add: the bar must close back the other way (a bounce) and stay clear of the stop; fill at that close
                pc = float(bars[j - 1][3])
                if not ((side > 0 and cl > pc and cl > stop) or (side < 0 and cl < pc and cl < stop)):
                    break
                fills.append((cl, m))
                nxt += 1
                size = sum(q for _, q in fills)
                avg = sum(p * q for p, q in fills) / size
                target = tp if tp_mode == "orig" else avg * (1 + side * tp_f)
                continue
            if touched:
                fills.append((lvl, m))
                nxt += 1
                size = sum(q for _, q in fills)
                avg = sum(p * q for p, q in fills) / size
                target = tp if tp_mode == "orig" else avg * (1 + side * tp_f)
            else:
                break
        if cfg["honor_tp"] and ((side > 0 and hi >= target) or (side < 0 and lo <= target)):
            exit_px = target
            break
        if j - i0 >= cfg["time_bars"]:
            exit_px = cl
            break
        if j - i0 >= cfg["scratch_bars"] and (cl - avg) / avg * side >= cfg["scratch_min"]:
            exit_px = cl
            break
    return sum(q * ((exit_px - p) / p * side - cf) for p, q in fills), sum(q for _, q in fills)


def summarize_net(vals):
    gp = sum(v for _, v in vals if v > 0)
    gl = -sum(v for _, v in vals if v < 0)
    eq = peak = mdd = 0.0
    for _, v in sorted(vals):
        eq += v
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    return {"pf": round(gp / gl, 3) if gl > 0 else None, "orders": len(vals), "net_pct": round(100 * (gp - gl), 2),
            "max_dd_pct": round(100 * mdd, 2), "net_per_dd": round((gp - gl) / mdd, 3) if mdd > 0 else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--cache", default="")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    t0 = time.time()
    names, bars, times = sg.load_bars(a.data, a.symbols)
    if a.cache and os.path.exists(a.cache):
        shadow = pickle.load(open(a.cache, "rb"))
    else:
        shadow = sg.build(names, bars, times, a.jobs)
        if a.cache:
            pickle.dump(shadow, open(a.cache, "wb"))
    book = sg.make_book()
    tapes = sg.Tapes(book, names, times, shadow)
    start = max(times[s][0] for s in names) + book.lookback * 60
    end = min(times[s][-1] for s in names)
    hours = [float(h) for h in range(int(start // 3600 + 1) * 3600, int(end), 3600)]
    split = hours[len(hours) * 2 // 3]
    cfg = sg.lane_cfg(book)
    rows = [r for r in sg.execute(book, names, bars, times, shadow, tapes, hours) if "tr" in r]
    dev = [r for r in rows if r["t_in"] < split]
    print(f"symbols={len(names)} hours={len(hours)} dev positions={len(dev)} (sealed {len(rows) - len(dev)} not read) build {time.time() - t0:.0f}s", flush=True)

    def run(rungs, tp_mode, stop_mode, rs, confirm=False):
        vals = []
        for r in rs:
            net, _ = walk(bars[r["symbol"]], r["tr"], r["fracs"], cfg, rungs, tp_mode, stop_mode, confirm)
            vals.append((r["t"], net))
        return summarize_net(vals)

    res = {}
    base = run([], "orig", "orig", dev)
    res["no DCA"] = base
    print(f"{'no DCA':52s} PF {base['pf']}  orders {base['orders']}  net {base['net_pct']}%  maxDD {base['max_dd_pct']}%", flush=True)
    cands = []
    for n in (1, 2, 3, 4):
        for gap in (0.25, 0.4, 0.6):
            for mult in (0.5, 1.0, 1.5):
                for tp_mode in ("orig", "avg"):
                    for stop_mode in ("orig",):
                        rungs = [((k + 1) * gap if gap * n < 1.0 else (k + 1) / (n + 1), mult) for k in range(n)]
                        cands.append((n, gap, mult, tp_mode, stop_mode, rungs))
    seen = set()
    for n, gap, mult, tp_mode, stop_mode, rungs in cands:
        key = (tuple(round(d, 3) for d, _ in rungs), mult, tp_mode, stop_mode)
        if key in seen:
            continue
        seen.add(key)
        sm = run(rungs, tp_mode, stop_mode, dev)
        name = f"n={n} rungs@{[round(d, 2) for d, _ in rungs]} x{mult} tp={tp_mode} stop={stop_mode}"
        res[name] = sm
    for n, gap, mult in ((1, 0.4, 0.5), (1, 0.4, 1.0), (1, 0.6, 1.0), (2, 0.33, 0.5), (2, 0.25, 0.5), (3, 0.2, 0.5)):
        rungs = [((k + 1) * gap, mult) for k in range(n)]
        for tp_mode in ("orig", "avg"):
            res[f"CONFIRMED n={n} rungs@{[round(d, 2) for d, _ in rungs]} x{mult} tp={tp_mode}"] = run(rungs, tp_mode, "orig", dev, confirm=True)
    ranked = sorted(((k, v) for k, v in res.items() if k != "no DCA"), key=lambda kv: (kv[1]["net_per_dd"] if kv[1]["net_per_dd"] is not None else -9), reverse=True)
    for k, v in ranked[:12]:
        print(f"{k:52s} PF {v['pf']}  net {v['net_pct']}%  maxDD {v['max_dd_pct']}%  net/DD {v['net_per_dd']}")
    print("...")
    for k, v in ranked[-3:]:
        print(f"{k:52s} PF {v['pf']}  net {v['net_pct']}%  maxDD {v['max_dd_pct']}%  net/DD {v['net_per_dd']}")
    if a.out:
        json.dump(res, open(a.out, "w"), indent=1)


if __name__ == "__main__":
    main()
