#!/usr/bin/env python3
"""Detailed trade simulation, walked forward hour by hour on stored 1m history (stage_eval's tapes and execution, the
engine's own gate and ranking, checked against SetBook on samples).

Everything is counted in orders, never in hours: a Set is validated on its last N orders (the Base gate window), and what a
validation is worth is read on the orders that follow it. The gate and the ranking are recomputed every --refresh seconds
(the live engine: histRefreshS, 120 s).

For every strategy type (pack x kind: base = TP step, exit = no TP, trail = trailing pair) and every range (TP step, SL
ratio, trailing pair) it reports, on the development window (the first two thirds; --sealed reads the last third once):

  sets          Sets of that group
  valid %       mean share of its Sets that pass Base at each evaluation
  eff PF        PF after Base: the last-N gate windows of the validated Sets, pooled (in-sample: what they were chosen on)
  next8 PF      the PF of each validated Set's next 8 orders after the validation (MIN_VALIDATION_N), pooled
  next50 PF     the same over its next 50 orders (the gate window): does a passed last-N carry into the next N?
  exec PF       the positions the walk-forward opened under that group's Sets (one per symbol, best valid Set)
  orders, net   executed orders and their net (percent of one position's notional, after PositionCost)
  blk50 >= 1    consecutive blocks of 50 executed orders whose PF is at least 1 (stability, in orders)
  min n         the fewest gate-window orders behind any validation of the group (the last-N count; must be >= 8)

usage: trade_sim.py --data DIR --cache shadow.pkl [--symbols 20] [--out FILE] [--sealed] [--patch JSON] [--refresh 600]
"""
from __future__ import annotations

import argparse
import bisect
import collections
import json
import os
import pickle
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))


def pf(gp, gl):
    if gl > 0:
        return round(gp / gl, 3)
    return 99.0 if gp > 0 else None


def acc_pf(vals):
    return pf(sum(v for v in vals if v > 0), -sum(v for v in vals if v < 0))


