#!/usr/bin/env python3
"""Stage evaluation on stored 1m history, walked forward like the live engine: PF after Base, after the coordinator
axes, and with Block legs.

1. Shadow tapes. Every Set runs its own lane over every symbol (SetLane.scan, the replay's code), always on. These tapes
   are what Base validation reads.
2. Base, hour by hour. At the start of each hour every Set is scored from its shadow trades that closed in the lookback
   before that hour (gate window, minimum orders, PF over the window, drawdown time: the engine's rule, checked against
   SetBook._score_one / gate_reason on samples). No look-ahead.
3. Execution, like live. One position per symbol. On a signal bar the pack with the higher confidence decides; the
   position opens under the best valid Set of that pack (SetBook.valid_for order, base before trail) and closes by that
   Set's own lane from that bar (a fresh SetLane). The symbol is busy until the exit.
4. Axis (optional). Before an entry, the coordinator's gate on the executed closes so far: PF of the last N closes at or
   above a minimum, and a pause after K consecutive losses. A Set-level variant asks the chosen Set's own last N shadow
   closes for a minimum PF instead.
5. Block (optional). On an executed position, leg k (k = 1..max stack) is added at entry + k x spacing steps in its
   favour, the first bar that reaches it before the exit bar, one base size each; every leg leaves at the position's exit.

Numbers: PF is net of PositionCost (the sum of net fractions of equal-size positions; Block legs add their size). Max
drawdown is in percent of one position's notional. The development window is the first two thirds of the hours after
the burn-in, the sealed window the last third: choose on dev, read sealed once.

usage: stage_eval.py --data DIR [--symbols 20] [--out stage.json] [--cache cache.pkl]
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import pickle
import sys
import time
from array import array
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
PULSE = os.path.join(HERE, "..", "server", "pulse")
sys.path.insert(0, PULSE)
import set_engine as se  # noqa: E402
from position_cost import cost_as_frac, signed_result_r  # noqa: E402

OVERLAY = os.path.join(PULSE, "overlay-bingx-x01.json")


def load_bars(data_dir: str, n_symbols: int) -> Tuple[List[str], Dict[str, List[List[float]]], Dict[str, List[int]]]:
    names = sorted(f[:-5] for f in os.listdir(data_dir) if f.endswith("-USDT.json"))[:n_symbols]
    bars, times = {}, {}
    for s in names:
        rows = json.load(open(os.path.join(data_dir, s + ".json")))
        rows.sort(key=lambda r: r[0])
        times[s] = [int(r[0]) // 1000 for r in rows]
        bars[s] = [[float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in rows]
    return names, bars, times


def make_book() -> se.SetBook:
    b = se.SetBook()
    ov = json.load(open(OVERLAY))
    patch = os.environ.get("STAGE_OVERLAY_PATCH")      # e.g. {"extraIndicators": ["trend_c10"]}: a variant under test
    if patch:
        ov.update(json.loads(patch))
    b.load(ov)
    return b


def lane_cfg(b: se.SetBook) -> Dict[str, Any]:
    return {"entry_conf": b.entry_conf, "time_bars": b.hist_time_bars, "scratch_bars": b.scratch_bars,
            "scratch_min": b.scratch_min, "cooldown": b.cooldown_bars, "cost_pct": b.cost_pct,
            "honor_tp": bool(getattr(b, "hist_honor_tp", True)), "sl_floor_steps": b.sl_min_steps}


# ---- phase 1: shadow tapes and signals, one worker per symbol ---------------------------------------------------------

def _symbol_job(args):
    s, bars, t0 = args
    b = make_book()
    warm = min(b.warmup, max(16, len(bars) // 5))
    sig = se.pack_signals(bars, b.packs, b.ind_settings, float(t0), warm)
    dirs = {p: [x[0] for x in sig[p]] for p in b.packs}
    confs = {p: [float(x[1]) for x in sig[p]] for p in b.packs}
    cfg = lane_cfg(b)
    acc = se.BarIndex(bars)
    out = {}
    for st in b.by_idx:
        cand = [i for i in range(warm, len(bars)) if dirs[st.pack][i] != 0 and confs[st.pack][i] >= cfg["entry_conf"]]
        lane = se.SetLane(st, s)
        trades = lane.scan(0, len(bars), bars, dirs[st.pack], confs[st.pack], True, cfg, cand, acc)
        ent = array("i", (t["entry_i"] for t in trades))
        ex = array("i", (t["exit_i"] for t in trades))
        pp = array("d", (t["pnl_pct"] for t in trades))
        out[st.idx] = (ent, ex, pp)
    return s, warm, dirs, confs, out


def build(names, bars, times, jobs: int):
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        res = list(pool.map(_symbol_job, [(s, bars[s], times[s][0]) for s in names]))
    return {s: (w, d, c, o) for s, w, d, c, o in res}


# ---- phase 2: the engine's gate, computed directly ----------------------------------------------------------------------

class Tapes:
    """Per Set: every shadow trade over all symbols, ordered by exit time (seconds)."""

    def __init__(self, book, names, times, shadow):
        self.cf = cost_as_frac(book.cost_pct)
        self.by_set: Dict[int, Tuple[array, array, array]] = {}
        for st in book.by_idx:
            rows = []
            for s in names:
                ent, ex, pp = shadow[s][3][st.idx]
                ts = times[s]
                rows.extend((ts[ex[k]] + 60, ts[ent[k]] + 60, pp[k]) for k in range(len(ex)))
            rows.sort()
            self.by_set[st.idx] = (array("d", (r[0] for r in rows)), array("d", (r[1] for r in rows)),
                                   array("d", (r[2] for r in rows)))

    def window(self, idx: int, now: float, lookback_s: float, n: int):
        tx, te, pp = self.by_set[idx]
        end = bisect.bisect_left(tx, now)
        lo = bisect.bisect_left(tx, now - lookback_s)
        start = max(lo, end - n)
        return tx[start:end], te[start:end], pp[start:end]


def gate_metrics(book, tapes: Tapes, st, now: float):
    """(valid, rank, gate_n, gate_pf): the gate and the ranking key exactly as SetBook._score_one / gate_reason /
    rank_key compute them (self_check compares both with the engine)."""
    lb = book.lookback * 60.0
    tx, te, pp = tapes.window(st.idx, now, lb, max(book.gate_window, book.deact_n))
    txa = tapes.by_set[st.idx][0]
    n_all = bisect.bisect_left(txa, now) - bisect.bisect_left(txa, now - lb)     # SetState.n: every order in the lookback
    cf = tapes.cf
    w = pp[-book.gate_window:]
    wt = tx[-book.gate_window:]
    gp = sum(x - cf for x in w if x - cf > 0)
    gl = -sum(x - cf for x in w if x - cf < 0)
    n = len(w)
    pf = round(gp / gl, 4) if gl > 0 else (99.0 if gp > 0 else 0.0)     # rounded as last_n_cost_pf does
    # drawdown time over the last gate_window closes, open drawdown measured to now (se.drawdown_time)
    eq = peak = 0.0
    peak_t = wt[0] if n else now
    in_dd = False
    max_s = 0.0
    for t, x in zip(wt, w):
        eq += x - cf
        if eq >= peak - 1e-12:
            if in_dd:
                max_s = max(max_s, t - peak_t)
                in_dd = False
            peak = max(peak, eq)
            peak_t = t
        else:
            in_dd = True
    if in_dd:
        max_s = max(max_s, now - peak_t)
    last25 = pp[-book.deact_n:]
    r25 = sum(signed_result_r(x, book.cost_pct) for x in last25) / len(last25) if len(last25) else 0.0
    floor = book.pack_min_pf.get(st.pack, book.min_pf)
    valid = n >= book.gate_min and pf + 1e-9 >= floor and max_s < book.max_dd_s

    def _pf(v):
        g = sum(x - cf for x in v if x - cf > 0)
        l_ = -sum(x - cf for x in v if x - cf < 0)
        return g / l_ if l_ > 0 else (99.0 if g > 0 else 0.0)
    h = n // 2
    old, new = (round(_pf(w[:h]), 4), round(_pf(w[h:]), 4)) if h >= 1 else (0.0, 0.0)
    if valid and book.consistency_pf > 0 and n >= 2:
        valid = min(old, new) + 1e-9 >= book.consistency_pf
    halves = min(old, new) if (old or new) else pf
    shrunk = 1.0 + (min(float(pf), se.RANK_PF_CAP) - 1.0) * n_all / (n_all + se.RANK_SHRINK_N)
    rank = (halves + 1e-9 >= floor, round(shrunk, 4), n_all, r25, -max_s)      # SetBook.rank_key
    return valid, rank, n, pf


def self_check(book, tapes: Tapes, hours: Sequence[float], samples: int = 40) -> Tuple[int, int]:
    """The direct gate against the engine's own scoring on sampled (Set, hour) pairs."""
    import random
    rnd = random.Random(5)
    bad = 0
    for _ in range(samples):
        st = rnd.choice(book.by_idx)
        now = rnd.choice(list(hours))
        tx, te, pp = tapes.window(st.idx, now, book.lookback * 60.0, 10 ** 9)
        st.hist = [{"t": float(a), "pnl": float(x) - tapes.cf, "pnl_pct": float(x), "hold_s": float(a - b), "reason": "x"}
                   for a, b, x in zip(tx, te, pp)]
        st.live = []
        book._score_one(st, now=now)
        st.n = len(st.hist)
        eng_valid = book.gate_reason(st) == ""
        valid, key, n, pf = gate_metrics(book, tapes, st, now)
        ek = book.rank_key(st)
        if valid != eng_valid or n != st.gate_n or abs(pf - st.gate_pf) > 1e-6 or key[:3] != ek[:3] or abs(key[3] - ek[3]) > 1e-9 \
                or abs(key[4] - ek[4]) > 1e-6:
            bad += 1
        st.hist = []
    return samples, bad


