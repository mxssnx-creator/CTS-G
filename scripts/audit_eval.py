#!/usr/bin/env python3
"""Audit of what gets executed. (1) Ranking and validation controls on the same hours and tapes: the top-ranked valid Set (the engine's
choice), a random valid Set, the lowest-ranked valid Set, the Set with the most orders, and a control that ignores validation and
ranks every Set. If they all execute alike, neither the gate nor the rank carries information out of sample. (2) The detailed listing:
per strategy type (pack x base/trail) and TP-step range, the Sets, how many were validated, the validated share, the effective PF
after Base with its orders, and the executed PF with its orders, win rate and net. (3) Every executed position must have come from
a Set that passed the gate at the hour it opened (checked from the rows). Development window only; --sealed reads the last third once.

usage: audit_eval.py --data DIR --cache DIR [--out FILE] [--sealed]"""
from __future__ import annotations

import argparse, json, os, pickle, random, statistics, sys, collections

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))
import stage_eval as sg  # noqa: E402

RANGES = [(2, 5, "steps 2-5"), (6, 10, "steps 6-10"), (11, 20, "steps 11-20"), (21, 30, "steps 21-30")]


def rng_of(book, st):
    step = int(st.step) if st.kind == "base" else int(round(float(st.trail_arm) / max(1e-9, float(book.cost_pct))))
    for lo, hi, name in RANGES:
        if lo <= step <= hi:
            return name
    return "steps other"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--sealed", action="store_true")
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
    rnd = random.Random(11)
    modes = {
        "top rank (engine)": dict(),
        "random valid Set": dict(pick=lambda pool, k: rnd.choice(pool)),
        "lowest-ranked valid Set": dict(pick=lambda pool, k: pool[-1]),
        "most orders (valid)": dict(pick=lambda pool, k: max(pool, key=lambda s: tapes.window(s.idx, float(k[1]), book.lookback * 60.0, 10 ** 9)[2].__len__())),
        "CONTROL: any Set, gate ignored (top rank)": dict(control="all"),
        "CONTROL: any Set, gate ignored (random)": dict(control="all", pick=lambda pool, k: rnd.choice(pool)),
    }
    out = {"modes": {}, "listing": {}}
    rows_top = None
    print(f"{'mode':44s} {'exec PF':>8s} {'orders':>7s} {'net %':>9s} {'maxDD %':>9s} {'hours+':>9s} {'validated %':>11s}")
    for name, kw in modes.items():
        rows = sg.execute(book, names, bars, times, shadow, tapes, run_hours, **kw)
        r = sg.summarize(rows, lo, hi, False)
        out["modes"][name] = r
        print(f"{name:44s} {r['pf']!s:>8s} {r['orders']:>7d} {r['net_pct']:>9} {r['max_dd_pct']:>9} {r['hours_positive']:>9s} {r['validated_pct']:>11}", flush=True)
        if rows_top is None:
            rows_top = rows
    # (3) every executed position of the engine's mode came from a Set that was valid at its entry hour
    by_idx = {st.id: st for st in book.by_idx}
    bad = 0
    n_tr = 0
    for r in rows_top:
        if "net" not in r:
            continue
        n_tr += 1
        st = by_idx[r["set"]]
        h = float(int(r["t_in"] - 60) // 3600 * 3600)
        if not sg.gate_metrics(book, tapes, st, h)[0]:
            bad += 1
    print(f"audit: {n_tr} executed positions, {bad} from a Set that was not valid at its entry hour")
    out["audit"] = {"executed": n_tr, "not_valid_at_entry": bad}
    # (2) listing per type and range: Sets, validated share, effective PF after Base, executed PF
    sample = run_hours[:: max(1, len(run_hours) // 24)]
    groups = collections.defaultdict(lambda: {"sets": 0, "val": 0, "gp": 0.0, "gl": 0.0, "n": 0, "gpa": 0.0, "gla": 0.0, "hours": 0})
    cf = tapes.cf
    lb = book.lookback * 60.0
    for st in book.by_idx:
        key = (st.pack, st.kind, rng_of(book, st))
        groups[key]["sets"] += 1
    for h in sample:
        seen = set()
        for st in book.by_idx:
            key = (st.pack, st.kind, rng_of(book, st))
            g = groups[key]
            ok, _, n, _ = sg.gate_metrics(book, tapes, st, h)
            tx, te, pp = tapes.window(st.idx, h, lb, book.gate_window)
            w = pp[-book.gate_window:]
            gp = sum(x - cf for x in w if x - cf > 0)
            gl = -sum(x - cf for x in w if x - cf < 0)
            g["gpa"] += gp; g["gla"] += gl
            if ok:
                g["val"] += 1; g["gp"] += gp; g["gl"] += gl; g["n"] += len(w)
    ex = collections.defaultdict(lambda: {"gp": 0.0, "gl": 0.0, "n": 0, "w": 0})
    for r in rows_top:
        if "net" not in r:
            continue
        key = (r["pack"], r["kind"], rng_of(book, by_idx[r["set"]]))
        e = ex[key]
        e["n"] += 1
        e["w"] += r["net"] > 0
        e["gp"] += max(0.0, r["net"]); e["gl"] += max(0.0, -r["net"])
    print(f"\n{'pack':12s} {'type':6s} {'range':12s} {'Sets':>5s} {'valid %':>8s} {'unfilt PF':>9s} {'eff PF':>7s} {'eff orders/h':>12s} {'exec PF':>8s} {'exec n':>7s} {'win %':>6s}")
    for key in sorted(set(groups) | set(ex)):
        g, e = groups[key], ex[key]
        pre = g["gpa"] / g["gla"] if g["gla"] > 0 else None
        post = g["gp"] / g["gl"] if g["gl"] > 0 else None
        vp = 100.0 * g["val"] / max(1, g["sets"] * len(sample))
        xp = e["gp"] / e["gl"] if e["gl"] > 0 else None
        out["listing"]["|".join(key)] = {"sets": g["sets"], "valid_pct": round(vp, 2), "unfiltered_pf": pre and round(pre, 3), "effective_pf": post and round(post, 3),
                                         "effective_orders_per_hour": round(g["n"] / len(sample)), "exec_pf": xp and round(xp, 3), "exec_orders": e["n"],
                                         "win_pct": round(100 * e["w"] / e["n"], 1) if e["n"] else None}
        f = lambda x, d=3: "-" if x is None else f"{x:.{d}f}"
        print(f"{key[0]:12s} {key[1]:6s} {key[2]:12s} {g['sets']:>5d} {vp:>8.2f} {f(pre):>9s} {f(post):>7s} {g['n'] / len(sample):>12.0f} {f(xp):>8s} {e['n']:>7d} {100 * e['w'] / e['n'] if e['n'] else 0:>6.1f}")
    # (4) counts behind every validation: the orders in each validated Set's gate window, and how many of those orders are distinct
    # (the same entry minute, exit minute and result, shared by Sets of one pack, count once)
    ns, tot_orders, distinct = [], 0, set()
    per_hour_dup = []
    for h in sample:
        seen_h, tot_h = set(), 0
        for st in book.by_idx:
            ok, _, n, _ = sg.gate_metrics(book, tapes, st, h)
            if not ok:
                continue
            ns.append(n)
            tx, te, pp = tapes.window(st.idx, h, lb, book.gate_window)
            for a_, b_, x_ in zip(tx[-book.gate_window:], te[-book.gate_window:], pp[-book.gate_window:]):
                seen_h.add((a_, b_, round(x_, 7)))
                tot_h += 1
        if tot_h:
            per_hour_dup.append((tot_h, len(seen_h)))
    ns.sort()
    q = lambda f: ns[int(f * (len(ns) - 1))] if ns else None
    tot = sum(a_ for a_, _ in per_hour_dup)
    dis = sum(b_ for _, b_ in per_hour_dup)
    print(f"\ncounts: validated Set-hours {len(ns)}; orders in the gate window min {ns[0] if ns else None} p10 {q(0.1)} median {q(0.5)} max {ns[-1] if ns else None}; "
          f"below 8: {sum(1 for x in ns if x < 8)}; below gate minimum {book.gate_min}: {sum(1 for x in ns if x < book.gate_min)}")
    print(f"counts: orders behind the effective PF {tot} but distinct {dis} ({100 * dis / max(1, tot):.1f}%): the Sets of a pack share their entries")
    out["counts"] = {"validated_set_hours": len(ns), "n_min": ns[0] if ns else None, "n_p10": q(0.1), "n_median": q(0.5), "below_8": sum(1 for x in ns if x < 8),
                     "orders_pooled": tot, "orders_distinct": dis}
    if a.out:
        json.dump(out, open(a.out, "w"), indent=1)


if __name__ == "__main__":
    main()
