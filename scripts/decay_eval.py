#!/usr/bin/env python3
"""Why the PF drops after Base validation: for every (Set, hour) that passes the gate, the PF of its gate window (in-sample) against the
PF of its own orders that close in the next hour (out of sample), pooled by TP step, with the cost (gross = before PositionCost) and the
duplicated share (orders that several validated Sets of the same symbol and entry bar share). Development window only.

usage: decay_eval.py --data DIR --cache DIR [--out FILE]"""
from __future__ import annotations

import argparse, json, os, pickle, sys, collections

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))
import stage_eval as sg  # noqa: E402


def pf(gp, gl):
    return round(gp / gl, 3) if gl > 0 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--symbols", type=int, default=20)
    a = ap.parse_args()
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps({"holdEnabled": False})
    names, bars, times = sg.load_bars(a.data, a.symbols)
    shadow = pickle.load(open(os.path.join(a.cache, "shadow_h0.pkl"), "rb"))
    book = sg.make_book()
    tapes = sg.Tapes(book, names, times, shadow)
    cf = tapes.cf
    start = max(times[s][0] for s in names) + book.lookback * 60
    end = min(times[s][-1] for s in names)
    hours = [float(h) for h in range(int(start // 3600 + 1) * 3600, int(end), 3600)]
    split = hours[len(hours) * 2 // 3]
    dev = [h for h in hours if h < split]
    lb = book.lookback * 60.0
    acc = collections.defaultdict(lambda: collections.defaultdict(float))
    for h in dev:
        for st in book.by_idx:
            ok, key, n, pf_in = sg.gate_metrics(book, tapes, st, h)
            if not ok:
                continue
            tx, te, pp = tapes.window(st.idx, h, lb, book.gate_window)
            gi = sum(x - cf for x in pp if x - cf > 0)
            li = -sum(x - cf for x in pp if x - cf < 0)
            tx2, te2, pp2 = tapes.by_set[st.idx]
            import bisect
            lo, hi = bisect.bisect_left(tx2, h), bisect.bisect_left(tx2, h + 3600)
            fw = pp2[lo:hi]
            for tag in ("all", f"step{st.step:02d}" if st.kind == "base" else "trail"):
                d = acc[tag]
                d["sets"] += 1
                d["in_gp"] += gi; d["in_gl"] += li; d["in_n"] += len(pp)
                d["gross_in_gp"] += sum(x for x in pp if x > 0); d["gross_in_gl"] += -sum(x for x in pp if x < 0)
                d["out_gp"] += sum(x - cf for x in fw if x - cf > 0); d["out_gl"] += -sum(x - cf for x in fw if x - cf < 0)
                d["gross_out_gp"] += sum(x for x in fw if x > 0); d["gross_out_gl"] += -sum(x for x in fw if x < 0)
                d["out_n"] += len(fw)
                d["out_cost"] += cf * len(fw)
                d["out_gross_sum"] += sum(fw)
    res = {}
    print(f"{'group':8s} {'Set-hours':>9s} {'in PF':>6s} {'in grossPF':>10s} {'out PF':>7s} {'out grossPF':>11s} {'out orders':>10s} {'avg gross/order %':>17s} {'cost/order %':>12s}")
    for tag in sorted(acc):
        d = acc[tag]
        r = {"set_hours": int(d["sets"]), "in_pf": pf(d["in_gp"], d["in_gl"]), "in_gross_pf": pf(d["gross_in_gp"], d["gross_in_gl"]),
             "out_pf": pf(d["out_gp"], d["out_gl"]), "out_gross_pf": pf(d["gross_out_gp"], d["gross_out_gl"]), "out_orders": int(d["out_n"]),
             "avg_gross_pct": round(100 * d["out_gross_sum"] / d["out_n"], 4) if d["out_n"] else None, "cost_pct": round(100 * cf, 4)}
        res[tag] = r
        print(f"{tag:8s} {r['set_hours']:>9d} {r['in_pf']!s:>6s} {r['in_gross_pf']!s:>10s} {r['out_pf']!s:>7s} {r['out_gross_pf']!s:>11s} {r['out_orders']:>10d} {r['avg_gross_pct']!s:>17s} {r['cost_pct']:>12}")
    if a.out:
        json.dump(res, open(a.out, "w"), indent=1)


if __name__ == "__main__":
    main()
