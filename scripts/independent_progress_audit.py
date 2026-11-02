#!/usr/bin/env python3
"""Progress audit that does not trust the engine's lane or stage code.

The engine produces, for every Set, its orders (entry bar, exit bar, gross result, reason). Everything below is then re-derived from the raw 1m bars and the
written rules, in plain loops, without set_engine.stage_chain / SetLane / prefix sums:

  1  MARKET TRUTH. For a sample of Sets (base and Exit Sets, both directions) every order is replayed from the bars: the entry is the close of the entry bar, the
     stop and target sit at the Set's distances (TP = step x 0.1 %, SL = ratio x TP, never under (1 + slMinSteps) x PositionCost; an Exit Set has no target), the
     exit is the first bar that touches the stop (stop first when one bar touches both), then the target, then the time exit, then the scratch exit. The exit bar,
     the reason and the gross result must equal the engine's order, and a Set's next order must not start before the previous one closed.
  2  THE GATE LADDER, one gate at a time, on a plain chain (main_stage_audit.naive_chain): the Base tape, + Base long window, + the other Base windows, + the
     Main long window, + the Main short / minimal windows (the Real Set), + the Real window at the entry (what execution would take). For each gate: the orders
     of the test window, the PF they delivered (gains over losses of the pooled net results, one PositionCost per order), the PF of the window that selected
     them (in-sample), the Sets that traded, per direction. A gate that lowers the delivered PF is shown as it is.
  3  MANY LAST-N CONFIGURATIONS. The same ladder's last gate for Base long x short, Base minimal, Main long, Main minimal and Real N, each independent of the others.

The engine only supplies the orders (and the per-Set floor). usage:
  independent_progress_audit.py --data DIR --out FILE.json [--txt FILE.txt] [--symbols 20] [--lookback 1440] [--hours 24] [--jobs 4] [--sample-every 25]
                                [--per-group 6] [--patch JSON]"""
from __future__ import annotations

import argparse
import bisect
import dataclasses
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Dict, List, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))
import main_stage_audit as ma  # noqa: E402
import set_engine as se  # noqa: E402
import stage_audit as sa  # noqa: E402
import stage_eval as sg  # noqa: E402
from position_cost import cost_as_frac  # noqa: E402

G: Dict[str, Any] = {}


# ---- 1. market truth ------------------------------------------------------------------------------------------------------------------------------

def expected_exit(bars: Sequence[Sequence[float]], ei: int, side: int, honor_tp: bool, sl_frac: float, tp_frac: float, cfg: Dict[str, Any]):
    """The first exit after entry bar ei, from the bars alone: (exit bar, reason, price) or None while the data ends."""
    entry = float(bars[ei][3])
    sl = entry * (1 - side * sl_frac)
    tp = entry * (1 + side * tp_frac)
    for j in range(ei + 1, len(bars)):
        hi, lo, cl = float(bars[j][1]), float(bars[j][2]), float(bars[j][3])
        held = j - ei
        if side > 0:
            if lo <= sl:
                return j, "sl", sl
            if honor_tp and hi >= tp:
                return j, "tp", tp
        else:
            if hi >= sl:
                return j, "sl", sl
            if honor_tp and lo <= tp:
                return j, "tp", tp
        if held >= cfg["time_bars"]:
            return j, "time", cl
        if held >= cfg["scratch_bars"] and (cl - entry) / entry * side >= cfg["scratch_min"]:
            return j, "scratch+", cl
    return None