def range_of(st):
    if st.kind == "trail":
        return f"trail {st.trail_key}"
    return f"step {st.step:02d}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--out", default="")
    ap.add_argument("--sealed", action="store_true", help="read the sealed window (finalists only, once)")
    ap.add_argument("--patch", default="", help="overlay patch (JSON) applied to the x01 overlay")
    ap.add_argument("--refresh", type=int, default=600, help="seconds between gate evaluations (live: 120)")
    a = ap.parse_args()
    args_refresh = max(60, min(3600, int(a.refresh)))
    if a.patch:
        os.environ["STAGE_OVERLAY_PATCH"] = a.patch
    import stage_eval as sg  # noqa: E402  (reads STAGE_OVERLAY_PATCH)
    from position_cost import MIN_VALIDATION_N, cost_as_frac  # noqa: E402
    t0 = time.time()
    names, bars, times = sg.load_bars(a.data, a.symbols)
    if os.path.exists(a.cache):
        shadow = pickle.load(open(a.cache, "rb"))
    else:
        shadow = sg.build(names, bars, times, a.jobs)
        pickle.dump(shadow, open(a.cache, "wb"))
    book = sg.make_book()
    if any(len(shadow[names[0]][3]) != len(book.by_idx) for _ in (0,)):
        print(f"cache holds {len(shadow[names[0]][3])} Sets, the book {len(book.by_idx)}: rebuild the cache for this grid")
        return 2
    tapes = sg.Tapes(book, names, times, shadow)
    cf = cost_as_frac(book.cost_pct)
    start = max(times[s][0] for s in names) + book.lookback * 60
    end = min(times[s][-1] for s in names)
    hours = [float(h) for h in range(int(start // 3600 + 1) * 3600, int(end), 3600)]
    split = hours[len(hours) * 2 // 3]
    lo, hi = (split, hours[-1] + 3600) if a.sealed else (hours[0], split)
    run_hours = [h for h in hours if lo <= h < hi]
    n_chk, bad = sg.self_check(book, tapes, run_hours, samples=60)
    print(f"symbols={len(names)} sets={len(book.by_idx)} hours={len(run_hours)} ({'sealed' if a.sealed else 'development'}) "
          f"self-check {n_chk - bad}/{n_chk} gate+rank match the engine  [{time.time() - t0:.0f}s]", flush=True)
    if bad:
        print("the simulated gate or ranking differs from the engine: numbers below would not be the engine's")
        return 1

    groups = {}            # key -> stats
    by_set_group = {}
    for st in book.by_idx:
        keys = [("type", f"{st.pack}/{st.kind}"), ("range", f"{st.pack}/{st.kind}/{range_of(st)}"), ("all", "all")]
        if st.kind != "trail":
            keys.append(("sl", f"{st.pack}/{st.kind}/sl {st.sl_ratio:.2f}"))
        by_set_group[st.idx] = keys
        for k in keys:
            g = groups.setdefault(k, collections.defaultdict(float))
            g["sets"] += 1
            g["min_n"] = 1e9
    nxt_ns = (MIN_VALIDATION_N, book.gate_window)
    slots = [h + k * args_refresh for h in run_hours for k in range(max(1, 3600 // args_refresh))]
    evals = []
    for now in slots:
        hv = collections.defaultdict(float)
        for st in book.by_idx:
            ok, key, n, gpf = sg.gate_metrics(book, tapes, st, now)
            keys = by_set_group[st.idx]
            for k in keys:
                groups[k]["set_evals"] += 1
            if not ok:
                continue
            tx, te, pp = tapes.window(st.idx, now, book.lookback * 60.0, book.gate_window)
            gp = sum(x - cf for x in pp if x - cf > 0)
            gl = -sum(x - cf for x in pp if x - cf < 0)
            txa, _tea, ppa = tapes.by_set[st.idx]
            i0 = bisect.bisect_left(txa, now)
            nxt = {m: [x - cf for x in ppa[i0:i0 + m]] for m in nxt_ns}
            for k in keys:
                g = groups[k]
                g["valid_evals"] += 1
                g["in_gp"] += gp
                g["in_gl"] += gl
                g["in_n"] += len(pp)
                for m, v in nxt.items():
                    g[f"n{m}_gp"] += sum(x for x in v if x > 0)
                    g[f"n{m}_gl"] += -sum(x for x in v if x < 0)
                    g[f"n{m}_n"] += len(v)
                g["min_n"] = min(g["min_n"], n)
            hv["valid"] += 1
            hv["in_gp"] += gp
            hv["in_gl"] += gl
            hv["min_n"] = n if not hv.get("min_n") else min(hv["min_n"], n)
        evals.append({"t": now, "valid": int(hv["valid"]), "valid_pct": round(100.0 * hv["valid"] / len(book.by_idx), 2),
                      "eff_pf": pf(hv["in_gp"], hv["in_gl"]), "min_n": int(hv.get("min_n") or 0)})
    print(f"gate pass done [{time.time() - t0:.0f}s]", flush=True)

    rows = sg.execute(book, names, bars, times, shadow, tapes, run_hours, refresh_s=args_refresh)
    trades = [r for r in rows if "net" in r and lo <= r["t_in"] < hi]
    by_id = {st.id: st for st in book.by_idx}
    trades.sort(key=lambda r: r["t_in"])
    for r in trades:
        st = by_id[r["set"]]
        for k in by_set_group[st.idx]:
            g = groups[k]
            g["ex_n"] += 1
            g["ex_net"] += r["net"]
            g["ex_gp"] += max(0.0, r["net"])
            g["ex_gl"] += max(0.0, -r["net"])
            g["ex_win"] += 1 if r["net"] > 0 else 0
            g.setdefault("ex_list", []).append(r["net"])

    def blocks(vals, size=50):
        """PF of consecutive blocks of `size` executed orders (the last, shorter block is left out)."""
        return [acc_pf(vals[i:i + size]) for i in range(0, len(vals) - size + 1, size)]
    def summary(k):
        g = groups[k]
        bl = blocks(g.get("ex_list") or [])
        return {"group": k[1], "sets": int(g["sets"]),
                "valid_pct": round(100.0 * g["valid_evals"] / g["set_evals"], 2) if g["set_evals"] else 0.0,
                "eff_pf": pf(g["in_gp"], g["in_gl"]), "eff_orders": int(g["in_n"]),
                "next8_pf": pf(g[f"n{nxt_ns[0]}_gp"], g[f"n{nxt_ns[0]}_gl"]), "next8_orders": int(g[f"n{nxt_ns[0]}_n"]),
                "next50_pf": pf(g[f"n{nxt_ns[1]}_gp"], g[f"n{nxt_ns[1]}_gl"]), "next50_orders": int(g[f"n{nxt_ns[1]}_n"]),
                "exec_pf": pf(g["ex_gp"], g["ex_gl"]), "orders": int(g["ex_n"]), "net_pct": round(100 * g["ex_net"], 2),
                "win_pct": round(100.0 * g["ex_win"] / g["ex_n"], 1) if g["ex_n"] else None,
                "blocks50_pf1": f"{sum(1 for x in bl if (x or 0) >= 1.0)}/{len(bl)}",
                "blocks50_min_pf": min((x for x in bl if x is not None), default=None),
                "min_n": int(g["min_n"]) if g["min_n"] < 1e9 else None}

    out = {"window": "sealed" if a.sealed else "development", "symbols": names, "sets": len(book.by_idx),
           "hours": len(run_hours), "gate": {"window": book.gate_window, "min": book.gate_min, "min_pf": book.min_pf,
                                              "max_ddt_h": book.max_ddt_h, "lookback_bars": book.lookback},
           "config": {"honor_tp_trail": book.hist_honor_tp, "exit_sets": book.exit_sets, "time_bars": book.hist_time_bars,
                      "scratch_bars": book.scratch_bars, "scratch_min_pct": round(book.scratch_min * 100, 3),
                      "cost_pct": book.cost_pct},
           "all": summary(("all", "all")),
           "types": [summary(k) for k in sorted(groups) if k[0] == "type"],
           "ranges": [summary(k) for k in sorted(groups) if k[0] == "range"],
           "sl": [summary(k) for k in sorted(groups) if k[0] == "sl"],
           "evaluations": evals,
           "exits": dict(collections.Counter(r["reason"] for r in trades)),
           "trades": [{"t_in": r["t_in"], "t_out": r["t"], "symbol": r["symbol"], "set": r["set"], "pack": r["pack"],
                       "kind": r["kind"], "reason": r["reason"], "net_pct": round(100 * r["net"], 4)} for r in trades]}
    hdr = (f"{'group':40s} {'sets':>5s} {'valid%':>7s} {'effPF':>6s} {'next8PF':>7s} {'next50PF':>8s} {'execPF':>6s} {'orders':>6s} "
           f"{'net%':>8s} {'win%':>5s} {'blk50>=1':>8s} {'minN':>4s}")

    def line(r):
        return (f"{r['group'][:40]:40s} {r['sets']:>5d} {r['valid_pct']:>7.2f} {r['eff_pf']!s:>6s} {r['next8_pf']!s:>7s} {r['next50_pf']!s:>8s} "
                f"{r['exec_pf']!s:>6s} {r['orders']:>6d} {r['net_pct']:>8.2f} {r['win_pct']!s:>5s} {r['blocks50_pf1']:>8s} {r['min_n']!s:>4s}")
    print(hdr)
    print(line(out["all"]))
    for r in out["types"]:
        print(line(r))
    print("-- ranges")
    for r in out["ranges"]:
        if r["orders"] or r["valid_pct"]:
            print(line(r))
    print("-- exits", out["exits"])
    vmin = [x["min_n"] for x in evals if x["valid"]]
    print(f"-- last-N: fewest gate-window orders behind a validation {min(vmin) if vmin else None} (gate minimum {book.gate_min})")
    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        json.dump(out, open(a.out + ".json", "w"), indent=1)
    print(f"done [{time.time() - t0:.0f}s]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
