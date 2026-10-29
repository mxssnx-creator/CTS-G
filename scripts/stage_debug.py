#!/usr/bin/env python3
"""Process debug of one Set through the stage chain: every order of its tape with the verdict of each window of each stage at the moment the order was
opened, the Sets that chain builds (Main, Real, Real Negative) and the verdicts now. Nothing here decides anything: it reads the engine's own rules
(set_engine.stage_chain / stage_verdicts) and shows, order by order, why an order is in the Main Set or not, which Main window stopped it, and what the
numbers behind each window were, so a figure on the desk can be followed back to the orders that made it.

  trace_set(ts, ents, nets, P)  -> {"orders": [per order flags], "main", "real", "realneg", "short_n", "verdict": stage_verdicts now}
  render(trace, ...)            -> text

usage: stage_debug.py --data DIR --set ID [--hour N] [--symbols 20] [--lookback 1440] [--orders 40] [--patch JSON]
  --set    a Set id (e.g. indications:1m:sl1.00:st10) or its index
  --hour   the verdict hour as an index into the last 24 test hours (default: the last one); the trace shows the orders closed before it
Every number is net of one PositionCost (0.12 % by default)."""
from __future__ import annotations

import argparse
import bisect
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))
import set_engine as se  # noqa: E402


def _window(lt: Sequence[float], nets: Sequence[float], entry: float, n: int, lookback_s: float, full: bool) -> Dict[str, Any]:
    """The newest n results of a lane that closed before `entry` (within the lookback): count, PF, gains, losses, whether it is judged."""
    e = bisect.bisect_left(lt, entry)
    s0 = max(bisect.bisect_left(lt, entry - lookback_s), e - n)
    w = list(nets[s0:e])
    gp, gl = sum(x for x in w if x > 0), -sum(x for x in w if x < 0)
    return {"n": len(w), "pf": round(se.pf_of(gp, gl), 4), "gp": gp, "gl": gl, "judged": len(w) >= (n if full else 1), "results": w}


