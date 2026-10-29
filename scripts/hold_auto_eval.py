#!/usr/bin/env python3
"""Hold-time Auto Mode on the walk-forward of stage_eval: every hour each Set judges the candidate holds on its own tapes (its last
holdAutoMax orders, at least holdAutoMin, the global time stop as the reference with the engine's PF margin), keeps the best one,
and is gated and executed with it (the engine's choice, set_engine.SetBook._choose_hold). Compared with the global time stop on the same
hours: post-Base effective PF, validated share and orders, and the executed PF (one position per symbol, as stage_eval executes).
Choose on the development window; --sealed reads the last third once.

usage: hold_auto_eval.py --data DIR [--cache DIR] [--out FILE] [--sealed]"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import pickle
import statistics
import sys
import time
from collections import OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))
import stage_eval as sg  # noqa: E402
import set_engine as se  # noqa: E402


class AutoTapes:
    """Quacks like stage_eval.Tapes. window() answers with the tape of the hold the Set chose at that hour (and leaves the choice in
    st.hold_bars, which the executed lane reads). mode 'global' = hold 0 only."""

    def __init__(self, book, by_hold, mode, cap=se.HOLD_AUTO_MAX_ORDERS, floor=se.HOLD_AUTO_MIN_ORDERS):
        self.book, self.by_hold, self.mode, self.cap, self.floor = book, by_hold, mode, cap, floor
        self.cf = by_hold[0].cf
        self.memo = OrderedDict()
        self.memo_limit = max(128, min(100_000, 4 * len(book.by_idx)))
        self.chosen = {}

    def pf(self, pp):
        g = sum(x - self.cf for x in pp if x - self.cf > 0)
        l_ = -sum(x - self.cf for x in pp if x - self.cf < 0)
        return (g / l_ if l_ > 0 else (99.0 if g > 0 else 0.0)), len(pp)

    def choose(self, idx, now, lb):
        key = (idx, now)
        if key in self.memo:
            self.memo.move_to_end(key)
            self.book.by_idx[idx].hold_bars = self.memo[key]
            return self.memo[key]
        st = self.book.by_idx[idx]
        best = 0
        if self.mode == "auto":
            holds = se.HOLD_AUTO_LOW if self.book.step_of(st) <= 5 else se.HOLD_AUTO_HIGH
            ref, _ = self.pf(self.by_hold[0].window(idx, now, lb, self.cap)[2])
            top = ref
            for h in holds:
                pf, n = self.pf(self.by_hold[h].window(idx, now, lb, self.cap)[2])
                if n >= self.floor and pf > top and pf >= ref * se.HOLD_AUTO_MARGIN:
                    top, best = pf, h
        self.memo[key] = best
        while len(self.memo) > self.memo_limit:
            self.memo.popitem(last=False)
        st.hold_bars = best
        self.chosen[best] = self.chosen.get(best, 0) + 1
        return best

    def window(self, idx, now, lookback_s, n):
        return self.by_hold[self.choose(idx, now, lookback_s)].window(idx, now, lookback_s, n)

    def tape_source(self, idx, now, lookback_s):
        return self.by_hold[self.choose(idx, now, lookback_s)]

    def next_change_time(self, idx, now, lookback_s):
        """A different hold's next close or expiry can change the selected tape."""
        if self.mode != "auto":
            return float("inf")
        st = self.book.by_idx[idx]
        holds = se.HOLD_AUTO_LOW if self.book.step_of(st) <= 5 else se.HOLD_AUTO_HIGH
        until = float("inf")
        for hold in (0,) + holds:
            tx = self.by_hold[hold].by_set[idx][0]
            end = bisect.bisect_left(tx, now)
            lo = bisect.bisect_left(tx, now - lookback_s)
            if end < len(tx):
                until = min(until, float(tx[end]))
            if lo < end:
                until = min(until, float(tx[max(lo, end - self.cap)]) + lookback_s)
        return until