def verify_set(st, trades, bars, cfg, cost_pct: float, sl_min_steps: int, cap: int) -> Dict[str, Any]:
    cost = cost_pct / 100.0
    tp_frac = max(1.0 * cost, st.step * 0.1 / 100.0)
    sl_frac = max((1 + sl_min_steps) * cost, st.sl_ratio * (st.step * 0.1 / 100.0))
    honor = st.kind == "base"
    checked = bad = overlap = 0
    reasons: Dict[str, int] = defaultdict(int)
    first: List[str] = []
    prev_x = -1
    for (ei, xi, pp, why) in trades[:cap]:
        got = expected_exit(bars, ei, st.side, honor, sl_frac, tp_frac, cfg)
        checked += 1
        reasons[why] += 1
        entry = float(bars[ei][3])
        ok = got is not None and got[0] == xi and got[1] == why and abs((got[2] - entry) / entry * st.side - pp) < 1e-9
        if not ok:
            bad += 1
            if len(first) < 3:
                first.append(f"{st.id} entry bar {ei}: engine ({xi}, {why}, {pp:.6f}) plain {got}")
        if ei <= prev_x:
            overlap += 1
        prev_x = xi
    return {"orders": checked, "bad": bad, "overlap": overlap, "reasons": dict(reasons), "first": first}


def job(args):
    s, bars, t0, sample_every, cap = args
    _, out = sa._job((s, bars, t0))
    b = sg.make_book()
    cfg = sg.lane_cfg(b)
    res = {"orders": 0, "bad": 0, "overlap": 0, "sets": 0, "skipped_trail": 0, "skipped_combined": 0, "reasons": defaultdict(int), "first": []}
    for st in b.by_idx[::sample_every]:
        if st.kind == "trail":
            res["skipped_trail"] += 1
            continue
        if st.side == 0:
            res["skipped_combined"] += 1
            continue
        v = verify_set(st, out[st.idx], bars, cfg, b.cost_pct, b.sl_min_steps, cap)
        res["sets"] += 1
        for k in ("orders", "bad", "overlap"):
            res[k] += v[k]
        for k, n in v["reasons"].items():
            res["reasons"][k] += n
        for f in v["first"]:
            if len(res["first"]) < 3:
                res["first"].append(f)
    res["reasons"] = dict(res["reasons"])
    return s, out, res


# ---- 2/3. the ladder and the last-N configurations --------------------------------------------------------------------------------------------------

def _sel(ts_lane: Sequence[float], nets_lane: Sequence[float], entry: float, n: int, lb: float) -> Tuple[float, float]:
    idx = ma._closed(ts_lane, entry, lb, n)
    g = sum(nets_lane[j] for j in idx if nets_lane[j] > 0)
    l_ = -sum(nets_lane[j] for j in idx if nets_lane[j] < 0)
    return g, l_


def real_window_ok(real_t: Sequence[float], real_nets: Sequence[float], entry: float, P) -> bool:
    idx = ma._closed(real_t, entry, P.lookback_s, P.real_n)
    return len(idx) >= (P.real_n if P.full_window else 1) and round(ma._pf([real_nets[j] for j in idx]), 4) + 1e-9 >= P.floor_for(P.real_n)


def lane_totals(ts, ents, nets, member: Sequence[int], t0: float, sel_lane: Tuple[Sequence[float], Sequence[float]], sel_n: int, lb: float, keep=None):
    """Orders of the test window in `member` (optionally only those keep(k) accepts): gains, losses, orders, and the selecting window's gains / losses."""
    gp = gl = sg_ = sl_ = 0.0
    n = 0
    for k in member:
        if ents[k] < t0 or (keep is not None and not keep(k)):
            continue
        n += 1
        if nets[k] > 0:
            gp += nets[k]
        else:
            gl -= nets[k]
        g, l_ = _sel(sel_lane[0], sel_lane[1], ents[k], sel_n, lb)
        sg_ += g
        sl_ += l_
    return [gp, gl, n, sg_, sl_]