def trace_set(ts: Sequence[float], ents: Sequence[float], nets: Sequence[float], P: "se.StageParams") -> Dict[str, Any]:
    """Order by order: the Base windows at the entry (long with orders / PF / drawdown time, short, minimal), whether the order joined the Main Set; for a
    Main-Set order the Main windows at its entry (long, the chosen short, minimal), whether it joined the Real Set, whether it is a dip order (Negative
    lane). Computed from the same positions in the tape as set_engine.stage_chain and cross-checked against it (the lists must be identical)."""
    ch = se.stage_chain(ts, ents, nets, P)
    gpre, lpre = se.prefix_sums(nets)
    lb, full = P.lookback_s, P.full_window
    main_pos = {k: j for j, k in enumerate(ch["main"])}
    mt = [ts[k] for k in ch["main"]]
    mn = [nets[k] for k in ch["main"]]
    real_set, neg_set = set(ch["real"]), set(ch["realneg"])
    orders: List[Dict[str, Any]] = []
    for k in range(len(ts)):
        entry = ents[k]
        e = bisect.bisect_left(ts, entry)
        lo = bisect.bisect_left(ts, entry - lb)
        s0 = max(lo, e - P.base_long)
        g, l_ = gpre[e] - gpre[s0], lpre[e] - lpre[s0]
        long_pf = round(se.pf_of(g, l_), 4)
        o: Dict[str, Any] = {"k": k, "entry": entry, "exit": ts[k], "net": nets[k], "closed_before": e - s0, "base_long_pf": long_pf}
        ok_flag = se.processed_flag(ts, nets, gpre, lpre, entry, P.base_long, P.gate_min, P.floor, P.max_dd_s, lb, 0, P.consistency)
        if e - s0 < P.gate_min:
            o["base_stop"] = "orders"
        elif long_pf + 1e-9 < P.floor:
            o["base_stop"] = "long"
        elif not ok_flag:
            o["base_stop"] = "drawdown/consistency"
        else:
            o["base_stop"] = ""
            if P.base_short:
                sw = _window(ts, nets, entry, P.base_short, lb, full)
                o["base_short_pf"] = sw["pf"]
                if not sw["judged"] or sw["pf"] + 1e-9 < P.floor:
                    o["base_stop"] = "short"
            if not o["base_stop"] and P.base_min_n:
                mw = _window(ts, nets, entry, P.base_min_n, lb, full)
                o["base_minimal"] = mw["results"]
                if not (mw["judged"] and (all(x > 0 for x in mw["results"]) if P.base_min_positive else all(x < 0 for x in mw["results"]))):
                    o["base_stop"] = "minimal"
        o["in_main"] = k in main_pos
        if o["in_main"]:
            lw = _window(mt, mn, entry, P.main_n, lb, full)
            o["main_long_pf"], o["main_long_n"] = lw["pf"], lw["n"]
            stop = ""
            if P.main_on:
                if not lw["judged"] or lw["pf"] + 1e-9 < P.floor:
                    stop = "main"
                if P.short_on:
                    o["main_short_win"] = ch["short_by_order"][main_pos[k]]          # the window this order's own entry could pick (only Main orders closed before it)
                    sw = _window(mt, mn, entry, o["main_short_win"], lb, full)
                    o["main_short_pf"], o["main_short_n"] = sw["pf"], sw["n"]
                    if not stop and (not sw["judged"] or sw["pf"] + 1e-9 < P.floor):
                        stop = "mainshort"
                if P.main_min_n:
                    kw = _window(mt, mn, entry, P.main_min_n, lb, full)
                    o["main_minimal"] = kw["results"]
                    if not stop and not (kw["judged"] and (all(x > 0 for x in kw["results"]) if P.main_min_positive else all(x < 0 for x in kw["results"]))):
                        stop = "mainminimal"
            o["main_stop"] = stop
            o["in_real"] = k in real_set
            o["in_realneg"] = k in neg_set
        orders.append(o)
    v = se.stage_verdicts([nets[k] for k in ch["main"]], [nets[k] for k in ch["real"]], [nets[k] for k in ch["realneg"]], ch["short_n"], P)
    return {"orders": orders, "main": ch["main"], "real": ch["real"], "realneg": ch["realneg"], "short_n": ch["short_n"], "verdict": v, "main_on": P.main_on}


def consistent(tr: Dict[str, Any]) -> bool:
    """The per-order flags of a trace agree with the chain lists it was built from (the debug view and the engine say the same)."""
    ords = tr["orders"]
    return ([o["k"] for o in ords if o["in_main"]] == tr["main"]
            and [o["k"] for o in ords if o.get("in_real")] == tr["real"]
            and [o["k"] for o in ords if o.get("in_realneg")] == tr["realneg"]
            and all((o["base_stop"] == "") == o["in_main"] for o in ords)
            and all(((o.get("main_stop") or "") == "") == bool(o.get("in_real")) for o in ords if o["in_main"] and tr["main_on"]))


