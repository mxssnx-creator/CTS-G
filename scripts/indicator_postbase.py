#!/usr/bin/env python3
"""Post-Base evaluation of indicator variants (the standard): for each variant of the overlay (extraIndicators, switches) the
walk-forward of stage_eval runs, and per hour the engine's pooled PF after Base validation ("effective PF"), the validated share
of Sets and the orders behind them are read, plus the executed positions' PF. Choose on the development window only; the
sealed window is read once with --sealed, for the finalists.

usage: indicator_postbase.py --data DIR --variants FILE.json [--symbols 20] [--out FILE] [--sealed]
  FILE.json: {"name": {overlay patch}, ...}
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))
import stage_eval as sg  # noqa: E402
import sim12h as sm  # noqa: E402


def evaluate(data, symbols, jobs, patch, sealed):
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(patch)
    names, bars, times = sg.load_bars(data, symbols)
    shadow = sg.build(names, bars, times, jobs)
    book = sg.make_book()
    tapes = sg.Tapes(book, names, times, shadow)
    start = max(times[s][0] for s in names) + book.lookback * 60
    end = min(times[s][-1] for s in names)
    hours = [float(h) for h in range(int(start // 3600 + 1) * 3600, int(end), 3600)]
    split = hours[len(hours) * 2 // 3]
    window = [h for h in hours if (h >= split) == sealed]
    pools = [sm.pool(book, tapes, h) for h in window[:: max(1, len(window) // 24)]]       # up to ~24 evenly spaced hours
    rows = sg.execute(book, names, bars, times, shadow, tapes, window[:: max(1, len(window) // 24)] if False else window)
    trades = [r["net"] for r in rows if "net" in r]
    eff = [p["pf_post"] for p in pools if p["pf_post"] is not None]
    pcts = [p["validated_pct"] for p in pools]
    packs = {}
    for p in pools:
        for k, v in p["by_pack"].items():
            d = packs.setdefault(k, {"pf": [], "pct": [], "orders": []})
            if v["pf_post"] is not None:
                d["pf"].append(v["pf_post"])
            d["pct"].append(v["pct"])
            d["orders"].append(v["orders"])
    by_pack = {k: {"eff_pf_median": round(statistics.median(d["pf"]), 3) if d["pf"] else None, "validated_pct_mean": round(sum(d["pct"]) / len(d["pct"]), 2),
                   "orders_mean": round(sum(d["orders"]) / len(d["orders"]))} for k, d in packs.items()}
    gp = sum(x for x in trades if x > 0)
    gl = -sum(x for x in trades if x < 0)
    return {"hours_evaluated": len(pools), "effective_pf_median": round(statistics.median(eff), 3) if eff else None,
            "effective_pf_min": round(min(eff), 3) if eff else None, "validated_pct_mean": round(sum(pcts) / len(pcts), 2),
            "orders_valid_mean": round(sum(p["orders_post"] for p in pools) / len(pools)), "valid_sets_mean": round(sum(p["valid"] for p in pools) / len(pools), 1),
            "sets_total": pools[0]["total"], "pf_unfiltered_median": (round(statistics.median([p["pf_pre"] for p in pools if p["pf_pre"] is not None]), 3) if any(p["pf_pre"] is not None for p in pools) else None),
            "exec_orders": len(trades), "exec_pf": round(gp / gl, 3) if gl > 0 else None, "by_pack": by_pack}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--variants", required=True)
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--out", default="")
    ap.add_argument("--sealed", action="store_true")
    a = ap.parse_args()
    variants = json.load(open(a.variants))
    out = json.load(open(a.out)) if a.out and os.path.exists(a.out) else {}
    for name, patch in variants.items():
        if name in out:
            continue            # resumed run: already evaluated
        t0 = time.time()
        r = evaluate(a.data, a.symbols, a.jobs, patch, a.sealed)
        out[name] = r
        print(f"{name:34s} eff PF median {r['effective_pf_median']} (min {r['effective_pf_min']})  valid {r['validated_pct_mean']}% ({r['valid_sets_mean']}/{r['sets_total']})  "
              f"orders {r['orders_valid_mean']}  unfiltered {r['pf_unfiltered_median']}  exec PF {r['exec_pf']} on {r['exec_orders']}  [{time.time() - t0:.0f}s]", flush=True)
        for k, v in r["by_pack"].items():
            print(f"    pack {k:12s} eff PF median {v['eff_pf_median']}  valid {v['validated_pct_mean']}%  orders {v['orders_mean']}", flush=True)
        if a.out:
            json.dump(out, open(a.out, "w"), indent=1)


if __name__ == "__main__":
    main()
