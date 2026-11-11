#!/usr/bin/env python3
"""Reference progress: the whole Set process rebuilt from the written rules, independent of the engine's code.

The only engine input is the per-bar signal of each pack (set_engine.pack_signals: direction and confidence). Everything else is plain code here:

  orders     per Set (pack x direction x TP step x SL ratio, base type: stop, target, time exit, no scratch): entry at the close of a signal bar of the
             Set's direction, stop first when a bar touches both, cooldown after a close, one position per Set and symbol
  Base       on the Set's own tape: the last 50 and the last 25 orders closed before the entry (inside the 24 h lookback) at the Set's floor
             (1 + TP % x 0.2), and the drawdown time of the last 50 under 24 h
  Main Set   a NEW Set: the orders the Set opened while Base passed. Main gate on the Main Set: its last 25 and last 10 closed before the entry
  Real Set   a NEW Set: the Main orders opened while Main passed. Real window: its last 15 closed before the entry ("executable")
  execution  one position per symbol: at each signal bar the executable order of the best-ranked Set of that direction (its Real Set PF shrunk
             toward 1 by its orders), never while the symbol holds a position

Per gate it prints the test-window orders, the PF they delivered, the PF of the window that admitted them (in-sample), the PF with each entry
counted once (the mean of the Sets that took it), the Sets, and the executed PF. Pause off, hold Auto off, Exit and trailing Sets not built;
these are the differences to the engine, stated, not hidden.

usage: reference_progress.py --data DIR [--symbols 20] [--hours 24] [--lookback 1440] [--jobs 4]"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))

COST = 0.0012                 # one PositionCost per order (0.12 %)
STEPS = (2, 3, 4, 6, 8, 10, 13, 16, 20, 25, 32)
RATIOS = (0.5, 1.0, 1.5, 2.0, 3.0)
TIME_BARS = 360               # timeStopS 21600
COOLDOWN = 2
CONF = None                   # filled from the book (setEntryConf)
BASE_LONG, BASE_SHORT, MAIN_LONG, MAIN_SHORT, REAL_N = 50, 25, 25, 10, 15
DDT_S = 24 * 3600.0
RATIO_FLOOR = 0.2


def floor_of(step: int) -> float:
    return round(1.0 + (step * 0.1) * RATIO_FLOOR, 6)


def pf(v):
    g = sum(x for x in v if x > 0)
    l_ = -sum(x for x in v if x < 0)
    return g / l_ if l_ > 0 else (99.0 if g > 0 else 0.0)


def signals_job(args):
    s, rows, t0 = args
    os.environ.setdefault("STAGE_OVERLAY_PATCH", json.dumps({"holdEnabled": False, "histLookbackBars": 1440, "histLookbackAuto": False}))
    import stage_eval as sg
    import set_engine as se
    b = sg.make_book()
    warm = min(b.warmup, max(16, len(rows) // 5))
    sig = se.pack_signals(rows, b.packs, b.ind_settings, float(t0) + 60.0, warm)
    return s, {p: [(int(d), float(c)) for d, c, *_ in sig[p]] for p in b.packs}, float(b.entry_conf)


def orders_job(args):
    """All orders of every Set on one symbol: {(pack, side, step, ratio): [(entry t, exit t, net, entry bar)]}."""
    s, rows, times, sig, conf = args
    hi = np.array([float(r[1]) for r in rows])
    lo = np.array([float(r[2]) for r in rows])
    cl = np.array([float(r[3]) for r in rows])
    n = len(rows)
    out = {}
    for pack, ps in sig.items():
        for side in (1, -1):
            cand = [i for i, (d, c) in enumerate(ps) if d == side and c >= conf]
            for step in STEPS:
                tp_f = max(COST, step * 0.001)
                for ratio in RATIOS:
                    sl_f = max(2 * COST, ratio * step * 0.001)
                    res = []
                    free = 0
                    for i in cand:
                        if i < free or i + 1 >= n:
                            continue
                        e = cl[i]
                        j1 = min(n, i + 1 + TIME_BARS)
                        if side > 0:
                            sl, tp = e * (1 - sl_f), e * (1 + tp_f)
                            hit_sl = lo[i + 1:j1] <= sl
                            hit_tp = hi[i + 1:j1] >= tp
                        else:
                            sl, tp = e * (1 + sl_f), e * (1 - tp_f)
                            hit_sl = hi[i + 1:j1] >= sl
                            hit_tp = lo[i + 1:j1] <= tp
                        k_sl = int(np.argmax(hit_sl)) if hit_sl.any() else None
                        k_tp = int(np.argmax(hit_tp)) if hit_tp.any() else None
                        if k_sl is not None and (k_tp is None or k_sl <= k_tp):
                            j, px = i + 1 + k_sl, sl
                        elif k_tp is not None:
                            j, px = i + 1 + k_tp, tp
                        elif i + TIME_BARS < n:
                            j, px = i + TIME_BARS, cl[i + TIME_BARS]
                        else:
                            break                                   # still open at the end of the data
                        res.append((times[i] + 60.0, times[j] + 60.0, (px - e) / e * side - COST, i))
                        free = j + COOLDOWN + 1
                    out[(pack, side, step, ratio)] = res
    return s, out


def closed_before(ts, entry, lb, last):
    e = bisect.bisect_left(ts, entry)
    lo_ = bisect.bisect_left(ts, entry - lb)
    return list(range(max(lo_, e - last), e))


def dd_ok(ts, nets, idx, entry):
    eq = peak = 0.0
    peak_t = ts[idx[0]] if idx else entry
    worst, under = 0.0, False
    for j in idx:
        eq += nets[j]
        if eq >= peak - 1e-12:
            if under:
                worst = max(worst, ts[j] - peak_t)
            peak, peak_t, under = max(peak, eq), ts[j], False
        else:
            under = True
    if under:
        worst = max(worst, entry - peak_t)
    return worst < DDT_S


def chain(tape, floor, lb):
    """tape: [(entry t, exit t, net, symbol, bar)] sorted by exit. Returns base/main/real/exec membership flags and the selecting PFs."""
    ts = [o[1] for o in tape]
    nets = [o[2] for o in tape]
    main, sel_base = [], {}
    for k, o in enumerate(tape):
        idx = closed_before(ts, o[0], lb, BASE_LONG)
        if not idx:
            continue
        w = [nets[j] for j in idx]
        if round(pf(w), 4) + 1e-9 < floor or round(pf(w[-BASE_SHORT:]), 4) + 1e-9 < floor or not dd_ok(ts, nets, idx, o[0]):
            continue
        main.append(k)
        sel_base[k] = w
    mt = [ts[k] for k in main]
    mn = [nets[k] for k in main]
    real, sel_main = [], {}
    for j, k in enumerate(main):
        idx = closed_before(mt, tape[k][0], lb, MAIN_LONG)
        if not idx:
            continue
        w = [mn[q] for q in idx]
        if round(pf(w), 4) + 1e-9 < floor or round(pf(w[-MAIN_SHORT:]), 4) + 1e-9 < floor:
            continue
        real.append(k)
        sel_main[k] = w
    rt = [ts[k] for k in real]
    rn = [nets[k] for k in real]
    execu, sel_real, rank = [], {}, {}
    for j, k in enumerate(real):
        idx = closed_before(rt, tape[k][0], lb, REAL_N)
        if not idx:
            continue
        w = [rn[q] for q in idx]
        if round(pf(w), 4) + 1e-9 < floor:
            continue
        execu.append(k)
        sel_real[k] = w
        allr = [rn[q] for q in closed_before(rt, tape[k][0], lb, 200)]
        nn = len(allr)
        rank[k] = 1.0 + (min(pf(allr), 10.0) - 1.0) * nn / (nn + 60)
    return main, real, execu, sel_base, sel_main, sel_real, rank


def main_() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--lookback", type=int, default=1440)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    t_start = time.time()
    import stage_eval as sg
    names, bars, times = sg.load_bars(a.data, a.symbols)
    with ProcessPoolExecutor(a.jobs) as pool:
        sigs = list(pool.map(signals_job, [(s, bars[s], times[s][0]) for s in names]))
    conf = sigs[0][2]
    sig_of = {s: d for s, d, _ in sigs}
    print(f"signals [{time.time() - t_start:.0f}s]", flush=True)
    with ProcessPoolExecutor(a.jobs) as pool:
        per_sym = list(pool.map(orders_job, [(s, bars[s], times[s], sig_of[s], conf) for s in names]))
    tapes = defaultdict(list)
    for s, out in per_sym:
        for key, res in out.items():
            for (te, tx, net, i) in res:
                tapes[key].append((te, tx, net, s, i))
    for v in tapes.values():
        v.sort(key=lambda o: o[1])
    print(f"orders: {sum(len(v) for v in tapes.values())} on {len(tapes)} Sets [{time.time() - t_start:.0f}s]", flush=True)
    data_end = min(times[s][-1] for s in names)
    t0 = float(int(data_end // 3600) * 3600) - a.hours * 3600.0
    lb = a.lookback * 60.0
    gates = {g: [[], [], set()] for g in ("tape", "base", "main", "exec")}     # nets, selecting nets, Sets
    per_entry = {g: defaultdict(list) for g in gates}
    cands = defaultdict(list)                                                  # (symbol, bar, side) -> [(rank, exit bar, net, key)]
    for key, tape in tapes.items():
        floor = floor_of(key[2])
        mainl, reall, execl, sb, sm, sr, rank = chain(tape, floor, lb)
        for g, members, sel in (("tape", range(len(tape)), None), ("base", mainl, sb), ("main", reall, sm), ("exec", execl, sr)):
            for k in members:
                o = tape[k]
                if o[0] < t0:
                    continue
                gates[g][0].append(o[2])
                if sel is not None:
                    gates[g][1].extend(sel[k])
                gates[g][2].add(key)
                per_entry[g][(o[3], o[4], key[0], key[1])].append(o[2])
        for k in execl:
            o = tape[k]
            if o[0] >= t0:
                cands[(o[3], o[4], key[1])].append((rank[k], o[1], o[2], key))
    # execution: one position per symbol, the best-ranked executable order at each signal bar
    by_sym = defaultdict(list)
    for (s, i, side), lst in cands.items():
        by_sym[s].append((i, max(lst, key=lambda r: (r[0], -r[1]))))
    executed = []
    for s, lst in by_sym.items():
        busy_until = -1.0
        for i, best in sorted(lst):
            entry_t = times[s][i] + 60.0
            if entry_t < busy_until:
                continue
            executed.append(best[2])
            busy_until = best[1]
    res = {"meta": {"symbols": names, "test_start": int(t0), "hours": a.hours, "sets": len(tapes)}}
    print(f"\nreference progress: {len(names)} symbols, test {time.strftime('%m-%d %H:%M', time.gmtime(t0))} UTC + {a.hours} h, {len(tapes)} Sets "
          f"(3 packs x 2 directions x {len(STEPS)} steps x {len(RATIOS)} SL ratios, base type)")
    print(f"  {'gate':<34s}{'orders':>9s}{'PF':>8s}{'selected at':>13s}{'per entry PF':>14s}{'entries':>9s}{'Sets':>7s}")
    label = {"tape": "every order (no gate)", "base": "Main Set (Base passed at entry)", "main": "Real Set (Main passed at entry)",
             "exec": "Real window at entry (executable)"}
    for g in gates:
        nets, sel, sets_ = gates[g]
        pe = [sum(v) / len(v) for v in per_entry[g].values()]
        row = {"orders": len(nets), "pf": round(pf(nets), 3), "selected": round(pf(sel), 3) if sel else None, "per_entry_pf": round(pf(pe), 3),
               "entries": len(pe), "sets": len(sets_)}
        res[g] = row
        print(f"  {label[g]:<34s}{row['orders']:>9d}{row['pf']:>8.3f}{str(row['selected']):>13s}{row['per_entry_pf']:>14.3f}{row['entries']:>9d}{row['sets']:>7d}")
    res["executed"] = {"orders": len(executed), "pf": round(pf(executed), 3), "net_pct": round(100 * sum(executed), 2)}
    print(f"  executed (one position per symbol, best-ranked executable Set): PF {res['executed']['pf']} on {len(executed)} orders, net {res['executed']['net_pct']} %")
    print(f"done [{time.time() - t_start:.0f}s]")
    if a.out:
        json.dump(res, open(a.out, "w"))
    return 0


if __name__ == "__main__":
    sys.exit(main_())