def render(tr: Dict[str, Any], last: int = 40, cost_pct: float = 0.12) -> str:
    out: List[str] = []
    orders = tr["orders"]
    n, nm, nr, nq = len(orders), len(tr["main"]), len(tr["real"]), len(tr["realneg"])
    nets = [o["net"] for o in orders]
    def pf(v): return round(se.pf_of(sum(x for x in v if x > 0), -sum(x for x in v if x < 0)), 3)
    out.append(f"tape {n} orders (PF {pf(nets)}) -> Main Set {nm} (PF {pf([orders[k]['net'] for k in tr['main']])}) -> Real Set {nr} "
               f"(PF {pf([orders[k]['net'] for k in tr['real']])}) | Real Negative Set {nq} (PF {pf([orders[k]['net'] for k in tr['realneg']])}); "
               f"Main short window chosen: {tr['short_n']}; net after one PositionCost of {cost_pct:g} %")
    stops: Dict[str, int] = {}
    for o in orders:
        key = o["base_stop"] or (o.get("main_stop") or "in Real Set")
        stops[key] = stops.get(key, 0) + 1
    out.append("where the orders stopped: " + ", ".join(f"{k} {v}" for k, v in sorted(stops.items(), key=lambda kv: -kv[1])))
    v = tr["verdict"]
    out.append("verdict now: " + ", ".join(f"{k}={v[k]}" for k in ("main", "main_pf", "main_n", "short", "short_pf", "short_n", "short_win", "minimal", "minimal_n", "neg", "real", "real_pf", "real_n", "pos", "negl") if k in v))
    out.append(f"\nthe last {min(last, n)} orders (net %, Base long window PF at the entry, where it stopped):")
    for o in orders[-last:]:
        stop = o["base_stop"] or o.get("main_stop") or ""
        tail = f"main PF {o.get('main_long_pf')}/{o.get('main_long_n')}" + (f" short {o.get('main_short_pf')}" if "main_short_pf" in o else "") if o["in_main"] else ""
        out.append(f"  {o['k']:>5d} entry {o['entry']:.0f} exit {o['exit']:.0f} net {100 * o['net']:+.3f}%  closed-before {o['closed_before']:>3d}  Base PF {o['base_long_pf']:>7}  "
                   f"{'Main' if o['in_main'] else '    '} {'Real' if o.get('in_real') else '    '} {'Neg' if o.get('in_realneg') else '   '}  {('stopped: ' + stop) if stop else ''} {tail}")
    return "\n".join(out)


def main() -> int:
    import stage_audit as sa
    import stage_eval as sg
    from concurrent.futures import ProcessPoolExecutor
    from position_cost import cost_as_frac
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--set", required=True)
    ap.add_argument("--hour", type=int, default=-1)
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--lookback", type=int, default=1440)
    ap.add_argument("--orders", type=int, default=40)
    ap.add_argument("--patch", default="")
    a = ap.parse_args()
    patch = {"holdEnabled": False, "histLookbackBars": a.lookback, "histLookbackAuto": False, **(json.loads(a.patch) if a.patch else {})}
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(patch)
    names, bars, times = sg.load_bars(a.data, a.symbols)
    book = sg.make_book()
    st = book.by_idx[int(a.set)] if a.set.isdigit() else next(x for x in book.by_idx if x.id == a.set)
    rows = []
    for s in names:
        b = bars[s]
        warm = min(book.warmup, max(16, len(b) // 5))
        sig = se.pack_signals(b, book.packs, book.ind_settings, float(times[s][0]), warm)
        dirs, confs = [x[0] for x in sig[st.pack]], [float(x[1]) for x in sig[st.pack]]
        cfg = sg.lane_cfg(book)
        cand = [i for i in range(warm, len(b)) if dirs[i] != 0 and confs[i] >= cfg["entry_conf"]]
        for t in se.SetLane(st, s).scan(0, len(b), b, dirs, confs, True, cfg, cand, se.BarIndex(b)):
            rows.append((times[s][t["exit_i"]] + 60.0, times[s][t["entry_i"]] + 60.0, t["pnl_pct"], t["reason"]))
    rows.sort()
    start = max(times[s][0] for s in names) + book.lookback * 60
    end = min(times[s][-1] for s in names)
    hours = [float(h) for h in range(int(start // 3600 + 1) * 3600, int(end), 3600)][-24:]
    now = hours[a.hour]
    cf = cost_as_frac(book.cost_pct)
    rows = [r for r in rows if r[0] <= now]
    tr = trace_set([r[0] for r in rows], [r[1] for r in rows], [r[2] - cf for r in rows], book.stage_params(st.pack))
    print(f"Set {st.id} (kind {st.kind}, TP {st.tp_pct * 100:.3f} %, SL ratio {st.sl_ratio}); verdict hour {int(now)}")
    print(render(tr, a.orders, book.cost_pct))
    print("trace consistent with the chain lists:", consistent(tr))
    return 0


if __name__ == "__main__":
    sys.exit(main())
