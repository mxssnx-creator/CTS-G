#!/usr/bin/env python3
"""Main-stage audit: why the PF reads lower after a stage than at the selection that opened it, and whether any of it is a defect.

The question (owner, 2026-10-29): the effective PF after Base is high (1.6-2.8) but the PF of what the Main stage and the later stages deliver is lower.
Is that a logic or accounting defect, or the plain consequence of selecting on a window? This script answers it on stored 1m history (a fresh test window
after a pre-history window), shadow tapes per Set exactly as the engine replays them, the engine's own chain (set_engine.stage_chain):

  A  Accounting and independence. The chain of a sample of Sets is recomputed by a plain reading of the rules (no prefix sums, no shortcuts) and compared
     with the engine's (identical lists required); Real is a subset of Main is a subset of the Base tape; costs: every stop and target exit equals its lane
     distance (set_engine.lane_fracs) and every net is that gross minus one PositionCost, once.
  B  Zero-edge benchmark. A random entry with the Set's stop and target has an exact expected PF below 1 (win rate = stop / (stop + target), a cost on
     every order); the observed PF of the unfiltered tapes is set beside it per step range, with the mean gross result per order against the cost.
  C  The stage ladder, in-sample against realised: for each lane (Base tape, Main Set, Real Set, Real Negative Set) the PF of the window that selected the
     order (what the Set looked like when it was picked) next to the PF the order's own result delivered. The first is high because it is the selection,
     the second is what follows it; the gap is the selection effect, not a loss created by a stage.
  D  Placebo. The same chain on tapes whose results are shuffled inside each Set (times and entries kept, any persistence destroyed). Whatever the real
     ladder shows above the placebo is persistence; whatever equals it is selection bias alone.
  E  Persistence by decile. Every (Set, hour) pair by its window PF at the hour start; the PF of the Set's next orders. A selection rule can only hold
     its PF out of sample if this curve rises.
  F  Lag. The Base verdict reads closed orders; positions still open when an order is opened are not in it. Realised PF by the number of the Set's own
     positions still open at the entry.
  G  Exit reasons and hold times per lane. H  By category and type. I  The live engine's own figures (stats file), when given.

Windows. One 24 h test window is a thin base for statements about persistence, so --windows N walks N consecutive days back from the end of the data (each a
24 h test after 24 h of pre-history, 53 h of 1m bars per symbol), pools the raw gains and losses of every figure over the windows (the pooled PF, never the
mean of PFs) and reports how often each lane and group held above 1 window by window.

usage: main_stage_audit.py --data DIR --out FILE.json [--txt FILE.txt] [--windows 1] [--end EPOCH] [--cache rows.pkl] [--hours 24] [--lookback 1440]
                           [--jobs 4] [--shuffles 2] [--sample 120] [--live stats.json] [--patch JSON]"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import pickle
import random
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))
import set_engine as se  # noqa: E402
import stage_audit as sa  # noqa: E402
import stage_eval as sg  # noqa: E402
from position_cost import cost_as_frac  # noqa: E402

LANES = ("base", "main", "real", "realneg")
LANE_LABEL = {"base": "Base tape (every order of every Set)", "main": "Main Set (opened while Base passed)",
              "real": "Real Set (opened while Base and Main passed)", "realneg": "Real Negative Set (opened in a Main dip)"}


# ---- A. an independent reading of the rules -----------------------------------------------------------------------------------------------------

def _pf(v: Sequence[float]) -> float:
    gp, gl = sum(x for x in v if x > 0), -sum(x for x in v if x < 0)
    return gp / gl if gl > 0 else (99.0 if gp > 0 else 0.0)


def _closed(ts: Sequence[float], entry: float, lookback_s: float, last: Optional[int] = None) -> List[int]:
    """Indices of the lane's orders that closed before `entry` and inside the lookback (ts ascending), the newest `last` of them when given."""
    e = bisect.bisect_left(ts, entry)
    lo = bisect.bisect_left(ts, entry - lookback_s)
    if last is not None:
        lo = max(lo, e - last)
    return list(range(lo, e))


def _dd_ok(ts: Sequence[float], nets: Sequence[float], idx: Sequence[int], entry: float, limit_s: float) -> bool:
    """The longest drawdown over the window's orders (equity from zero, the open drawdown measured to the entry) is under limit_s."""
    if not idx:
        return True
    eq = peak = 0.0
    peak_t = ts[idx[0]]
    worst, under = 0.0, False
    for j in idx:
        eq += nets[j]
        if eq >= peak - 1e-12:
            if under:
                worst = max(worst, ts[j] - peak_t)
                under = False
            peak = max(peak, eq)
            peak_t = ts[j]
        else:
            under = True
    if under:
        worst = max(worst, entry - peak_t)
    return worst < limit_s


