#!/usr/bin/env python3
"""Per-step hold times on the walk-forward of stage_eval: for each candidate hold H (minutes) every Set leaves on a time exit after H
minutes (stepHoldOn / stepHoldMin), the shadow tapes are rebuilt, and each step's own Sets are judged on the development window:
the validated share and the post-Base effective PF with its orders (the engine's pool), and the executed PF of that step alone
(the best valid Set of the step, one position per symbol, as stage_eval executes). Steps are independent, so one build per H
serves every step. Choose on the development window; --sealed reads the last third once, for the chosen values.

usage: hold_eval.py --data DIR [--out FILE] [--steps 2-12] [--sealed] [--only 1:5,10:15 ...]"""
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

LOW = (1, 2, 3, 5, 8, 10, 15, 20, 30, 40, 50)             # minutes for steps 1..5 (the desk range 1-50)
HIGH = (30, 45, 60, 90, 120, 180, 240, 300, 350)          # steps 6..12 (the desk range 30-350)


def pool_step(book, tapes, hour, step):
    cf = tapes.cf
    lb = book.lookback * 60.0
    tot = val = 0
    gp_a = gl_a = gp_v = gl_v = 0.0
    n_v = 0
    for st in book.by_idx:
        if st.kind != "base" or st.step != step:
            continue
        ok, key, n, pf = sg.gate_metrics(book, tapes, st, hour)
        tx, te, pp = tapes.window(st.idx, hour, lb, max(book.gate_window, book.deact_n))
        w = pp[-book.gate_window:]
        gp = sum(x - cf for x in w if x - cf > 0)
        gl = -sum(x - cf for x in w if x - cf < 0)
        tot += 1
        gp_a += gp
        gl_a += gl
        if ok:
            val += 1
            gp_v += gp
            gl_v += gl
            n_v += len(w)
    return {"total": tot, "valid": val, "pf_pre": gp_a / gl_a if gl_a > 0 else None, "pf_post": gp_v / gl_v if gl_v > 0 else (99.0 if gp_v > 0 else None),
            "orders": n_v}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--sealed", action="store_true")
    ap.add_argument("--holds", default="", help="comma list of minutes to test for every step (default: the desk ranges)")
    ap.add_argument("--steps", default="2-12")
    a = ap.parse_args()
    lo_s, hi_s = (int(x) for x in a.steps.split("-"))
    steps = list(range(lo_s, hi_s + 1))
    cands = sorted({*LOW, *HIGH} | ({0} if not a.holds else set())) if not a.holds else [int(x) for x in a.holds.split(",")]
    out = json.load(open(a.out)) if a.out and os.path.exists(a.out) else {}
    names, bars, times = sg.load_bars(a.data, a.symbols)
    for H in [0] + cands:
        todo = [s for s in steps if (H == 0 or (LOW[0] <= H <= LOW[-1] and s <= 5) or (HIGH[0] <= H <= HIGH[-1] and s >= 6) or a.holds)]
        todo = [s for s in todo if f"{s}:{H}" not in out]
        if not todo:
            continue
        t0 = time.time()
        # H == 0: no per-step hold (the shipped global time stop); otherwise every step holds H minutes
        patch = {"stepHoldOn": [False] * 12, "stepHoldMin": [10] * 12} if H == 0 else {"stepHoldOn": [True] * 12, "stepHoldMin": [H] * 12}
        os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(patch)
        shadow = sg.build(names, bars, times, a.jobs)
        book = sg.make_book()
        tapes = sg.Tapes(book, names, times, shadow)
        start = max(times[s][0] for s in names) + book.lookback * 60
        end = min(times[s][-1] for s in names)
        hours = [float(h) for h in range(int(start // 3600 + 1) * 3600, int(end), 3600)]
        split = hours[len(hours) * 2 // 3]
        lo, hi = (split, hours[-1] + 3600) if a.sealed else (hours[0], split)
        run_hours = [h for h in hours if lo <= h < hi]
        sample = run_hours[:: max(1, len(run_hours) // 24)]
        for s in todo:
            pools = [pool_step(book, tapes, h, s) for h in sample]
            eff = [p["pf_post"] for p in pools if p["pf_post"] is not None]
            pre = [p["pf_pre"] for p in pools if p["pf_pre"] is not None]
            rows = sg.execute(book, names, bars, times, shadow, tapes, run_hours, only_steps={s})
            r = sg.summarize(rows, lo, hi, False)
            tot = pools[0]["total"] if pools else 0
            out[f"{s}:{H}"] = {"step": s, "hold": H, "exec_pf": r["pf"], "exec_orders": r["orders"], "net_pct": r["net_pct"], "max_dd_pct": r["max_dd_pct"],
                               "hours_pos": r["hours_positive"], "eff_pf": round(statistics.median(eff), 3) if eff else None,
                               "valid_pct": round(100 * sum(p["valid"] for p in pools) / max(1, tot * len(pools)), 2), "orders_valid": round(sum(p["orders"] for p in pools) / len(pools)),
                               "pf_unfiltered": round(statistics.median(pre), 3) if pre else None}
        print(f"hold {H:>3}  steps {todo}  [{time.time() - t0:.0f}s]", flush=True)
        if a.out:
            json.dump(out, open(a.out, "w"), indent=1)


if __name__ == "__main__":
    main()