def ladder_of_set(idx: int) -> Dict[str, Any]:
    """All the gates of one Set, plus the final gate for every last-N configuration."""
    st, rows, t0, book = G["sets"][idx], G["rows"][idx], G["t0"], G["book"]
    cf = cost_as_frac(book.cost_pct)
    ts, ents = [r[0] for r in rows], [r[1] for r in rows]
    nets = [r[2] - cf for r in rows]
    P0 = book.stage_params(st.pack, st)
    lb = P0.lookback_s
    out: Dict[str, Any] = {"side": st.side, "gates": {}, "configs": {}}

    def chain(**kw):
        return ma.naive_chain(ts, ents, nets, dataclasses.replace(P0, **kw))

    base_off = dict(base_short=0, base_min_n=0, main_on=False, neg_on=False, real_on=False, short_on=False, main_min_n=0)
    # g0 the Base tape: every order of the Set, selected by nothing (the window that "selected" it is its own Base long window, for the in-sample column)
    out["gates"]["g0 Base tape (every order)"] = lane_totals(ts, ents, nets, range(len(rows)), t0, (ts, nets), P0.base_long, lb)
    c1 = chain(**base_off)
    out["gates"]["g1 + Base long window"] = lane_totals(ts, ents, nets, c1["main"], t0, (ts, nets), P0.base_long, lb)
    c2 = chain(**{**base_off, "base_short": P0.base_short, "base_min_n": P0.base_min_n})
    out["gates"]["g2 + Base short / minimal windows"] = lane_totals(ts, ents, nets, c2["main"], t0, (ts, nets), P0.base_long, lb)
    # Main windows act on the Main Set (the orders Base let through)
    c3 = chain(main_on=True, short_on=False, main_min_n=0, neg_on=False)
    mt3, mn3 = [ts[k] for k in c2["main"]], [nets[k] for k in c2["main"]]
    out["gates"]["g3 + Main long window"] = lane_totals(ts, ents, nets, c3["real"], t0, (mt3, mn3), P0.main_n, lb)
    c4 = chain(neg_on=P0.neg_on)
    out["gates"]["g4 + Main short / minimal windows (the Real Set)"] = lane_totals(ts, ents, nets, c4["real"], t0, (mt3, mn3), P0.main_n, lb)
    rt, rn = [ts[k] for k in c4["real"]], [nets[k] for k in c4["real"]]
    pos = {k: j for j, k in enumerate(c4["real"])}
    out["gates"]["g5 + Real window at the entry (executable)"] = lane_totals(ts, ents, nets, c4["real"], t0, (rt, rn), P0.real_n, lb,
                                                                           keep=lambda k: real_window_ok(rt, rn, ents[k], P0))
    if P0.neg_on:
        qt, qn = [ts[k] for k in c4["realneg"]], [nets[k] for k in c4["realneg"]]
        out["gates"]["n  Real Negative Set (opened in a Main dip)"] = lane_totals(ts, ents, nets, c4["realneg"], t0, (mt3, mn3), max(P0.neg_windows), lb)
        out["gates"]["n+ Real Negative window at the entry"] = lane_totals(ts, ents, nets, c4["realneg"], t0, (qt, qn), P0.real_n, lb,
                                                                            keep=lambda k: real_window_ok(qt, qn, ents[k], P0))
    # last-N configurations: the executable gate under each setting
    for name, kw in G["configs"].items():
        P = dataclasses.replace(P0, **kw)
        c = ma.naive_chain(ts, ents, nets, P)
        mt, mn = [ts[k] for k in c["main"]], [nets[k] for k in c["main"]]
        rt_, rn_ = [ts[k] for k in c["real"]], [nets[k] for k in c["real"]]
        out["configs"][name] = lane_totals(ts, ents, nets, c["real"], t0, (rt_, rn_), P.real_n, lb, keep=lambda k, rt_=rt_, rn_=rn_, P=P: real_window_ok(rt_, rn_, ents[k], P))
        out["configs"][name + "|main"] = lane_totals(ts, ents, nets, c["real"], t0, (mt, mn), P.main_n, lb)
    return out


def pf(g: float, l_: float):
    return round(g / l_, 3) if l_ > 0 else (None if g <= 0 else 99.0)