def naive_chain(ts: Sequence[float], ents: Sequence[float], nets: Sequence[float], P: "se.StageParams") -> Dict[str, Any]:
    """The Base -> Main -> Real chain read straight from the rules: for every order, the newest N results that closed before its entry, summed in a loop.
    Slow and plain on purpose; set_engine.stage_chain must give the same lists."""
    full = P.full_window
    n = len(ts)

    def win_ok(lane_t, lane_net, entry, w):
        idx = _closed(lane_t, entry, P.lookback_s, w)
        return len(idx) >= (w if full else 1) and round(_pf([lane_net[j] for j in idx]), 12) + 1e-9 >= P.floor, idx

    main = []
    for k in range(n):
        entry = ents[k]
        idx = _closed(ts, entry, P.lookback_s, P.base_long)
        if len(idx) < P.gate_min:
            continue
        if round(_pf([nets[j] for j in idx]), 4) + 1e-9 < P.floor:
            continue
        if P.consistency > 0:
            h = len(idx) // 2
            if min(_pf([nets[j] for j in idx[:h]]), _pf([nets[j] for j in idx[h:]])) + 1e-9 < P.consistency:
                continue
        if not _dd_ok(ts, nets, idx, entry, P.max_dd_s):
            continue
        if P.base_short:
            sw = idx[-P.base_short:]
            if len(sw) < (P.base_short if full else 1) or _pf([nets[j] for j in sw]) + 1e-9 < P.floor:
                continue
        if P.base_min_n:
            mw = [nets[j] for j in idx[-P.base_min_n:]]
            if len(mw) < (P.base_min_n if full else 1) or not se.sign_ok(mw, P.base_min_positive, P.base_min_both):
                continue
        main.append(k)
    mt, mn = [ts[k] for k in main], [nets[k] for k in main]

    cand_ws = [P.main_n] + [int(w) for w in P.short_windows]
    passed = {w: [win_ok(mt, mn, ents[main[j]], w)[0] for j in range(len(main))] for w in cand_ws}

    def pick(idx):
        """The Main short window the Main orders `idx` (closed before the entry, results known) vote for: the candidate with the best PF over the orders that had
        passed it, at least short_min of them and short_margin x the long window's PF, else the long window."""
        ref = _pf([mn[i] for i in idx if passed[P.main_n][i]])
        best, sel = ref, P.main_n
        for w in P.short_windows:
            inc = [mn[i] for i in idx if passed[int(w)][i]]
            if len(inc) < P.short_min:
                continue
            pf = _pf(inc)
            if pf > best + se.SHORT_EPS and pf >= ref * P.short_margin - se.SHORT_EPS:
                best, sel = pf, int(w)
        return sel

    short_n = P.main_n
    choice = [P.main_n] * len(main)
    if P.main_on and P.short_on:
        short_n = pick(range(len(main)))
        choice = [pick(_closed(mt, ents[main[j]], P.lookback_s)) for j in range(len(main))]
    real = []
    for j, k in enumerate(main):
        entry = ents[k]
        if P.main_on:
            if not passed[P.main_n][j] or not passed[choice[j]][j]:
                continue
            if P.main_min_n:
                kw = [mn[i] for i in _closed(mt, entry, P.lookback_s, P.main_min_n)]
                if len(kw) < (P.main_min_n if full else 1) or not se.sign_ok(kw, P.main_min_positive, P.main_min_both):
                    continue
        real.append(k)
    realneg = []
    for j, k in enumerate(main):
        if not P.neg_on:
            break
        entry = ents[k]
        for w in P.neg_windows:
            idx = _closed(mt, entry, P.lookback_s, w)
            if len(idx) >= w and sum(mn[i] for i in idx) < -se.NEG_EPS:
                realneg.append(k)
                break
    return {"main": main, "real": real, "realneg": realneg, "short_n": short_n, "short_by_order": choice}


# ---- the rows ---------------------------------------------------------------------------------------------------------------------------------

