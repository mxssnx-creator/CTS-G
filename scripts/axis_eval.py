#!/usr/bin/env python3
"""Axis configurations on the walk-forward of stage_eval, post-Base: the coordinator's gate on the executed closes
(last N closes at or above a minimum PF; a pause after K losses in a row) against no Axis, development window only
(--sealed reads the last third, for the finalists). Each row: executed positions, PF, net, max drawdown, hours with a profit.

usage: axis_eval.py --data DIR --cache stage.pkl [--symbols 20] [--out FILE] [--sealed] [--grid small|full]"""
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
    lo, hi = (split, hours[-1] + 3600) if a.sealed else (hours[0], split)
    run_hours = [h for h in hours if lo <= h < hi]
    if a.grid == "small":
        grid = [None] + [(n, m, k) for n, m, k in itertools.product((4, 8, 16), (1.0, 1.25, 1.5), (0, 5))]
    elif a.grid == "mid":
        grid = [None] + [(n, m, k) for n, m, k in itertools.product((3, 6, 12, 24), (1.0, 1.25, 1.5), (0, 5))]
    else:
        grid = [None] + [(n, m, k) for n, m, k in itertools.product((3, 4, 6, 8, 12, 16, 24), (0.8, 1.0, 1.25, 1.5, 2.0), (0, 3, 5, 8))]
    out = {}
    # The Axis reads a paper stream: what every signal would have made with no Axis in the way. Judged on the positions it let
    # through only, a gate that closes never reopens (no closes, so no evidence).
    free = sg.execute(book, names, bars, times, shadow, tapes, run_hours)
    evidence = sorted((r["t"], r["net"]) for r in free if "net" in r)
    for ax in grid:
        t0 = time.time()
        rows = free if ax is None else sg.execute(book, names, bars, times, shadow, tapes, run_hours, axis=ax, axis_evidence=evidence)
        r = sg.summarize(rows, lo, hi, False)
        key = "no axis" if ax is None else f"last{ax[0]}>={ax[1]} pause{ax[2]}"
        out[key] = r
        print(f"{key:22s} PF {r['pf']}  orders {r['orders']}  net {r['net_pct']}%  maxDD {r['max_dd_pct']}%  hours+ {r['hours_positive']}  [{time.time() - t0:.0f}s]", flush=True)
        if a.out:
            json.dump(out, open(a.out, "w"), indent=1)


if __name__ == "__main__":
    main()
