#!/usr/bin/env python3
"""Coverage census: does every strategy, type, direction, step range and configured indication actually process through the stages?

One SetBook with everything switched on (general, indications, Short Range, the five extra packs, and a set of `indicationConfigs` entries covering several
families x ranges x timeframes x relations), replayed over stored 1m history exactly as the engine does. Per group (pack x type x direction x TP-step range)
and per configured indication entry it counts, for the test window:

  Sets        configured Sets                      traded     Sets with at least one order (a group with none never processed anything)
  Base/Main/Real/executable   orders after each gate (the engine's chain, which scripts/independent_progress_audit.py checks against a plain reading) and the
                              Sets that have at least one order there; executable = also through the Real window at the entry, the orders execution would take
  PF          gains over losses of the pooled net results of that gate's orders (one PositionCost each)

A group with Sets but no order, or with orders and none past a gate, is listed: it is either a signal that never fires on this data or a stage that blocks
everything, and the report says which gate. Nothing is tuned here; this is a map of what processes.

usage: coverage_census.py --data DIR --out FILE.json [--txt FILE.txt] [--symbols 8] [--lookback 1440] [--hours 24] [--jobs 3] [--patch JSON]"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))
import main_stage_audit as ma  # noqa: E402
import set_engine as se  # noqa: E402
import stage_audit as sa  # noqa: E402
import stage_eval as sg  # noqa: E402
from position_cost import cost_as_frac  # noqa: E402

DEMO_CONFIGS = [
    {"label": "trend fast", "families": ["trend", "donch"], "ranges": [5, 8], "timeframes": [1, 5], "relations": ["", "T5"], "stepMin": 3, "stepMax": 10, "slRatios": [1.0, 2.0]},
    {"label": "breakout votes", "families": ["brk", "kelt"], "ranges": [8, 14], "timeframes": ["c", "x"], "relations": [""], "stepMin": 4, "stepMax": 16, "slRatios": [1.0, 1.5]},
    {"label": "reversion and flow", "families": ["bbrev", "obv"], "ranges": [8, 14], "timeframes": [1, 15], "relations": ["", "V"], "stepMin": 2, "stepMax": 8},
    {"label": "groups", "names": ["g_trend_min", "g_volume_med", "g_all_med"], "stepMin": 3, "stepMax": 12, "types": ["base", "exit"], "pause": 0},
    {"label": "thirty minute", "families": ["trend"], "ranges": [8, 21], "timeframes": [30], "relations": ["", "B15"], "stepMin": 6, "stepMax": 20, "types": ["base", "exit", "trail"]},
]
EVERYTHING = {"stratGeneral": True, "stratIndications": True, "stratShortRange": True, "stratTrend": True, "stratBreakout": True, "stratReversion": True,
              "stratFlow": True, "stratMulti": True, "indicationConfigs": DEMO_CONFIGS}
GATES = ("tape", "base", "main", "real", "exec")


def range_of(step: int) -> str:
    return "2-6" if step <= 6 else "7-12" if step <= 12 else "13-20" if step <= 20 else "21-32"


def real_ok(real_t, real_nets, entry: float, P) -> bool:
    idx = ma._closed(real_t, entry, P.lookback_s, P.real_n)
    return len(idx) >= (P.real_n if P.full_window else 1) and round(ma._pf([real_nets[j] for j in idx]), 4) + 1e-9 >= P.floor_for(P.real_n)


G: Dict[str, Any] = {}


def census_set(idx: int):
    st, rows, t0, book = G["sets"][idx], G["rows"][idx], G["t0"], G["book"]
    if not rows:
        return idx, None
    cf = cost_as_frac(book.cost_pct)
    ts, ents, nets = [r[0] for r in rows], [r[1] for r in rows], [r[2] - cf for r in rows]
    P = book.stage_params(st.pack, st)
    ch = se.stage_chain(ts, ents, nets, P)
    rt, rn = [ts[k] for k in ch["real"]], [nets[k] for k in ch["real"]]
    members = {"tape": range(len(rows)), "base": ch["main"], "main": ch["real"] if P.main_on else ch["main"], "real": ch["real"],
               "exec": [k for k in ch["real"] if real_ok(rt, rn, ents[k], P)]}
    out = {}
    for gate in GATES:
        gp = gl = 0.0
        n = 0
        for k in members[gate]:
            if ents[k] < t0:
                continue
            n += 1
            if nets[k] > 0:
                gp += nets[k]
            else:
                gl -= nets[k]
        out[gate] = (gp, gl, n)
    return idx, out


def job(args):
    s, bars, t0 = args
    _, out = sa._job((s, bars, t0))
    return s, out


def pf(g: float, l_: float):
    return round(g / l_, 3) if l_ > 0 else (None if g <= 0 else 99.0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--txt", default="")
    ap.add_argument("--symbols", type=int, default=8)
    ap.add_argument("--lookback", type=int, default=1440)
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--patch", default="")
    a = ap.parse_args()
    t_start = time.time()
    patch = {"holdEnabled": False, "histLookbackBars": a.lookback, "histLookbackAuto": False, **EVERYTHING, **(json.loads(a.patch) if a.patch else {})}
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(patch)
    names, bars, times = sg.load_bars(a.data, a.symbols)
    book = sg.make_book()
    data_end = min(times[s][-1] for s in names)
    t0 = float(int(data_end // 3600) * 3600) - a.hours * 3600.0
    dropped = ((book.snapshot().get("indicationConfigs") or {}) if hasattr(book, "snapshot") else {})
    print(f"{len(book.by_idx)} Sets configured over {len(book.packs)} packs; replaying {len(names)} symbols  [{time.time() - t_start:.0f}s]", flush=True)
    with ProcessPoolExecutor(max_workers=a.jobs) as pool:
        results = list(pool.map(job, [(s, bars[s], times[s][0]) for s in names]))
    rows: Dict[int, List[tuple]] = {st.idx: [] for st in book.by_idx}
    for s, out in results:
        for st in book.by_idx:
            for (ei, xi, pp, why) in out[st.idx]:
                rows[st.idx].append((times[s][xi] + 60.0, times[s][ei] + 60.0, pp, why))
    for r in rows.values():
        r.sort()
    print(f"replayed  [{time.time() - t_start:.0f}s]", flush=True)
    G.update(sets={st.idx: st for st in book.by_idx}, rows=rows, t0=t0, book=book)
    with ProcessPoolExecutor(max_workers=a.jobs) as pool:
        per_set = dict(pool.map(census_set, [st.idx for st in book.by_idx], chunksize=32))
    groups: Dict[Tuple, Dict[str, Any]] = defaultdict(lambda: {"sets": 0, "traded": 0, **{g: [0.0, 0.0, 0, 0] for g in GATES}})
    by_pack: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"sets": 0, "traded": 0, **{g: [0.0, 0.0, 0, 0] for g in GATES}})
    for st in book.by_idx:
        for key, tab in (((st.pack, st.kind, "long" if st.side > 0 else "short" if st.side < 0 else "both", range_of(st.step)), groups), (st.pack, by_pack)):
            e = tab[key]
            e["sets"] += 1
            res = per_set.get(st.idx)
            if res is None:
                continue
            if res["tape"][2] > 0:
                e["traded"] += 1
            for g in GATES:
                gp, gl, n = res[g]
                e[g][0] += gp
                e[g][1] += gl
                e[g][2] += n
                e[g][3] += 1 if n > 0 else 0
    result = {"meta": {"symbols": names, "sets": len(book.by_idx), "packs": list(book.packs), "test_start": int(t0), "test_hours": a.hours, "lookback_bars": a.lookback,
                       "indicationConfigs": dropped},
              "packs": {k: v for k, v in sorted(by_pack.items())}, "groups": {"|".join(k): v for k, v in sorted(groups.items())}}
    json.dump(result, open(a.out, "w"))
    txt = render(result)
    if a.txt:
        open(a.txt, "w", encoding="utf-8").write(txt)
    print(txt)
    print(f"done in {time.time() - t_start:.0f}s")
    return 0


def render(r: Dict[str, Any]) -> str:
    m = r["meta"]
    L = [f"coverage census: {m['sets']} Sets, {len(m['packs'])} packs, {len(m['symbols'])} symbols, test window {time.strftime('%m-%d %H:%M', time.gmtime(m['test_start']))} UTC + {m['test_hours']} h", ""]
    head = f"  {'':<34s}{'Sets':>6s}{'traded':>7s}" + "".join(f"{g + ' n':>10s}{'PF':>7s}{'Sets':>6s}" for g in GATES)
    L.append("PER PACK (orders after each gate in the test window, PF of those orders, Sets with at least one)")
    L.append(head)
    for k, v in r["packs"].items():
        L.append(f"  {k:<34s}{v['sets']:>6d}{v['traded']:>7d}" + "".join(f"{v[g][2]:>10d}{str(pf(v[g][0], v[g][1])):>7s}{v[g][3]:>6d}" for g in GATES))
    L.append("")
    L.append("PER GROUP (pack | type | direction | TP-step range)")
    L.append(head)
    dead: List[str] = []
    for k, v in r["groups"].items():
        L.append(f"  {k:<34s}{v['sets']:>6d}{v['traded']:>7d}" + "".join(f"{v[g][2]:>10d}{str(pf(v[g][0], v[g][1])):>7s}{v[g][3]:>6d}" for g in GATES))
        if v["traded"] == 0:
            dead.append(f"{k}: {v['sets']} Sets, none traded (no signal fired on this data in this direction)")
        else:
            first = next((g for g in GATES[1:] if v[g][2] == 0), None)
            if first:
                dead.append(f"{k}: {v['traded']} of {v['sets']} Sets traded; no order passes the {first} gate")
    L.append("")
    L.append(f"GROUPS THAT DO NOT REACH EXECUTION ({len(dead)}):")
    L.extend("  " + d for d in dead)
    return "\n".join(L)


if __name__ == "__main__":
    sys.exit(main())