def build_rows(a, names, bars, times, t_lo: Optional[float] = None, t_hi: Optional[float] = None):
    """Shadow tapes of every Set over the bars of [t_lo, t_hi) (seconds): {idx: [(exit t, entry t, gross, reason)]} sorted by exit, and the test hours."""
    patch = {"holdEnabled": False, "histLookbackBars": a.lookback, "histLookbackAuto": False, **(json.loads(a.patch) if a.patch else {})}
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(patch)
    book = sg.make_book()
    sb, st_ = {}, {}
    for s in names:
        lo = 0 if t_lo is None else bisect.bisect_left(times[s], t_lo)
        hi = len(times[s]) if t_hi is None else bisect.bisect_left(times[s], t_hi)
        sb[s], st_[s] = bars[s][lo:hi], times[s][lo:hi]
    if a.cache and os.path.exists(a.cache) and a.windows == 1:
        rows = pickle.load(open(a.cache, "rb"))
    else:
        with ProcessPoolExecutor(max_workers=a.jobs) as pool:
            res = dict(pool.map(sa._job, [(s, sb[s], st_[s][0]) for s in names]))
        rows = {}
        for st in book.by_idx:
            r = []
            for s in names:
                ts = st_[s]
                for (ei, xi, pp, why) in res[s][st.idx]:
                    r.append((ts[xi] + 60.0, ts[ei] + 60.0, pp, why))
            r.sort()
            rows[st.idx] = r
        if a.cache and a.windows == 1:
            pickle.dump(rows, open(a.cache, "wb"))
    start = max(st_[s][0] for s in names) + book.lookback * 60
    end = min(st_[s][-1] for s in names)
    hours = [float(h) for h in range(int(start // 3600 + 1) * 3600, int(end), 3600)][-a.hours:]
    return book, rows, hours


class Tot:
    """Raw totals by key: gains, losses, orders, wins and one extra sum. Windows add into it; a PF is always read as gains over losses of the sums."""

    def __init__(self):
        self.d: Dict[str, List[float]] = defaultdict(lambda: [0.0, 0.0, 0, 0, 0.0])

    def add(self, key: str, x: float, extra: float = 0.0):
        v = self.d[key]
        v[2] += 1
        v[4] += extra
        if x > 0:
            v[0] += x
            v[3] += 1
        elif x < 0:
            v[1] -= x

    def put(self, key: str, gp: float, gl: float, n: int = 0, w: int = 0, extra: float = 0.0):
        v = self.d[key]
        v[0] += gp
        v[1] += gl
        v[2] += n
        v[3] += w
        v[4] += extra

    def merge(self, other: "Tot"):
        for k, o in other.d.items():
            self.put(k, o[0], o[1], o[2], o[3], o[4])

    def pf(self, key: str) -> Optional[float]:
        v = self.d.get(key)
        if not v:
            return None
        return round(v[0] / v[1], 3) if v[1] > 0 else (None if v[0] <= 0 else 99.0)

    def n(self, key: str) -> int:
        v = self.d.get(key)
        return int(v[2]) if v else 0


def window_sums(ts, gpre, lpre, entry, n, lb):
    e = bisect.bisect_left(ts, entry)
    s0 = max(bisect.bisect_left(ts, entry - lb), e - n)
    return gpre[e] - gpre[s0], lpre[e] - lpre[s0]


def ladder(book, rows, t0: float, tot: Tot, tag: str, shuffle: Optional[random.Random] = None) -> None:
    """C / D: per lane the in-sample selection PF (the window that selected each order, pooled) and the realised results of the lane's orders in the test
    window, into tot under lad.<tag>.<lane>. shuffle: permute the net results inside each Set first (the placebo)."""
    cf = cost_as_frac(book.cost_pct)
    for st in book.by_idx:
        r = rows[st.idx]
        if not r:
            continue
        ts, ents = [x[0] for x in r], [x[1] for x in r]
        nets = [x[2] - cf for x in r]
        if shuffle is not None:
            shuffle.shuffle(nets)
        P = book.stage_params(st.pack)
        ch = se.stage_chain(ts, ents, nets, P)
        gpre, lpre = se.prefix_sums(nets)
        mt = [ts[k] for k in ch["main"]]
        mg, ml = se.prefix_sums([nets[k] for k in ch["main"]])
        member = {"base": range(len(r)), "main": ch["main"], "real": ch["real"], "realneg": ch["realneg"]}
        for lane, idxs in member.items():
            for k in idxs:
                if ents[k] < t0:
                    continue
                tot.add(f"lad.{tag}.{lane}", nets[k])
                g, l_ = window_sums(ts, gpre, lpre, ents[k], P.base_long, P.lookback_s)
                tot.put(f"lad.{tag}.{lane}.selbase", g, l_)
                if lane == "real":
                    g2, l2 = window_sums(mt, mg, ml, ents[k], P.main_n, P.lookback_s)
                    tot.put(f"lad.{tag}.real.selmain", g2, l2)


def zero_edge_pf(sl: float, tp: float, cost: float) -> float:
    """The expected PF of a random entry with a stop at -sl and a target at +tp (price fractions), one cost per order, a driftless continuous path:
    the target is hit first with probability sl / (sl + tp)."""
    p = sl / (sl + tp)
    return p * (tp - cost) / ((1 - p) * (sl + cost))


def spearman(x: Sequence[float], y: Sequence[float]) -> Optional[float]:
    n = len(x)
    if n < 10:
        return None

    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for m in range(i, j + 1):
                r[order[m]] = (i + j) / 2.0 + 1
            i = j + 1
        return r
    rx, ry = ranks(x), ranks(y)
    mx, my = sum(rx) / n, sum(ry) / n
    sxx, syy = sum((a - mx) ** 2 for a in rx), sum((b - my) ** 2 for b in ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    return round(sxy / math.sqrt(sxx * syy), 4) if sxx > 0 and syy > 0 else None


def check_accounting(book, rows, a, cf) -> Dict[str, Any]:
    """A: the plain reading against the engine's chain, the subset relations and the cost / range identities."""
    rnd = random.Random(7)
    pick = rnd.sample(book.by_idx, min(a.sample, len(book.by_idx)))
    mism, checked = [], 0
    for st in pick:
        r = rows[st.idx]
        if len(r) < 30:
            continue
        r = r[-2500:]
        ts, ents, nets = [x[0] for x in r], [x[1] for x in r], [x[2] - cf for x in r]
        P = book.stage_params(st.pack)
        checked += len(r)
        if se.stage_chain(ts, ents, nets, P) != naive_chain(ts, ents, nets, P):
            mism.append(st.id)
    subset_bad = n_main = n_real = 0
    for st in book.by_idx:
        r = rows[st.idx]
        if not r:
            continue
        ch = se.stage_chain([x[0] for x in r], [x[1] for x in r], [x[2] - cf for x in r], book.stage_params(st.pack))
        m_, r_, q_ = set(ch["main"]), set(ch["real"]), set(ch["realneg"])
        subset_bad += int(not (r_ <= m_ and q_ <= m_))
        n_main += bool(m_)
        n_real += bool(r_)
    dev_tp = dev_sl = 0.0
    n_tp = n_sl = 0
    for st in book.by_idx:
        if st.kind not in ("base", "exit"):
            continue
        sl_f, tp_f = se.lane_fracs(st, book.cost_pct, book.sl_min_steps)
        for (_x, _e, pp, why) in rows[st.idx]:
            w = str(why).split(":")[0]
            if w == "sl":
                dev_sl = max(dev_sl, abs(pp + sl_f))
                n_sl += 1
            elif w == "tp" and st.kind == "base":
                dev_tp = max(dev_tp, abs(pp - tp_f))
                n_tp += 1
    return {"sets_recomputed_by_plain_reading": len(pick), "orders_checked": checked, "chains_that_differ": len(mism), "differing_sets": mism[:5],
            "chains_where_real_or_realneg_is_not_inside_main": subset_bad, "sets_with_a_main_set": n_main, "sets_with_a_real_set": n_real,
            "stop_exits_checked": n_sl, "stop_exit_max_deviation_from_lane_distance": dev_sl, "target_exits_checked": n_tp,
            "target_exit_max_deviation_from_lane_distance": dev_tp, "cost_frac": cf}


def collect(book, rows, hours, a, tot: Tot) -> Dict[str, Any]:
    """B-H for one window into tot; returns the window's own figures for the stability table."""
    cf = cost_as_frac(book.cost_pct)
    t0, lb = hours[0], book.lookback * 60.0
    wtot = Tot()
    # B
    for st in book.by_idx:
        sl_f, tp_f = se.lane_fracs(st, book.cost_pct, book.sl_min_steps)
        rid = sa.range_of(book.step_of(st)) if st.kind != "trail" else "trail"
        for (_x, ent, pp, why) in rows[st.idx]:
            if ent < t0:
                continue
            wtot.put(f"gross.{st.kind}", pp, 0.0, 1)
            if st.kind == "base":
                wtot.add(f"zero.{rid}", pp - cf, zero_edge_pf(sl_f, tp_f, cf))
    # C / D
    ladder(book, rows, t0, wtot, "real")
    for i in range(a.shuffles):
        ladder(book, rows, t0, wtot, f"placebo{i}", random.Random(100 + i))
    # E
    byentry = {}
    for st in book.by_idx:
        r = sorted(rows[st.idx], key=lambda x: x[1])
        byentry[st.idx] = ([x[1] for x in r], [x[2] - cf for x in r])
    pairs = []
    for st in book.by_idx:
        r = rows[st.idx]
        if len(r) < 20:
            continue
        ts = [x[0] for x in r]
        nets = [x[2] - cf for x in r]
        gpre, lpre = se.prefix_sums(nets)
        en, nn = byentry[st.idx]
        for h in hours:
            e = bisect.bisect_left(ts, h)
            s0 = max(bisect.bisect_left(ts, h - lb), e - book.gate_window)
            if e - s0 < 10:
                continue
            g, l_ = gpre[e] - gpre[s0], lpre[e] - lpre[s0]
            sel = g / l_ if l_ > 0 else (99.0 if g > 0 else 0.0)
            lo = bisect.bisect_left(en, h)
            pairs.append((sel, nn[lo:bisect.bisect_left(en, h + 3600.0)], nn[lo:lo + 25], st.kind))
    pairs.sort(key=lambda p: p[0])
    for kind in ("all", "base", "exit", "trail"):
        ps = pairs if kind == "all" else [p for p in pairs if p[3] == kind]
        for b in range(10):
            seg = ps[b * len(ps) // 10:(b + 1) * len(ps) // 10]
            wtot.put(f"decsel.{kind}.{b + 1}", 0.0, 0.0, len(seg), 0, sum(min(p[0], 5.0) for p in seg))
            for p in seg:
                for x in p[1]:
                    wtot.add(f"dec1.{kind}.{b + 1}", x)
                for x in p[2]:
                    wtot.add(f"dec25.{kind}.{b + 1}", x)
    sx = [min(p[0], 5.0) for p in pairs if len(p[2]) >= 5]
    sy = [min(_pf(p[2]), 5.0) for p in pairs if len(p[2]) >= 5]
    rho = spearman(sx, sy)
    # F / G / H
    for st in book.by_idx:
        r = rows[st.idx]
        if not r:
            continue
        ts, ents, nets = [x[0] for x in r], [x[1] for x in r], [x[2] - cf for x in r]
        ch = se.stage_chain(ts, ents, nets, book.stage_params(st.pack))
        en_sorted = sorted(ents)
        member = {"base": range(len(r)), "main": ch["main"], "real": ch["real"], "realneg": ch["realneg"]}
        for lane, idxs in member.items():
            for k in idxs:
                if ents[k] < t0:
                    continue
                wtot.add(f"rsn.{lane}.{str(r[k][3]).split(':')[0]}", nets[k], ts[k] - ents[k])
                wtot.add(f"hold.{lane}", nets[k], ts[k] - ents[k])
                wtot.add(f"grp.{st.pack}.{st.kind}.{lane}", nets[k])
                if lane in ("base", "main"):
                    n_open = bisect.bisect_left(en_sorted, ents[k]) - bisect.bisect_left(ts, ents[k])
                    b = "0" if n_open <= 0 else "1-2" if n_open <= 2 else "3-5" if n_open <= 5 else "6-10" if n_open <= 10 else "11+"
                    wtot.add(f"lag.{lane}.{b}", nets[k])
    tot.merge(wtot)
    fig: Dict[str, Any] = {"rho": rho, "pairs": len(pairs)}
    for lane in LANES:
        fig[lane] = wtot.pf(f"lad.real.{lane}")
        fig[lane + "_n"] = wtot.n(f"lad.real.{lane}")
    pl = [wtot.pf(f"lad.placebo{i}.main") for i in range(a.shuffles)]
    fig["placebo_main"] = round(sum(x for x in pl if x is not None) / max(1, len([x for x in pl if x is not None])), 3) if pl else None
    pl = [wtot.pf(f"lad.placebo{i}.real") for i in range(a.shuffles)]
    fig["placebo_real"] = round(sum(x for x in pl if x is not None) / max(1, len([x for x in pl if x is not None])), 3) if pl else None
    fig["dec25_low"], fig["dec25_high"] = wtot.pf("dec25.all.1"), wtot.pf("dec25.all.10")
    fig["groups"] = {k[4:]: wtot.pf(k) for k in sorted(wtot.d) if k.startswith("grp.") and k.endswith((".main", ".real", ".realneg"))}
    fig["group_orders"] = {k[4:]: wtot.n(k) for k in sorted(wtot.d) if k.startswith("grp.") and k.endswith((".main", ".real", ".realneg"))}
    return fig


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--txt", default="")
    ap.add_argument("--cache", default="")
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--lookback", type=int, default=1440)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--windows", type=int, default=1, help="consecutive 24 h test windows walked back from the end of the data")
    ap.add_argument("--end", type=float, default=0.0, help="end of the newest window (epoch seconds, default the last full hour of the data)")
    ap.add_argument("--shuffles", type=int, default=2)
    ap.add_argument("--sample", type=int, default=120, help="Sets whose chain is recomputed by the plain reading (first window)")
    ap.add_argument("--live", default="", help="the live engine's stats file (stats-bingx-x02.json) for the cross-check")
    ap.add_argument("--patch", default="")
    a = ap.parse_args()
    t_start = time.time()
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps({"holdEnabled": False, "histLookbackBars": a.lookback, "histLookbackAuto": False})
    names, bars, times = sg.load_bars(a.data, a.symbols)
    data_end = min(times[s][-1] for s in names)
    t_end = a.end or float(int(data_end // 3600 + 1) * 3600)
    span = (a.lookback / 60.0 + a.hours + 5) * 3600.0              # pre-history, test, indicator warm-up
    tot = Tot()
    res: Dict[str, Any] = {"windows": []}
    accounting = None
    for w in range(a.windows):
        hi = t_end - w * a.hours * 3600.0
        book, rows, hours = build_rows(a, names, bars, times, hi - span, hi)
        cf = cost_as_frac(book.cost_pct)
        if w == 0:
            res["meta"] = {"symbols": names, "lookback_bars": a.lookback, "created": int(time.time()), "sets": len(book.by_idx), "cost_pct": book.cost_pct,
                           "config": {"setGateWindow": book.gate_window, "setGateShortN": book.base_short, "setGateMinN": book.base_min_n, "mainGateN": book.main_n,
                                      "mainShortWindows": list(book.short_windows), "mainMinN": book.main_min_n, "realGateN": book.realst_n, "minPf": book.min_pf,
                                      "stageFullWindow": book.stage_full}}
            accounting = check_accounting(book, rows, a, cf)
        fig = collect(book, rows, hours, a, tot)
        fig["test_start"], fig["test_end"] = int(hours[0]), int(hours[-1] + 3600)
        res["windows"].append(fig)
        print(f"window {w + 1}/{a.windows} ({time.strftime('%m-%d %H:%M', time.gmtime(hours[0]))} - {time.strftime('%m-%d %H:%M', time.gmtime(hours[-1] + 3600))} UTC) "
              f"Main {fig['main']} Real {fig['real']} RealNeg {fig['realneg']} Base {fig['base']}  [{time.time() - t_start:.0f}s]", flush=True)
        res["totals"] = {k: v for k, v in tot.d.items()}
        res["accounting"] = accounting
        json.dump(res, open(a.out, "w"))
    res["accounting"] = accounting
    res["totals"] = {k: v for k, v in tot.d.items()}
    if a.live and os.path.exists(a.live):
        try:
            st_ = json.load(open(a.live))
            pb = (st_.get("sets") or {}).get("pfBase") or {}
            ex = st_.get("execution") or {}
            res["live"] = {"stats_file": os.path.basename(a.live), "base": pb.get("base"), "baseOnly": pb.get("baseOnly"), "main": pb.get("main"), "processed": pb.get("processed"),
                           "real": pb.get("real"), "realneg": pb.get("realneg"), "stageSets": pb.get("stageSets"), "unfiltered": pb.get("unfiltered"),
                           "validatedPct": pb.get("validatedPct"), "closed": (ex.get("closed") if isinstance(ex, dict) else None)}
        except (OSError, ValueError) as exc:
            res["live"] = {"error": repr(exc)}
    json.dump(res, open(a.out, "w"))
    txt = render(res, a.shuffles)
    if a.txt:
        open(a.txt, "w", encoding="utf-8").write(txt)
    print(txt)
    print(f"done in {time.time() - t_start:.0f}s")
    A = res["accounting"]
    return 0 if (not A["chains_that_differ"] and not A["chains_where_real_or_realneg_is_not_inside_main"]) else 2


def _f(v, d=3):
    return "–" if v is None else (f"{v:.{d}f}" if isinstance(v, float) else str(v))


def _pfk(d: Dict[str, List[float]], key: str) -> Optional[float]:
    v = d.get(key)
    return None if not v else (round(v[0] / v[1], 3) if v[1] > 0 else (None if v[0] <= 0 else 99.0))


def _nk(d: Dict[str, List[float]], key: str) -> int:
    v = d.get(key)
    return int(v[2]) if v else 0


def render(r: Dict[str, Any], shuffles: int = 2) -> str:
    m, T, W = r["meta"], r["totals"], r["windows"]
    c = m["config"]
    o: List[str] = []
    o.append(f"Main-stage audit · {len(m['symbols'])} symbols · {len(W)} test window(s) of 24 h, {time.strftime('%d %b %H:%M', time.gmtime(W[-1]['test_start']))} - "
             f"{time.strftime('%d %b %H:%M', time.gmtime(W[0]['test_end']))} UTC · pre-history 24 h · {m['sets']} Sets · PositionCost {m['cost_pct']} %")
    o.append(f"config: Base long {c['setGateWindow']} / short {c['setGateShortN']} / minimal {c['setGateMinN']}, Main long {c['mainGateN']} / short {c['mainShortWindows']} / minimal {c['mainMinN']}, "
             f"Real {c['realGateN']}, min PF {c['minPf']}, full windows {c['stageFullWindow']}. Every PF below is gains over losses of the POOLED orders (never a mean of PFs).")
    A = r["accounting"]
    o.append("\nA. ACCOUNTING AND INDEPENDENCE (first window)")
    o.append(f"  chains recomputed by a plain reading of the rules: {A['sets_recomputed_by_plain_reading']} Sets, {A['orders_checked']} orders - differing: {A['chains_that_differ']} {A['differing_sets']}")
    o.append(f"  Real and Real Negative inside Main (every Set): violations {A['chains_where_real_or_realneg_is_not_inside_main']}; Sets with a Main Set {A['sets_with_a_main_set']}, with a Real Set {A['sets_with_a_real_set']}")
    o.append(f"  costs and ranges: {A['stop_exits_checked']} stop exits equal -SL distance (max deviation {A['stop_exit_max_deviation_from_lane_distance']:.1e}), "
             f"{A['target_exits_checked']} target exits equal +TP distance (max deviation {A['target_exit_max_deviation_from_lane_distance']:.1e}); net = gross - {A['cost_frac']:.4f} once per order")
    o.append("\nB. ZERO-EDGE BENCHMARK (base Sets, unfiltered tape: observed PF against the PF of a random entry with the same stop and target and one cost per order)")
    o.append(f"  {'step range':<12s}{'observed PF':>12s}{'zero-edge PF':>14s}{'orders':>10s}")
    for rid in ("2-6", "7-12", "13-20", "21-30"):
        v = T.get(f"zero.{rid}")
        if v:
            o.append(f"  {rid:<12s}{_f(_pfk(T, f'zero.{rid}')):>12s}{_f(v[4] / v[2]):>14s}{int(v[2]):>10d}")
    gr = {k[6:]: T[k] for k in T if k.startswith("gross.")}
    o.append("  mean gross result per order (cost " + f"{m['cost_pct']} %): " + ", ".join(f"{k} {100 * (v[0]) / max(1, v[2]):+.4f} %" for k, v in sorted(gr.items())))
    o.append("\nC / D. THE STAGE LADDER: selection PF (the window that picked each order) against the PF its results delivered; placebo = the same chain on results shuffled inside each Set")
    o.append(f"  {'lane':<48s}{'orders':>10s}{'selection PF':>14s}{'realised PF':>13s}{'placebo realised':>18s}{'real - placebo':>16s}")
    for lane in LANES:
        pl = [_pfk(T, f"lad.placebo{i}.{lane}") for i in range(shuffles)]
        pl = [x for x in pl if x is not None]
        plm = sum(pl) / len(pl) if pl else None
        real = _pfk(T, f"lad.real.{lane}")
        diff = (real - plm) if (plm is not None and real is not None) else None
        spl = [_pfk(T, f"lad.placebo{i}.{lane}.selbase") for i in range(shuffles)]
        spl = [x for x in spl if x is not None]
        o.append(f"  {LANE_LABEL[lane]:<48s}{_nk(T, f'lad.real.{lane}'):>10d}{_f(_pfk(T, f'lad.real.{lane}.selbase')):>14s}{_f(real):>13s}"
                 f"{_f(plm) + (' (sel ' + _f(sum(spl) / len(spl), 2) + ')' if spl else ''):>18s}{('' if diff is None else format(diff, '+.3f')):>16s}")
    o.append(f"  Real Set orders were also selected by the Main long window: its pooled PF at their entry {_pfk(T, 'lad.real.real.selmain')}")
    o.append("\nE. PERSISTENCE: every (Set, hour) by its Base window PF at the hour start (deciles) against the PF of the Set's next orders (pooled over all windows)")
    o.append(f"  {'decile':>6s}{'window PF mean':>16s}{'next hour PF':>14s}{'orders':>9s}{'next 25 PF':>13s}{'orders':>9s}")
    for b in range(1, 11):
        sel = T.get(f"decsel.all.{b}")
        o.append(f"  {b:>6d}{_f(sel[4] / sel[2] if sel and sel[2] else None):>16s}{_f(_pfk(T, f'dec1.all.{b}')):>14s}{_nk(T, f'dec1.all.{b}'):>9d}{_f(_pfk(T, f'dec25.all.{b}')):>13s}{_nk(T, f'dec25.all.{b}'):>9d}")
    rhos = [w["rho"] for w in W if w.get("rho") is not None]
    if rhos:
        o.append(f"  rank correlation (window PF vs PF of the next 25 orders), per window: mean {sum(rhos) / len(rhos):+.4f}, min {min(rhos):+.4f}, max {max(rhos):+.4f}, "
                 f"negative in {sum(1 for x in rhos if x < 0)} of {len(rhos)} windows")
    for kind in ("base", "exit", "trail"):
        lo, hi = _pfk(T, f"dec25.{kind}.1"), _pfk(T, f"dec25.{kind}.10")
        sl, sh = T.get(f"decsel.{kind}.1"), T.get(f"decsel.{kind}.10")
        o.append(f"  {kind:<6s} lowest decile: window PF {_f(sl[4] / sl[2] if sl and sl[2] else None, 2)} -> next 25 PF {_f(lo)} ({_nk(T, f'dec25.{kind}.1')} orders); "
                 f"highest decile: window PF {_f(sh[4] / sh[2] if sh and sh[2] else None, 2)} -> next 25 PF {_f(hi)} ({_nk(T, f'dec25.{kind}.10')} orders)")
    o.append("\nF. LAG: realised PF by the number of the Set's own positions still open when the order was opened")
    for lane in ("base", "main"):
        o.append(f"  {lane:<5s} " + "  ".join(f"[{b}: PF {_f(_pfk(T, f'lag.{lane}.{b}'))} on {_nk(T, f'lag.{lane}.{b}')}]" for b in ("0", "1-2", "3-5", "6-10", "11+") if _nk(T, f"lag.{lane}.{b}")))
    o.append("\nG. EXIT REASONS by lane (share of the lane's orders, PF, average hold in minutes)")
    for lane in LANES:
        tot_n = max(1, _nk(T, f"hold.{lane}"))
        items = sorted(((k.split('.', 2)[2], v) for k, v in T.items() if k.startswith(f"rsn.{lane}.") and v[2] >= 20), key=lambda kv: -kv[1][2])
        o.append(f"  {lane:<8s}" + "  ".join(f"[{k} {100.0 * v[2] / tot_n:.1f}% PF {_f(round(v[0] / v[1], 3) if v[1] > 0 else None)} {v[4] / v[2] / 60.0:.1f}m]" for k, v in items))
    o.append("\nH. BY CATEGORY AND TYPE: pooled PF / orders on the Base tape, Main Set, Real Set, Real Negative Set; and in how many of the windows the lane held above 1")
    groups = sorted({tuple(k.split(".")[1:3]) for k in T if k.startswith("grp.")})
    for pack, kind in groups:
        line = f"  {pack:<12s}{kind:<7s}"
        for lane in LANES:
            key = f"grp.{pack}.{kind}.{lane}"
            line += f"{_f(_pfk(T, key)):>8s}/{_nk(T, key):<9d}"
        if len(W) > 1:
            ws = []
            for lane in ("main", "real", "realneg"):
                vals = [w["groups"].get(f"{pack}.{kind}.{lane}") for w in W]
                vals = [x for x in vals if x is not None]
                ws.append(f"{lane} {sum(1 for x in vals if x >= 1.0)}/{len(vals)}")
            line += "  windows >= 1: " + ", ".join(ws)
        o.append(line)
    if len(W) > 1:
        o.append("\nSTABILITY ACROSS THE WINDOWS (PF of the lane in each 24 h window): mean, median, min, max, windows at or above 1")
        for lane in LANES:
            vals = [w[lane] for w in W if w.get(lane) is not None]
            if vals:
                sv = sorted(vals)
                o.append(f"  {lane:<8s} mean {sum(vals) / len(vals):.3f}  median {sv[len(sv) // 2]:.3f}  min {sv[0]:.3f}  max {sv[-1]:.3f}  >= 1 in {sum(1 for x in vals if x >= 1.0)} of {len(vals)}")
        for key, lab in (("placebo_main", "placebo Main"), ("placebo_real", "placebo Real")):
            vals = [w[key] for w in W if w.get(key) is not None]
            if vals:
                o.append(f"  {lab:<8s} mean {sum(vals) / len(vals):.3f}")
        diffs = [w["main"] - w["placebo_main"] for w in W if w.get("main") is not None and w.get("placebo_main") is not None]
        if diffs:
            mu = sum(diffs) / len(diffs)
            sd = math.sqrt(sum((x - mu) ** 2 for x in diffs) / max(1, len(diffs) - 1))
            o.append(f"  Main realised minus Main placebo, per window: mean {mu:+.3f} (standard error {sd / math.sqrt(len(diffs)):.3f}), negative in {sum(1 for x in diffs if x < 0)} of {len(diffs)} windows")
    if r.get("live") and "error" not in r["live"]:
        I = r["live"]
        o.append(f"\nI. THE LIVE ENGINE ({I['stats_file']}): validated {I.get('validatedPct')} %, unfiltered {I.get('unfiltered')}, end figure {I.get('base')}, Base alone {I.get('baseOnly')}")
        o.append(f"   Main {I.get('main')}; Main Sets of the Base-passing Sets {I.get('processed')}; Real {I.get('real')}; Real Negative {I.get('realneg')}; Sets per stage {I.get('stageSets')}")
        if I.get("closed"):
            o.append(f"   closed orders: {I['closed']}")
    return "\n".join(o)


if __name__ == "__main__":
    sys.exit(main())