def pool(book, tapes, hour):
    cf, lb = tapes.cf, book.lookback * 60.0
    tot = val = n_v = 0
    gp_a = gl_a = gp_v = gl_v = 0.0
    for st in book.by_idx:
        ok = sg.validation_lane(book, tapes, st, hour)
        tx, te, pp = tapes.window(st.idx, hour, lb, max(book.gate_window, book.deact_n))
        w = pp[-book.gate_window:]
        gp = sum(x - cf for x in w if x - cf > 0)
        gl = -sum(x - cf for x in w if x - cf < 0)
        tot += 1
        gp_a, gl_a = gp_a + gp, gl_a + gl
        if ok:
            val += 1
            gp_v, gl_v, n_v = gp_v + gp, gl_v + gl, n_v + len(w)
    return {"total": tot, "valid": val, "pf_pre": gp_a / gl_a if gl_a > 0 else None,
            "pf_post": gp_v / gl_v if gl_v > 0 else (99.0 if gp_v > 0 else None), "orders": n_v}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--cache", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--sealed", action="store_true")
    a = ap.parse_args()
    names, bars, times = sg.load_bars(a.data, a.symbols)
    holds = [0] + sorted({*se.HOLD_AUTO_LOW, *se.HOLD_AUTO_HIGH})
    shadows = {}
    for H in holds:
        f = os.path.join(a.cache, f"shadow_h{H}.pkl") if a.cache else ""
        if f and os.path.exists(f):
            shadows[H] = pickle.load(open(f, "rb"))
            continue
        t0 = time.time()
        patch = {"holdEnabled": False} if H == 0 else {"holdAuto": False, "stepHoldOn": [True] * 12, "stepHoldMin": [H] * 12}
        os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(patch)
        shadows[H] = sg.build(names, bars, times, a.jobs)
        if f:
            os.makedirs(a.cache, exist_ok=True)
            pickle.dump(shadows[H], open(f, "wb"))
        print(f"built hold {H} [{time.time() - t0:.0f}s]", flush=True)
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps({"holdEnabled": False})
    book = sg.make_book()
    by_hold = {H: sg.Tapes(book, names, times, shadows[H]) for H in holds}
    start = max(times[s][0] for s in names) + book.lookback * 60
    end = min(times[s][-1] for s in names)
    hours = [float(h) for h in range(int(start // 3600 + 1) * 3600, int(end), 3600)]
    split = hours[len(hours) * 2 // 3]
    lo, hi = (split, hours[-1] + 3600) if a.sealed else (hours[0], split)
    run_hours = [h for h in hours if lo <= h < hi]
    sample = run_hours[:: max(1, len(run_hours) // 24)]
    res = {}
    for mode in ("global", "auto"):
        tapes = AutoTapes(book, by_hold, mode)
        pools = [pool(book, tapes, h) for h in sample]
        eff = [p["pf_post"] for p in pools if p["pf_post"] is not None]
        pre = [p["pf_pre"] for p in pools if p["pf_pre"] is not None]
        for st in book.by_idx:
            st.hold_bars = 0
        rows = sg.execute(book, names, bars, times, shadows[0], tapes, run_hours)
        r = sg.summarize(rows, lo, hi, False)
        tot = pools[0]["total"] if pools else 0
        res[mode] = {"exec_pf": r["pf"], "exec_orders": r["orders"], "net_pct": r["net_pct"], "max_dd_pct": r["max_dd_pct"], "hours_pos": r["hours_positive"],
                     "eff_pf": round(statistics.median(eff), 3) if eff else None,
                     "valid_pct": round(100 * sum(p["valid"] for p in pools) / max(1, tot * len(pools)), 2),
                     "orders_valid": round(sum(p["orders"] for p in pools) / max(1, len(pools))),
                     "pf_unfiltered": round(statistics.median(pre), 3) if pre else None,
                     "chosen_hold_counts": dict(sorted(tapes.chosen.items()))}
        print(("SEALED " if a.sealed else "dev    ") + f"{mode:6s}", json.dumps(res[mode]), flush=True)
    if a.out:
        json.dump(res, open(a.out, "w"), indent=1)


if __name__ == "__main__":
    main()
