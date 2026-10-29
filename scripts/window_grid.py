#!/usr/bin/env python3
"""Window grid on a fresh test: the Base long window x the Base short window x the ongoing (Real) last N, walked forward on stored 1m history.

Every cell is one engine configuration (an overlay patch loaded into a SetBook, so the engine's own clamps apply) evaluated hour by hour
like the live engine: shadow tapes per Set (stage_eval), the gate and the stage chain per hour (Base long + short + minimal window, Main long + short + minimal,
Negative lane, Real), the best valid Set per signal, positions closed by their Set's own lane. Per cell it reports the validated share and the
effective PF after all gates (in-sample: the Sets were selected on those tapes), then the executed PF with its order count (the number that
counts), net, drawdown, hours positive, both 12 h halves and the hourly orders and PF.

Grids: the "real" third (Real last N 15 / 25, Main long as shipped) and the "main" third (Main long 15 / 25, Real as shipped), because
"ongoing 15, 25" can be read either way. Cells with a short window at or over the long window are the long window itself and are skipped.
References: Base long window alone (stages off). The test window is the last --hours hour starts of the data; the pre-history of every hour is
--lookback bars (1440 = 24 h) before it. No development / sealed split: the whole window is the independent test, and the best cell chosen on
it is in-sample for that choice.

usage: window_grid.py --data DIR --cache FILE --out FILE [--long 20,30,40,50] [--short 5,10,15,20,25] [--mains 15,25] [--reals 15,25]
                      [--hours 24] [--lookback 1440] [--jobs 4]"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import pickle
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))
import hold_auto_eval as ha  # noqa: E402
import stage_eval as sg  # noqa: E402

ORDER_FLOOR = 150            # a cell with fewer executed orders is not ranked "best" (a PF on a handful of orders is noise)
G: Dict[str, Any] = {}       # the shared state the forked workers read


EXTRA: Dict[str, Any] = {}   # --patch: extra overlay keys applied to every cell (e.g. {"stageFullWindow": true})


def base_patch(lookback: int) -> Dict[str, Any]:
    return {"holdEnabled": False, "histLookbackBars": lookback, "histLookbackAuto": False, **EXTRA}


def cell_patch(cell: Dict[str, Any], lookback: int) -> Dict[str, Any]:
    p = base_patch(lookback)
    p["setGateWindow"] = cell["long"]
    if cell["stages"]:
        p.update({"stagesEnabled": True, "setGateShortN": cell["short"], "mainGateN": cell["main"], "realGateN": cell["real"]})
        if cell.get("neg"):
            p["mainNegWindows"] = cell["neg"]
        if cell.get("min_n"):
            p["setGateMinN"] = cell["min_n"]
            p["setGateMinMode"] = cell["min_mode"]
        if cell.get("main_min_n"):
            p["mainMinN"] = cell["main_min_n"]
            p["mainMinMode"] = cell["main_min_mode"]
    else:
        p["stagesEnabled"] = False
    return p


def book_for(cell: Dict[str, Any]):
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(cell_patch(cell, G["lookback"]))
    return sg.make_book()


def hourly(rows: List[Dict[str, Any]], hours: List[float]) -> List[Dict[str, Any]]:
    out = {int(h): {"orders": 0, "gp": 0.0, "gl": 0.0, "valid": None} for h in hours}
    for r in rows:
        if "hour" in r and int(r["hour"]) in out:
            out[int(r["hour"])]["valid"] = round(100.0 * r["valid"] / max(1, r["total"]), 2)
        elif "net" in r:
            k = int(r["t_in"] // 3600) * 3600
            if k in out:
                o = out[k]
                o["orders"] += 1
                o["gp" if r["net"] > 0 else "gl"] += abs(r["net"])
    return [{"hour": h, "orders": v["orders"], "pf": round(v["gp"] / v["gl"], 3) if v["gl"] > 0 else None,
             "net_pct": round(100 * (v["gp"] - v["gl"]), 3), "valid_pct": v["valid"]} for h, v in sorted(out.items())]


def se_ratio() -> float:
    import set_engine as se
    return float(se.BASE_SHORT_MAX_RATIO)


def lane_figures(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    gp = sum(r["net"] for r in trades if r["net"] > 0)
    gl = -sum(r["net"] for r in trades if r["net"] < 0)
    return {"orders": len(trades), "pf": round(gp / gl, 3) if gl > 0 else None, "net_pct": round(100 * (gp - gl), 2)}


WINDOW_KEYS = ("baseLong", "baseShort", "baseMinimal", "mainLong", "mainShort", "mainMinimal", "negative", "real", "realNeg")


def hour_figures(book, tapes, hour: float) -> Dict[str, Any]:
    """One test hour of the whole book, as the engine's stats count it: every Set's last-N windows each on its own (stage_eval.window_figures, checked against
    the engine's SetState fields), the Sets valid after every gate, and the effective PF's gains / losses / orders (the long-window orders of those Sets)."""
    from set_engine import RANK_PF_CAP, pf_of
    acc = {k: [0, 0, 0.0, 0.0, 0, 0.0] for k in WINDOW_KEYS}          # judged, passing, gains, losses, orders, sum of the passing Sets' own capped PFs
    valid = total = 0
    gp_v = gl_v = 0.0
    n_v = 0
    for st in book.by_idx:
        total += 1
        lane = sg.gate_metrics(book, tapes, st, hour)[0]
        fig = sg.window_figures(book, tapes, st, hour)
        for k, f in fig.items():
            if f is None:
                continue
            a = acc[k]
            a[0] += 1
            if f[0]:
                a[1] += 1
                a[2] += f[1]
                a[3] += f[2]
                a[4] += f[3]
                a[5] += min(RANK_PF_CAP, pf_of(f[1], f[2]))
        if lane:
            valid += 1
            f = fig["baseLong"]
            gp_v, gl_v, n_v = gp_v + f[1], gl_v + f[2], n_v + f[3]
    return {"total": total, "valid": valid, "windows": acc, "gp_post": gp_v, "gl_post": gl_v, "orders": n_v}


def window_summary(per_hour: List[Dict[str, Any]], book, cell: Dict[str, Any]) -> Dict[str, Any]:
    """The cell's windows pooled over every test hour: Sets judged / passing per hour on average, the share of the judged ones (pooled over the hours), the PF of the
    gains over the losses of ALL the orders the window read in all hours (the complete figure, not a median or a mean of hourly PFs), those orders, and the mean of
    the passing Sets' own PFs."""
    on = {"baseLong": True, "baseShort": bool(book.base_short), "baseMinimal": bool(book.base_min_n), "mainLong": book.main_on, "mainShort": book.main_on and book.short_on,
          "mainMinimal": book.main_on and bool(book.main_min_n), "negative": book.neg_on, "real": book.realst_on, "realNeg": book.realst_on and book.neg_on}
    nominal = {"baseLong": book.gate_window, "baseShort": book.base_short, "baseMinimal": book.base_min_n, "mainLong": book.main_n, "mainShort": list(book.short_windows),
               "mainMinimal": book.main_min_n, "negative": list(book.neg_windows), "real": book.realst_n, "realNeg": book.realst_n}
    out: Dict[str, Any] = {}
    hrs = max(1, len(per_hour))
    for k in WINDOW_KEYS:
        judged = sum(h["windows"][k][0] for h in per_hour)
        passing = sum(h["windows"][k][1] for h in per_hour)
        gp = sum(h["windows"][k][2] for h in per_hour)
        gl = sum(h["windows"][k][3] for h in per_hour)
        out[k] = {"on": bool(on[k]), "n": nominal[k], "judged_per_hour": round(judged / hrs, 1), "sets_per_hour": round(passing / hrs, 1),
                  "pct": round(100.0 * passing / judged, 2) if judged else 0.0, "pf": round(gp / gl, 4) if gl > 0 else (99.0 if gp > 0 else None),
                  "gp": round(gp, 6), "gl": round(gl, 6), "orders": sum(h["windows"][k][4] for h in per_hour),
                  "mean_set_pf": round(sum(h["windows"][k][5] for h in per_hour) / passing, 4) if passing else None}
    return out


def gp_gl(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    gp = sum(r["net"] for r in trades if r["net"] > 0)
    gl = -sum(r["net"] for r in trades if r["net"] < 0)
    return {"orders": len(trades), "gp_pct": round(100 * gp, 6), "gl_pct": round(100 * gl, 6), "pf": round(gp / gl, 3) if gl > 0 else None, "net_pct": round(100 * (gp - gl), 2)}


def run_cell(cell: Dict[str, Any]) -> Dict[str, Any]:
    t0 = time.time()
    book = book_for(cell)
    names, bars, times, shadow, tapes, hours = (G[k] for k in ("names", "bars", "times", "shadow", "tapes", "hours"))
    per_hour = [hour_figures(book, tapes, h) for h in hours]
    tot = per_hour[0]["total"]
    gp_e, gl_e = sum(p["gp_post"] for p in per_hour), sum(p["gl_post"] for p in per_hour)
    eff_hourly = [p["gp_post"] / p["gl_post"] for p in per_hour if p["gl_post"] > 0]
    rows = sg.execute(book, names, bars, times, shadow, tapes, hours)
    lo, mid, hi = hours[0], hours[len(hours) // 2], hours[-1] + 3600
    whole, h1, h2 = (sg.summarize(rows, a, b, False) for a, b in ((lo, hi), (lo, mid), (mid, hi)))
    trades = [r for r in rows if "net" in r]
    res = dict(cell)
    res.update({
        "short_eff": book.base_short if cell["stages"] else 0,
        "main_eff": book.main_n if cell["stages"] else None,
        "real_eff": book.realst_n if cell["stages"] else None,
        "valid_pct": round(100.0 * sum(p["valid"] for p in per_hour) / max(1, tot * len(per_hour)), 2),
        # the effective PF is the gains over the losses of ALL the long-window orders of the valid Sets over ALL the test hours (the complete figure);
        # the median of the hourly PFs is kept beside it as the read of its stability
        "eff_pf": round(gp_e / gl_e, 3) if gl_e > 0 else None, "eff_orders": sum(p["orders"] for p in per_hour), "eff_gp": round(gp_e, 6), "eff_gl": round(gl_e, 6),
        "eff_pf_median": round(statistics.median(eff_hourly), 3) if eff_hourly else None,
        "eff_orders_per_hour": round(sum(p["orders"] for p in per_hour) / len(per_hour)),
        "windows": window_summary(per_hour, book, cell) if cell["stages"] else None,
        "exec_pf": whole["pf"], "exec_orders": whole["orders"], "net_pct": whole["net_pct"], "net_per_order_pct": whole["net_per_order_pct"],
        "exec_gp_pct": whole["gp_pct"], "exec_gl_pct": whole["gl_pct"],
        "max_dd_pct": whole["max_dd_pct"],
        "hours_pos": whole["hours_positive"],
        "wins": sum(1 for r in trades if r["net"] > 0), "neg_lane_orders": sum(1 for r in trades if r.get("lane") == "neg"),
        "pos_lane": gp_gl([r for r in trades if r.get("lane") != "neg"]), "neg_lane": gp_gl([r for r in trades if r.get("lane") == "neg"]),
        "neg_windows": list(book.neg_windows) if cell["stages"] else None,
        "min_n_eff": book.base_min_n if cell["stages"] else 0,
        "main_min_eff": book.main_min_n if cell["stages"] else 0,
        "half1": {"pf": h1["pf"], "orders": h1["orders"], "net_pct": h1["net_pct"], "gp_pct": h1["gp_pct"], "gl_pct": h1["gl_pct"]},
        "half2": {"pf": h2["pf"], "orders": h2["orders"], "net_pct": h2["net_pct"], "gp_pct": h2["gp_pct"], "gl_pct": h2["gl_pct"]},
        "hourly": hourly(rows, hours), "secs": round(time.time() - t0, 1),
    })
    # one basis: the orders, the net and the PF of a cell are the same orders, whichever way they are cut
    hr = res["hourly"]
    pos_o, neg_o = res["pos_lane"]["orders"], res["neg_lane"]["orders"]
    checks = {
        "orders = sum of hourly orders": res["exec_orders"] == sum(h["orders"] for h in hr),
        "orders = halves": res["exec_orders"] == res["half1"]["orders"] + res["half2"]["orders"],
        "orders = lanes": res["exec_orders"] == pos_o + neg_o,
        "net = sum of hourly net": abs(res["net_pct"] - sum(h["net_pct"] for h in hr)) < 0.05 + 0.001 * len(hr),
        "net = lanes": abs(res["net_pct"] - res["pos_lane"]["net_pct"] - res["neg_lane"]["net_pct"]) < 0.05,
        "PF above 1 = net above 0": res["exec_pf"] is None or abs(res["exec_pf"] - 1.0) < 0.0015 or ((res["exec_pf"] > 1.0) == (res["net_pct"] > 0)),   # a PF rounded to 1.000 is break-even
        "hours positive <= test hours": int(res["hours_pos"].split("/")[1]) <= len(hours),
        "PF = gains / losses": res["exec_pf"] is None or (res["exec_gl_pct"] > 0 and abs(res["exec_pf"] - res["exec_gp_pct"] / res["exec_gl_pct"]) < 0.0015),
        "gains and losses add up over the halves and the lanes": (abs(res["exec_gp_pct"] - res["half1"]["gp_pct"] - res["half2"]["gp_pct"]) < 1e-4 + 1e-6 * res["exec_gp_pct"]
                                                                  and abs(res["exec_gl_pct"] - res["pos_lane"]["gl_pct"] - res["neg_lane"]["gl_pct"]) < 1e-4 + 1e-6 * res["exec_gl_pct"]),
        "effective orders = long-window orders of the valid Sets": res["eff_orders"] == sum(p["orders"] for p in per_hour),
    }
    res["consistent"] = all(checks.values())
    res["inconsistent"] = [k for k, v in checks.items() if not v]
    return res


def parse_minimal(raw: str) -> List[Tuple[int, str]]:
    """'0;+1;+2;-1' -> [(0, ''), (1, 'positive'), (2, 'positive'), (1, 'negative')]: the Base minimal window (newest N results all positive / negative)."""
    out: List[Tuple[int, str]] = []
    for tok in [t.strip() for t in raw.replace(",", ";").split(";") if t.strip()]:
        if tok in ("0", "off"):
            out.append((0, ""))
        else:
            n = int(tok.lstrip("+-"))
            out.append((n, "negative" if tok.startswith("-") else "positive"))
    return out or [(0, "")]


def enumerate_cells(longs, shorts, mains, reals, main_default, real_default, negs=("",), ratio: float = 1.0, minimals=((0, ""),), main_minimals=((0, ""),)) -> List[Dict[str, Any]]:
    seen: Dict[Tuple, Dict[str, Any]] = {}

    def add(L, S, M, R, neg, mn=0, mm="", kn=0, km="", stages=True):
        key = (L, S, M, R, neg, mn, mm, kn, km, stages)
        if key not in seen:
            tag = (f"_m{'+' if mm == 'positive' else '-'}{mn}" if mn else "") + (f"_k{'+' if km == 'positive' else '-'}{kn}" if kn else "")
            seen[key] = {"long": L, "short": S, "main": M, "real": R, "neg": neg, "min_n": mn, "min_mode": mm, "main_min_n": kn, "main_min_mode": km, "stages": stages,
                         "id": (f"L{L}_S{S}_M{M}_R{R}" + (f"_N{neg.replace(',', '-')}" if neg else "") + tag) if stages else f"L{L}_base_alone"}
        return seen[key]

    for L in longs:
        for S in shorts:
            if S >= L or S > int(L * ratio + 1e-9):
                continue                                   # the short window at or over the long window is the long window itself; over the allowed share it is clamped
            for neg in negs:
                for mn, mm in minimals:
                    if mn and mn > S:
                        continue                           # the minimal window is at most the short window
                    for kn, km in main_minimals:
                        for R in reals:
                            add(L, S, main_default, R, neg, mn, mm, kn, km)["real_third"] = True
                        for M in mains:
                            add(L, S, M, real_default, neg, mn, mm, kn, km)["main_third"] = True
    for L in longs:
        add(L, None, None, None, "", stages=False)
    return list(seen.values())


def best_of(cells: List[Dict[str, Any]], third: str) -> List[Dict[str, Any]]:
    pool = [c for c in cells if c["stages"] and c.get(third) and c["exec_pf"] is not None and c["exec_orders"] >= ORDER_FLOOR]
    return sorted(pool, key=lambda c: (c["exec_pf"], c["exec_orders"]), reverse=True)


def print_matrix(cells, third, vary, fixed_label, longs, shorts, values, title=""):
    for v in values:
        sel = {(c["long"], c["short"]): c for c in cells if c["stages"] and c.get(third) and c[vary] == v}
        print(f"\n{title}{third.split('_')[0]} third: {vary} {v} ({fixed_label}) - executed PF (orders) / effective PF / valid %")
        print("  long\\short " + "".join(f"{s:>22d}" for s in shorts))
        for L in longs:
            line = f"  {L:>9d} "
            for s in shorts:
                c = sel.get((L, s))
                line += f"{'n/a':>22s}" if c is None else f"{str(c['exec_pf']) + ' (' + str(c['exec_orders']) + ') ' + str(c['eff_pf']) + ' ' + str(c['valid_pct']) + '%':>22s}"
            print(line)


MEMO: Dict[str, Dict] = {"base": {}, "chain": {}, "eval": {}}
_GATE_BASE, _CHAIN_AT, _BASE_EVAL = sg.gate_base, sg.chain_at, sg.base_eval


def _gate_base_memo(book, tapes, st, now):
    k = (st.idx, now)
    v = MEMO["base"].get(k)
    if v is None:
        v = MEMO["base"][k] = _GATE_BASE(book, tapes, st, now)
    return v


def _base_eval_memo(book, tapes, st, now):
    k = (st.idx, now)
    v = MEMO["eval"].get(k)
    if v is None:
        v = MEMO["eval"][k] = _BASE_EVAL(book, tapes, st, now)
    return v


def _chain_at_memo(book, tapes, st, now):
    k = (st.idx, now)
    v = MEMO["chain"].get(k)
    if v is None:
        v = MEMO["chain"][k] = _CHAIN_AT(book, tapes, st, now)
    return v


def run_group(cells: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Cells that differ only in the Real last N: the Base verdicts and the chains of every (Set, hour) are computed once and shared (the chain never reads
    Real N), each cell then adds its own verdicts and its own execution. A group is one independent task: no state is shared between groups."""
    for m_ in MEMO.values():
        m_.clear()
    sg.gate_base, sg.chain_at, sg.base_eval = _gate_base_memo, _chain_at_memo, _base_eval_memo
    try:
        return [run_cell(c) for c in cells]
    finally:
        sg.gate_base, sg.chain_at, sg.base_eval = _GATE_BASE, _CHAIN_AT, _BASE_EVAL
        for m_ in MEMO.values():
            m_.clear()


def group_cells(cells: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    groups: Dict[Tuple, List[Dict[str, Any]]] = {}
    for c in cells:
        key = (c["long"], c["short"], c["main"], c.get("neg"), c.get("min_n"), c.get("min_mode"), c.get("main_min_n"), c.get("main_min_mode"), True) if c["stages"] else ("alone", c["long"])
        groups.setdefault(key, []).append(c)
    return list(groups.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--long", default="20,30,40,50")
    ap.add_argument("--short", default="5,10,15,20,25")
    ap.add_argument("--mains", default="15,25", help="Main long values (the main third); empty = skip that third")
    ap.add_argument("--reals", default="15,25")
    ap.add_argument("--minimal", default="0", help="Base minimal window variants separated by ';': 0 = off, +N = the newest N results all positive, -N = all negative (N 1-3)")
    ap.add_argument("--main-minimal", default="0", help="Main minimal window variants separated by ';' like --minimal: 0 = off, +N = the newest N Main results all positive, -N = all negative (N 1-3)")
    ap.add_argument("--neg", default="", help="Negative-window sets separated by ';', e.g. '5,10;1,2' (empty = the shipped 5,10)")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--lookback", type=int, default=1440)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--no-check", action="store_true")
    ap.add_argument("--patch", default="", help='extra overlay keys for every cell, as JSON, e.g. {"stageFullWindow": true}')
    a = ap.parse_args()
    if a.patch:
        EXTRA.update(json.loads(a.patch))
    t0 = time.time()
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(base_patch(a.lookback))
    names, bars, times = sg.load_bars(a.data, a.symbols)
    if os.path.exists(a.cache):
        shadow = pickle.load(open(a.cache, "rb"))
    else:
        shadow = sg.build(names, bars, times, a.jobs)
        pickle.dump(shadow, open(a.cache, "wb"))
    book0 = sg.make_book()
    tapes = sg.Tapes(book0, names, times, shadow)
    start = max(times[s][0] for s in names) + book0.lookback * 60
    end = min(times[s][-1] for s in names)
    all_hours = [float(h) for h in range(int(start // 3600 + 1) * 3600, int(end), 3600)]
    hours = all_hours[-a.hours:]
    if len(hours) < a.hours:
        print(f"only {len(hours)} test hours fit after a {a.lookback}-bar pre-history", file=sys.stderr)
    ov = json.load(open(sg.OVERLAY))
    main_default, real_default = int(ov.get("mainGateN", 25)), int(ov.get("realGateN", 15))
    longs, shorts = [int(x) for x in a.long.split(",")], [int(x) for x in a.short.split(",")]
    mains, reals = [int(x) for x in a.mains.split(",") if x.strip()], [int(x) for x in a.reals.split(",")]
    negs = [x.strip() for x in a.neg.split(";") if x.strip()] or [""]
    ratio = float(ov.get("setGateShortMaxRatio", se_ratio()))
    minimals = parse_minimal(a.minimal)
    main_minimals = parse_minimal(a.main_minimal)
    cells = enumerate_cells(longs, shorts, mains, reals, main_default, real_default, negs, ratio, minimals, main_minimals)
    G.update(names=names, bars=bars, times=times, shadow=shadow, tapes=tapes, hours=hours, lookback=a.lookback)
    groups = group_cells(cells)
    print(f"symbols={len(names)} bars/symbol={len(bars[names[0]])} test hours={len(hours)} "
          f"({time.strftime('%m-%d %H:%M', time.gmtime(hours[0]))} - {time.strftime('%m-%d %H:%M', time.gmtime(hours[-1] + 3600))} UTC) "
          f"pre-history {a.lookback} bars  cells={len(cells)} in {len(groups)} independent groups  prepared in {time.time() - t0:.0f}s", flush=True)
    checks = []
    if not a.no_check:
        pick = [c for c in cells if c["stages"]]
        corners = [pick[0], pick[len(pick) // 2], pick[-1]] + [c for c in pick if c.get("min_n")][:2]
        for c in corners:
            bk = book_for(c)
            n_chk, bad = sg.self_check(bk, tapes, hours, samples=30)
            checks.append({"cell": c["id"], "samples": n_chk, "bad": bad})
            print(f"self-check {c['id']}: {n_chk - bad}/{n_chk} samples equal the engine's gate", flush=True)
            if bad:
                print("the direct gate differs from SetBook._score_one / gate_reason: the grid would not be the engine's", file=sys.stderr)
                return 1
    results: List[Dict[str, Any]] = []
    ctx = multiprocessing.get_context("fork")
    done = 0
    with ProcessPoolExecutor(max_workers=a.jobs, mp_context=ctx) as pool:
        futs = {pool.submit(run_group, g): g for g in sorted(groups, key=len, reverse=True)}
        for f in as_completed(futs):
            for r in f.result():
                done += 1
                results.append(r)
                print(f"[{done}/{len(cells)}] {r['id']:<34s} exec PF {r['exec_pf']!s:>6s} ({r['exec_orders']:>4d} orders)  eff PF {r['eff_pf']!s:>6s}  "
                      f"valid {r['valid_pct']:>5}%  halves {r['half1']['pf']}/{r['half2']['pf']}  {r['secs']}s", flush=True)
    results.sort(key=lambda c: (not c["stages"], c.get("neg") or "", c.get("main_min_n") or 0, c.get("main_min_mode") or "", c.get("min_n") or 0, c.get("min_mode") or "", c["long"], c["short"] or 0, c["main"] or 0, c["real"] or 0))
    bad_cells = [(c["id"], c["inconsistent"]) for c in results if not c.get("consistent", True)]
    print(f"consistency: {len(results) - len(bad_cells)}/{len(results)} cells have the same orders, net and PF however they are cut" + (f"; broken: {bad_cells[:5]}" if bad_cells else ""), flush=True)
    neg_default = ",".join(str(w) for w in book0.neg_windows)
    meta = {"symbols": names, "hours": [int(h) for h in hours], "lookback_bars": a.lookback, "prehistory_h": a.lookback / 60.0,
            "bars_per_symbol": len(bars[names[0]]), "created": int(time.time()), "order_floor": ORDER_FLOOR,
            "shipped": {"long": int(ov.get("setGateWindow", 50)), "short": int(ov.get("setGateShortN", 25)), "main": main_default, "real": real_default},
            "longs": longs, "shorts": shorts, "mains": mains, "reals": reals, "main_default": main_default, "real_default": real_default,
            "short_max_ratio": ratio, "negs": [x or neg_default for x in negs], "neg_default": neg_default,
            "minimals": [{"n": n, "mode": m, "label": ("off" if not n else f"{'+' if m == 'positive' else '-'}{n}")} for n, m in minimals],
            "main_minimals": [{"n": n, "mode": m, "label": ("off" if not n else f"{'+' if m == 'positive' else '-'}{n}")} for n, m in main_minimals],
            "min_orders": int(ov.get("setGateMinTrades", 0)), "max_ddt_h": float(ov.get("setMaxDdtHours", 24)), "min_pf": float(ov.get("setMinPf", 1.25)),
            "cost_pct": book0.cost_pct, "sets": len(book0.by_idx), "self_check": checks, "hold": "off (as every window evaluation)", "groups": len(groups),
            "inconsistent_cells": len(bad_cells)}
    json.dump({"meta": meta, "cells": results}, open(a.out, "w"), indent=1)
    minimal_labels = [m["label"] for m in meta["minimals"]]
    main_labels = [m["label"] for m in meta["main_minimals"]]
    for neg in meta["negs"]:
        for ml, (mn, mm) in zip(minimal_labels, minimals):
            for kl, (kn, km) in zip(main_labels, main_minimals):
                sub = [c for c in results if (not c["stages"]) or ((c.get("neg") or neg_default) == neg and (c.get("min_n") or 0) == mn and (c.get("min_mode") or "") == mm
                                                                   and (c.get("main_min_n") or 0) == kn and (c.get("main_min_mode") or "") == km)]
                title = f"[Negative windows {neg} | Base minimal {ml} | Main minimal {kl}] "
                print_matrix(sub, "real_third", "real", f"Main long {main_default}", longs, shorts, reals, title)
                if mains:
                    print_matrix(sub, "main_third", "main", f"Real {real_default}", longs, shorts, mains, title)
                for third in ("real_third", "main_third"):
                    ranked = best_of(sub, third)
                    if not ranked:
                        continue
                    print(f"\n{title}best 5 by executed PF, {third}, at least {ORDER_FLOOR} executed orders:")
                    for c in ranked[:5]:
                        print(f"  long {c['long']:>2d} short {c['short']:>2d} main {c['main_eff']:>2d} real {c['real_eff']:>2d}: exec PF {c['exec_pf']} ({c['exec_orders']} orders; "
                              f"positive lane {c['pos_lane']['pf']} on {c['pos_lane']['orders']}, negative lane {c['neg_lane']['pf']} on {c['neg_lane']['orders']}) "
                              f"eff PF {c['eff_pf']} on {c['eff_orders']} orders, valid {c['valid_pct']}% halves {c['half1']['pf']}/{c['half2']['pf']}")
    print("\nbase long window alone (stages off):")
    for c in results:
        if not c["stages"]:
            print(f"  long {c['long']}: exec PF {c['exec_pf']} ({c['exec_orders']} orders) eff PF {c['eff_pf']} valid {c['valid_pct']}% halves {c['half1']['pf']}/{c['half2']['pf']}")
    print(f"\ndone in {time.time() - t0:.0f}s -> {a.out}")
    return 0 if not bad_cells else 2


if __name__ == "__main__":
    sys.exit(main())