# ---- phase 3: execution --------------------------------------------------------------------------------------------------

def execute(book, names, bars, times, shadow, tapes, hours, axis=None, set_axis=None, block=None, axis_evidence=None, only_steps=None) -> List[Dict[str, Any]]:
    cfg = lane_cfg(book)
    accs = {s: se.BarIndex(bars[s]) for s in names}
    idx_of = {s: {t: i for i, t in enumerate(times[s])} for s in names}
    busy = {s: -1 for s in names}
    executed: List[Dict[str, Any]] = []
    closes: List[Tuple[float, float]] = []          # (exit t, net) of executed positions, for the axes
    cf = cost_as_frac(book.cost_pct)
    lb = book.lookback * 60.0
    for h in hours:
        ranked: Dict[Tuple[str, str], List[Any]] = {}
        valid_n = 0
        for st in book.by_idx:
            if only_steps is not None and (st.kind == "trail" or st.step not in only_steps):
                continue                      # an evaluation of one step's Sets alone
            ok, key, _, _ = gate_metrics(book, tapes, st, h)
            if ok:
                valid_n += 1
                ranked.setdefault((st.pack, st.kind), []).append((key, st))
        for k in ranked:
            ranked[k].sort(key=lambda r: r[0], reverse=True)
        executed.append({"hour": h, "valid": valid_n, "total": len(book.by_idx)})
        for minute in range(60):
            t = int(h) + minute * 60
            for s in names:
                i = idx_of[s].get(t)
                if i is None or i <= busy[s]:
                    continue
                w, dirs, confs, _ = shadow[s]
                if i < w:
                    continue
                best = None
                for p in book.packs:
                    d, c = dirs[p][i], confs[p][i]
                    if d != 0 and c >= cfg["entry_conf"] and (best is None or c > best[1]):
                        best = (p, c, d)
                if best is None:
                    continue
                pack, conf, d = best
                # SetBook.pick_any: base and Exit Sets ranked together, else the trailing Sets
                pool = sorted(ranked.get((pack, "base"), []) + ranked.get((pack, "exit"), []), key=lambda r: r[0], reverse=True)
                pool = [st for _, st in pool] or [st for _, st in ranked.get((pack, "trail"), [])]
                if set_axis:
                    n_, min_pf = set_axis
                    keep = []
                    for st in pool:
                        tx, te, pp = tapes.window(st.idx, float(t), lb, n_)
                        g = sum(x - cf for x in pp if x - cf > 0)
                        l_ = -sum(x - cf for x in pp if x - cf < 0)
                        if len(pp) >= n_ and (g / l_ if l_ > 0 else 99.0) >= min_pf:
                            keep.append(st)
                    pool = keep
                if not pool:
                    continue
                if axis and not axis_allows(closes if axis_evidence is None else axis_evidence, float(t), axis, cf):
                    continue
                st = pool[0]
                lane = se.SetLane(st, s)
                dd = [0] * len(bars[s])
                cc = [0.0] * len(bars[s])
                dd[i], cc[i] = d, conf
                tr = lane.scan(i, len(bars[s]), bars[s], dd, cc, True, cfg, [i], accs[s])
                if not tr:
                    continue
                tr = tr[0]
                busy[s] = tr["exit_i"]
                legs = block_legs(bars[s], tr, block, book.cost_pct) if block else []
                rec = {"t": float(times[s][tr["exit_i"]] + 60), "t_in": float(t + 60), "symbol": s, "set": st.id,
                       "pack": pack, "kind": st.kind, "reason": tr["reason"], "net": tr["pnl_pct"] - cf, "legs": legs,
                       "tr": tr, "fracs": se.lane_fracs(st, book.cost_pct, book.sl_min_steps)}
                executed.append(rec)
                closes.append((rec["t"], rec["net"]))
    return executed


