#!/usr/bin/env python3
"""What the secondary last-N windows (pfWindow and friends) do as a validation on top of the Base gate. Arms (--arms): 'none' = the Base gate alone
(every window follows the overall validation N, nothing extra), and N = 6 (the old shipped value, below the floor of 8), 8, 12, 15, 20, 25: a Base-valid
Set must in addition have its last N orders at or above the minimum PF. Same hours, tapes and execution as stage_eval; development window
only (--sealed reads the last third once). Per arm: validated share, post-Base effective PF with its orders, executed PF with orders,
net, drawdown, hours positive.

usage: window_eval.py --data DIR --cache DIR [--symbols 20] [--out FILE] [--sealed]"""
from __future__ import annotations

import argparse, json, os, pickle, statistics, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))
import stage_eval as sg  # noqa: E402
import hold_auto_eval as ha  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--sealed", action="store_true")
    ap.add_argument("--arms", default="none,6,8,12,15,20,25")
    a = ap.parse_args()
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps({"holdEnabled": False})
    names, bars, times = sg.load_bars(a.data, a.symbols)
    shadow = pickle.load(open(os.path.join(a.cache, "shadow_h0.pkl"), "rb"))
    book = sg.make_book()
    tapes = sg.Tapes(book, names, times, shadow)
    start = max(times[s][0] for s in names) + book.lookback * 60
    end = min(times[s][-1] for s in names)
    hours = [float(h) for h in range(int(start // 3600 + 1) * 3600, int(end), 3600)]
    split = hours[len(hours) * 2 // 3]
    lo, hi = (split, hours[-1] + 3600) if a.sealed else (hours[0], split)
    run_hours = [h for h in hours if lo <= h < hi]
    sample = run_hours[:: max(1, len(run_hours) // 24)]
    base_gate = sg.gate_metrics
    cf = tapes.cf
    lb = book.lookback * 60.0
    res = {}
    print(f"{'arm':26s} {'valid %':>8s} {'eff PF':>7s} {'eff orders/h':>12s} {'exec PF':>8s} {'orders':>7s} {'net %':>9s} {'maxDD %':>9s} {'hours+':>9s}")
    for arm in [x if x == "none" else int(x) for x in a.arms.split(",")]:
        def gate(book_, tapes_, st, now, _arm=arm):
            ok, key, n, pf = base_gate(book_, tapes_, st, now)
            if ok and _arm != "none":
                w = tapes_.window(st.idx, now, lb, _arm)[2]
                g = sum(x - cf for x in w if x - cf > 0)
                l_ = -sum(x - cf for x in w if x - cf < 0)
                ok = len(w) >= _arm and (g / l_ if l_ > 0 else 99.0) + 1e-9 >= book_.pack_min_pf.get(st.pack, book_.min_pf)
            return ok, key, n, pf
        sg.gate_metrics = gate
        pools = [ha.pool(book, tapes, h) for h in sample]
        rows = sg.execute(book, names, bars, times, shadow, tapes, run_hours)
        r = sg.summarize(rows, lo, hi, False)
        eff = [p["pf_post"] for p in pools if p["pf_post"] is not None]
        tot = pools[0]["total"]
        name = f"last-N {arm}" if arm != "none" else "none (overall N only)"
        res[name] = {"valid_pct": round(100 * sum(p["valid"] for p in pools) / max(1, tot * len(pools)), 2), "eff_pf": round(statistics.median(eff), 3) if eff else None,
                     "eff_orders_per_hour": round(sum(p["orders"] for p in pools) / len(pools)), "exec_pf": r["pf"], "exec_orders": r["orders"],
                     "net_pct": r["net_pct"], "max_dd_pct": r["max_dd_pct"], "hours_pos": r["hours_positive"]}
        v = res[name]
        print(f"{name:26s} {v['valid_pct']:>8} {v['eff_pf']!s:>7s} {v['eff_orders_per_hour']:>12} {v['exec_pf']!s:>8s} {v['exec_orders']:>7} {v['net_pct']:>9} {v['max_dd_pct']:>9} {v['hours_pos']:>9s}", flush=True)
    sg.gate_metrics = base_gate
    if a.out:
        json.dump(res, open(a.out, "w"), indent=1)


if __name__ == "__main__":
    main()