def last_n_configs(book) -> Dict[str, Dict[str, Any]]:
    """Every setting varied on its own around the shipped chain; each is a full chain of its own (nothing is shared between configurations)."""
    c: Dict[str, Dict[str, Any]] = {"shipped": {}}
    for L, S in ((30, 10), (30, 15), (40, 20), (50, 10), (50, 25), (20, 10)):
        c[f"Base long {L} / short {S}"] = dict(base_long=L, base_short=S)
    c["Base short off"] = dict(base_short=0)
    for n in (1, 2, 3):
        c[f"Base minimal +{n}"] = dict(base_min_n=n, base_min_positive=True, base_min_both=False)
        c[f"Base minimal -{n}"] = dict(base_min_n=n, base_min_positive=False, base_min_both=False)
    c["Base minimal both 2"] = dict(base_min_n=2, base_min_both=True)
    for n in (15, 25, 35):
        c[f"Main long {n}"] = dict(main_n=n)
    for n in (1, 2, 3):
        c[f"Main minimal +{n}"] = dict(main_min_n=n, main_min_positive=True, main_min_both=False)
        c[f"Main minimal -{n}"] = dict(main_min_n=n, main_min_positive=False, main_min_both=False)
    c["Main short off"] = dict(short_on=False)
    for n in (1, 5, 10, 15, 25):
        c[f"Real {n}"] = dict(real_n=n)
    c["full windows"] = dict(full_window=True)
    c["stages off (Base long + short only)"] = dict(main_on=False, real_on=False, neg_on=False)
    return c


BASE_OPTIONS = {"B50/25": dict(base_long=50, base_short=25, base_min_n=0), "B30/10": dict(base_long=30, base_short=10, base_min_n=0),
                "B20/10": dict(base_long=20, base_short=10, base_min_n=0),
                "B50/25 m-1": dict(base_long=50, base_short=25, base_min_n=1, base_min_positive=False, base_min_both=False),
                "B30/10 m-1": dict(base_long=30, base_short=10, base_min_n=1, base_min_positive=False, base_min_both=False)}
MAIN_OPTIONS = {"M25": dict(main_n=25, short_on=True, main_min_n=0), "M15": dict(main_n=15, short_on=True, main_min_n=0),
                "M35": dict(main_n=35, short_on=True, main_min_n=0), "M25 noshort": dict(main_n=25, short_on=False, main_min_n=0),
                "M25 k+1": dict(main_n=25, short_on=True, main_min_n=1, main_min_positive=True, main_min_both=False)}
REAL_OPTIONS = (1, 5, 15, 25)


def grid_of_set(idx: int) -> Dict[str, List[float]]:
    """Every Base x Main x Real last-N combination on its own: a Base option builds its Main Set, a Main option builds its Real Set from that Main Set, a Real
    N judges that Real Set at the entry. Positive lane (Real Set) and Negative lane (Real Negative Set) each. {lane|base|main|real: [gains, losses, orders, traded]}."""
    st, rows, t0, book = G["sets"][idx], G["rows"][idx], G["t0"], G["book"]
    cf = cost_as_frac(book.cost_pct)
    ts, ents = [r[0] for r in rows], [r[1] for r in rows]
    nets = [r[2] - cf for r in rows]
    P0 = book.stage_params(st.pack, st)
    out: Dict[str, List[float]] = {}
    for bn, bkw in BASE_OPTIONS.items():
        for mn_, mkw in MAIN_OPTIONS.items():
            P = dataclasses.replace(P0, **bkw, **mkw)
            ch = se.stage_chain(ts, ents, nets, P)
            for lane, members in (("pos", ch["real"]), ("neg", ch["realneg"])):
                lt, ln = [ts[k] for k in members], [nets[k] for k in members]
                for rn_ in REAL_OPTIONS:
                    Pr = dataclasses.replace(P, real_n=rn_)
                    gp = gl = 0.0
                    n = 0
                    for k in members:
                        if ents[k] < t0 or not real_window_ok(lt, ln, ents[k], Pr):
                            continue
                        n += 1
                        if nets[k] > 0:
                            gp += nets[k]
                        else:
                            gl -= nets[k]
                    out[f"{lane}|{bn}|{mn_}|R{rn_}"] = [gp, gl, n, 1 if n else 0]
    return out