def axis_allows(closes, now, axis, cf) -> bool:
    last_n, min_pf, pause_k = axis
    done = [x for t, x in closes if t <= now]
    if pause_k:
        k = 0
        for x in reversed(done):
            if x < 0:
                k += 1
            else:
                break
        if k >= pause_k:
            return False
    if last_n and len(done) >= last_n:
        w = done[-last_n:]
        g = sum(x for x in w if x > 0)
        l_ = -sum(x for x in w if x < 0)
        if (g / l_ if l_ > 0 else 99.0) + 1e-9 < min_pf:
            return False
    return True


def block_legs(bars, tr, block, cost_pct) -> List[float]:
    """Net results of the Block legs of one position: leg k at entry + k x spacing steps in its favour, filled on the
    first bar that reaches it strictly before the exit bar, leaving at the position's exit price."""
    spacing, max_stack = block
    cf = cost_as_frac(cost_pct)
    side = int(tr["dir"])
    e = float(tr["entry_px"])
    x = float(tr["exit_px"])
    out = []
    i0, i1 = int(tr["entry_i"]) + 1, int(tr["exit_i"])
    for k in range(1, max_stack + 1):
        lvl = e * (1 + side * k * spacing * cf)
        hit = None
        for j in range(i0, i1):
            if (side > 0 and bars[j][1] >= lvl) or (side < 0 and bars[j][2] <= lvl):
                hit = j
                break
        if hit is None:
            break
        out.append((x - lvl) / lvl * side - cf)
        i0 = hit
    return out


