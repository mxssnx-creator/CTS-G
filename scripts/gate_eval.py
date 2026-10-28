#!/usr/bin/env python3
"""Base gate parameters on the walk-forward of stage_eval (development window; --sealed once for finalists): gate window, minimum
orders, minimum PF and the consistency floor, each judged by what the validated Sets then do on the following hours (executed PF,
orders, net, drawdown) together with the share of Sets that stay valid.

usage: gate_eval.py --data DIR --cache stage.pkl [--out FILE] [--sealed] [--grid small|wide]"""
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
import sim12h as sm  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--cache", default="")
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--out", default="")
    ap.add_argument("--sealed", action="store_true")
    ap.add_argument("--grid", default="small")
    a = ap.parse_args()
    names, bars, times = sg.load_bars(a.data, a.symbols)
    shadow = pickle.load(open(a.cache, "rb")) if a.cache and os.path.exists(a.cache) else sg.build(names, bars, times, a.jobs)
    if a.cache and not os.path.exists(a.cache):
        pickle.dump(shadow, open(a.cache, "wb"))
    book = sg.make_book()
    tapes = sg.Tapes(book, names, times, shadow)
    start = max(times[s][0] for s in names) + book.lookback * 60
    end = min(times[s][-1] for s in names)
    hours = [float(h) for h in range(int(start // 3600 + 1) * 3600, int(end), 3600)]
    split = hours[len(hours) * 2 // 3]
    lo, hi = (split, hours[-1] + 3600) if a.sealed else (hours[0], split)
    run_hours = [h for h in hours if lo <= h < hi]
    if a.grid == "small":
        grid = [(w, m, p, c) for w, m, p, c in itertools.product((50, 100), (30, 60), (1.25, 1.5, 2.0), (0.0, 1.0))]
    elif a.grid == "short":
        grid = [(w, m, p, c) for w, m, p, c in itertools.product((15, 20, 30), (10, 15, 20), (1.25, 1.5, 2.0), (0.0, 1.0))]
    else:
        grid = [(w, m, p, c) for w, m, p, c in itertools.product((30, 50, 100, 150), (20, 30, 60, 100), (1.1, 1.25, 1.5, 2.0), (0.0, 1.0, 1.2))]
    out = {}
    for w, m, p, c in grid:
        if m > w:
            continue
        t0 = time.time()
        book.gate_window, book.gate_min, book.min_pf, book.consistency_pf = w, m, p, c
        rows = sg.execute(book, names, bars, times, shadow, tapes, run_hours)
        r = sg.summarize(rows, lo, hi, False)
        pools = [sm.pool(book, tapes, h) for h in run_hours[:: max(1, len(run_hours) // 12)]]
        r["validated_pct"] = round(sum(x["validated_pct"] for x in pools) / len(pools), 2)
        vals = sorted(x["pf_post"] for x in pools if x["pf_post"] is not None)
        r["eff_pf"] = vals[len(vals) // 2] if vals else None
        key = f"window{w} min{m} pf{p} cons{c}"
        out[key] = r
        print(f"{key:30s} exec PF {r['pf']}  orders {r['orders']}  net {r['net_pct']}%  maxDD {r['max_dd_pct']}%  hours+ {r['hours_positive']}  valid {r['validated_pct']}%  eff PF {r['eff_pf']}  [{time.time() - t0:.0f}s]", flush=True)
        if a.out:
            json.dump(out, open(a.out, "w"), indent=1)


if __name__ == "__main__":
    main()