def pick_sets(book, per_group: int):
    groups: Dict[Tuple, List[Any]] = defaultdict(list)
    for st in book.by_idx:
        rng = 0 if st.step <= 6 else 1 if st.step <= 12 else 2 if st.step <= 20 else 3
        groups[(st.pack, st.kind, st.side, rng)].append(st)
    chosen = []
    for key, sts in sorted(groups.items()):
        step = max(1, len(sts) // per_group)
        chosen.extend(sts[::step][:per_group])
    return chosen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--txt", default="")
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--lookback", type=int, default=1440)
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--sample-every", type=int, default=25)
    ap.add_argument("--cap", type=int, default=400, help="orders verified per (Set, symbol)")
    ap.add_argument("--per-group", type=int, default=6)
    ap.add_argument("--patch", default="")
    ap.add_argument("--grid", action="store_true", help="also run every Base x Main x Real last-N combination (positive and Negative lane) on its own")
    a = ap.parse_args()
    t_start = time.time()
    patch = {"holdEnabled": False, "histLookbackBars": a.lookback, "histLookbackAuto": False, **(json.loads(a.patch) if a.patch else {})}
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(patch)
    names, bars, times = sg.load_bars(a.data, a.symbols)
    book = sg.make_book()
    data_end = min(times[s][-1] for s in names)
    t0 = float(int(data_end // 3600) * 3600) - a.hours * 3600.0
    with ProcessPoolExecutor(max_workers=a.jobs) as pool:
        results = list(pool.map(job, [(s, bars[s], times[s][0], a.sample_every, a.cap) for s in names]))
    truth = {"orders": 0, "bad": 0, "overlap": 0, "sets": 0, "skipped_trail": 0, "skipped_combined": 0, "reasons": defaultdict(int), "first": []}
    rows: Dict[int, List[tuple]] = {st.idx: [] for st in book.by_idx}
    for s, out, res in results:
        for k in ("orders", "bad", "overlap", "sets", "skipped_trail", "skipped_combined"):
            truth[k] += res[k]
        for k, n in res["reasons"].items():
            truth["reasons"][k] += n
        truth["first"].extend(res["first"])
        ts_ = times[s]
        for st in book.by_idx:
            for (ei, xi, pp, why) in out[st.idx]:
                rows[st.idx].append((ts_[xi] + 60.0, ts_[ei] + 60.0, pp, why))
    for r in rows.values():
        r.sort()
    truth["reasons"] = dict(truth["reasons"])
    print(f"market truth: {truth['orders']} orders of {truth['sets']} Sets replayed from the bars, {truth['bad']} differ, {truth['overlap']} overlap  [{time.time() - t_start:.0f}s]", flush=True)
    chosen = pick_sets(book, a.per_group)
    G.update(sets={st.idx: st for st in book.by_idx}, rows=rows, t0=t0, book=book, configs=last_n_configs(book))
    with ProcessPoolExecutor(max_workers=a.jobs) as pool:
        per_set = list(pool.map(ladder_of_set, [st.idx for st in chosen], chunksize=4))
    grid: Dict[str, List[float]] = {}
    if a.grid:
        with ProcessPoolExecutor(max_workers=a.jobs) as pool:
            for r in pool.map(grid_of_set, [st.idx for st in chosen], chunksize=4):
                for k, v in r.items():
                    acc = grid.setdefault(k, [0.0, 0.0, 0, 0])
                    for i in range(4):
                        acc[i] += v[i]
        print(f"grid: {len(grid)} combinations  [{time.time() - t_start:.0f}s]", flush=True)
    gates: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(lambda: [0.0] * 5 + [0]))
    configs: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(lambda: [0.0] * 5 + [0]))
    for st, r in zip(chosen, per_set):
        side = "long" if st.side > 0 else "short" if st.side < 0 else "both"
        for dst, src in ((gates, r["gates"]), (configs, r["configs"])):
            for name, v in src.items():
                for key in ("all", side):
                    acc = dst[name][key]
                    for i in range(5):
                        acc[i] += v[i]
                    acc[5] += 1 if v[2] > 0 else 0
    res = {"meta": {"symbols": names, "lookback_bars": a.lookback, "test_hours": a.hours, "test_start": int(t0), "sets_sampled": len(chosen), "sets_total": len(book.by_idx),
                    "cost_pct": book.cost_pct, "config": {"setGateWindow": book.gate_window, "setGateShortN": book.base_short, "mainGateN": book.main_n, "realGateN": book.realst_n,
                                                          "mainShortWindows": list(book.short_windows), "minPfByStep": book.pf_by_step}},
           "market_truth": truth, "gates": {k: dict(v) for k, v in gates.items()}, "configs": {k: dict(v) for k, v in configs.items()}, "grid": grid}
    json.dump(res, open(a.out, "w"))
    txt = render(res)
    if a.txt:
        open(a.txt, "w", encoding="utf-8").write(txt)
    print(txt)
    print(f"done in {time.time() - t_start:.0f}s")
    return 0 if truth["bad"] == 0 and truth["overlap"] == 0 else 2


def render(r: Dict[str, Any]) -> str:
    m, t = r["meta"], r["market_truth"]
    L = [f"independent progress audit: {len(m['symbols'])} symbols, test window {time.strftime('%m-%d %H:%M', time.gmtime(m['test_start']))} UTC + {m['test_hours']} h after "
         f"{m['lookback_bars']} bars of pre-history; {m['sets_sampled']} of {m['sets_total']} Sets sampled by (pack, kind, direction, step range); cost {m['cost_pct']} %; "
         f"config {m['config']}", ""]
    L.append("1  MARKET TRUTH (orders replayed from the raw 1m bars, no engine lane code)")
    L.append(f"   {t['orders']} orders of {t['sets']} Sets (base and Exit Sets, both directions; {t['skipped_trail']} trailing and {t['skipped_combined']} combined Sets skipped): "
             f"{t['bad']} differ in exit bar, reason or gross result; {t['overlap']} start before the Set's previous order closed; exits {t['reasons']}")
    for f in t["first"][:3]:
        L.append("   first difference: " + f)
    L.append("")
    L.append("2  THE GATE LADDER (plain chain; test-window orders; PF = gains / losses of the net results after one PositionCost; 'selected at' = PF of the window that admitted the orders)")
    for side in ("all", "long", "short"):
        L.append(f"   {side}:")
        L.append(f"     {'gate':<58s}{'orders':>9s}{'PF delivered':>14s}{'selected at':>13s}{'Sets traded':>13s}")
        for name in sorted(r["gates"]):
            v = r["gates"][name].get(side)
            if not v:
                continue
            L.append(f"     {name:<58s}{int(v[2]):>9d}{str(pf(v[0], v[1])):>14s}{str(pf(v[3], v[4])):>13s}{int(v[5]):>13d}")
    L.append("")
    L.append("3  LAST-N CONFIGURATIONS (the executable gate under each setting, each a full chain of its own; the shipped line first)")
    L.append(f"     {'configuration':<40s}{'orders':>9s}{'PF delivered':>14s}{'Main sel.':>11s}{'Sets traded':>13s}")
    base = r["configs"].get("shipped", {}).get("all")
    for name in r["configs"]:
        if name.endswith("|main"):
            continue
        v = r["configs"][name].get("all")
        w = r["configs"].get(name + "|main", {}).get("all")
        if v:
            L.append(f"     {name:<40s}{int(v[2]):>9d}{str(pf(v[0], v[1])):>14s}{str(pf(w[3], w[4]) if w else None):>11s}{int(v[5]):>13d}")
    if base:
        L.append(f"   (shipped: {int(base[2])} orders, PF {pf(base[0], base[1])}; a difference under about 0.1 on a few hundred orders is noise)")
    return "\n".join(L)


if __name__ == "__main__":
    sys.exit(main())