def summarize(rows: List[Dict[str, Any]], lo: float, hi: float, with_legs: bool) -> Dict[str, Any]:
    hours = [r for r in rows if "hour" in r and lo <= r["hour"] < hi]
    trades = [r for r in rows if "net" in r and lo <= r["t_in"] < hi]
    vals = []
    for r in trades:
        vals.append((r["t"], r["net"]))
        if with_legs:
            vals.extend((r["t"], v) for v in r["legs"])
    gp = sum(v for _, v in vals if v > 0)
    gl = -sum(v for _, v in vals if v < 0)
    eq = peak = mdd = 0.0
    for _, v in sorted(vals):
        eq += v
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    by_hour: Dict[int, float] = {}
    for t, v in vals:
        by_hour[int(t // 3600)] = by_hour.get(int(t // 3600), 0.0) + v
    pos_h = sum(1 for v in by_hour.values() if v > 0)
    vp = [100.0 * h["valid"] / h["total"] for h in hours]
    return {"pf": round(gp / gl, 3) if gl > 0 else None, "orders": len(trades), "legs": len(vals) - len(trades),
            "net_pct": round(100 * (gp - gl), 2), "max_dd_pct": round(100 * mdd, 2),
            "hours_positive": f"{pos_h}/{len(by_hour)}", "validated_pct": round(sum(vp) / len(vp), 2) if vp else 0.0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--cache", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--sealed", action="store_true", help="also print the sealed window (finalists only)")
    a = ap.parse_args()
    t0 = time.time()
    names, bars, times = load_bars(a.data, a.symbols)
    if a.cache and os.path.exists(a.cache):
        shadow = pickle.load(open(a.cache, "rb"))
    else:
        shadow = build(names, bars, times, a.jobs)
        if a.cache:
            pickle.dump(shadow, open(a.cache, "wb"))
    book = make_book()
    tapes = Tapes(book, names, times, shadow)
    start = max(times[s][0] for s in names) + book.lookback * 60
    end = min(times[s][-1] for s in names)
    hours = [float(h) for h in range(int(start // 3600 + 1) * 3600, int(end), 3600)]
    split = hours[len(hours) * 2 // 3]
    n_chk, bad = self_check(book, tapes, hours)
    print(f"symbols={len(names)} hours={len(hours)} dev<{time.strftime('%m-%d %H:%M', time.gmtime(split))}<=sealed "
          f"self-check {n_chk - bad}/{n_chk} match the engine's gate  build {time.time() - t0:.0f}s", flush=True)
    if bad:
        print("the direct gate differs from SetBook._score_one / gate_reason: numbers below are not the engine's")
        return 1
    variants = [("base", {}), ]
    for last_n in (4, 8, 16):
        for mpf in (1.0, 1.2, 1.5):
            variants.append((f"axis last{last_n}>={mpf} pause5", {"axis": (last_n, mpf, 5)}))
    variants.append(("axis current last4>=1.2 pause8", {"axis": (4, 1.2, 8)}))
    for n_, mpf in ((5, 1.2), (10, 1.2), (10, 1.5), (20, 1.5)):
        variants.append((f"set-axis last{n_}>={mpf}", {"set_axis": (n_, mpf)}))
    results = {}
    for name, kw in variants:
        rows = execute(book, names, bars, times, shadow, tapes, hours, **kw)
        dev = summarize(rows, hours[0], split, False)
        results[name] = {"dev": dev, "rows": rows}
        print(f"{name:34s} dev  PF {dev['pf']}  orders {dev['orders']}  net {dev['net_pct']}%  maxDD {dev['max_dd_pct']}%  "
              f"hours+ {dev['hours_positive']}  validated {dev['validated_pct']}%", flush=True)
    for spacing in (1, 2, 3):
        rows = execute(book, names, bars, times, shadow, tapes, hours, block=(spacing, 8))
        dev = summarize(rows, hours[0], split, True)
        name = f"base + block 8 legs every {spacing} step(s)"
        results[name] = {"dev": dev, "rows": rows}
        print(f"{name:34s} dev  PF {dev['pf']}  orders {dev['orders']}+{dev['legs']} legs  net {dev['net_pct']}%  "
              f"maxDD {dev['max_dd_pct']}%  hours+ {dev['hours_positive']}", flush=True)
    if a.sealed:
        for name in a.sealed_names if hasattr(a, "sealed_names") else results:
            r = results[name]
            sealed = summarize(r["rows"], split, hours[-1] + 3600, "block" in name)
            r["sealed"] = sealed
            print(f"{name:34s} SEALED PF {sealed['pf']}  orders {sealed['orders']}  net {sealed['net_pct']}%  "
                  f"maxDD {sealed['max_dd_pct']}%  hours+ {sealed['hours_positive']}  validated {sealed['validated_pct']}%")
    if a.out:
        json.dump({k: {kk: vv for kk, vv in v.items() if kk != "rows"} for k, v in results.items()}, open(a.out, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
