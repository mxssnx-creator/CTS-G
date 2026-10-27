#!/usr/bin/env python3
"""Measure the extra indicators one by one, on stored 1m history, net of PositionCost.

Protocol: the history is split by time into three parts, A (first third), B (second third) and S (last third, sealed).
Indicators and exit shapes are ranked on A and B only; S is read once, for the finalists, so it never steers a choice.
Every entry is a signal at the close of a bar; one position per symbol and indicator at a time; exits are the engine's
pessimistic same-bar rule (stop wins), or the close after `time_bars` bars.

usage: indicator_eval.py --data DIR --out FILE [--step 3] [--cost 0.12] [--workers 4]
  DIR holds <SYMBOL>.json files of rows [t_ms, o, h, l, c, v] and nothing else is read.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from multiprocessing import Pool
from typing import Any, Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))
import indication_extra as ix  # noqa: E402
from set_engine import hit_exit  # noqa: E402

EXITS: Dict[str, Tuple[float, float]] = {   # name: (tp %, sl %)
    "tp.48/sl.48": (0.48, 0.48),
    "tp.96/sl.48": (0.96, 0.48),
    "tp1.44/sl.72": (1.44, 0.72),
    "tp.72/sl.36": (0.72, 0.36),
}
def step_exits(cost: float) -> Dict[str, Tuple[float, float]]:
    """Exit shapes in the engine's own units: target = step x PositionCost, stop = SL ratio x target, never under the
    overall minimum of one PositionCost plus one step (two PositionCosts)."""
    out: Dict[str, Tuple[float, float]] = {}
    for step in (2, 3, 4, 6):
        for ratio in (0.5, 1.0, 2.0, 3.5):
            tp = step * cost
            out[f"s{step}/r{ratio}"] = (tp, max(ratio * tp, 2.0 * cost))
    return out


TIME_BARS = 60
ENTRY_CONF = 0.58
WARM = 100


def _simulate(bars: List[List[float]], i: int, side: int, tp_pct: float, sl_pct: float) -> Tuple[float, int]:
    entry = bars[i][3]
    sl = entry * (1 - side * sl_pct / 100.0)
    tp = entry * (1 + side * tp_pct / 100.0)
    last = min(len(bars) - 1, i + TIME_BARS)
    for j in range(i + 1, last + 1):
        why, px = hit_exit(side, entry, sl, tp, None, bars[j])
        if why:
            return side * (px - entry) / entry * 100.0, j
    px = bars[last][3]
    return side * (px - entry) / entry * 100.0, last


def _one_symbol(args: Tuple[str, List[List[float]], int, float, List[str], Dict[str, Tuple[float, float]]]) -> Dict[str, Any]:
    sym, bars, step, cost, names, EXITS = args
    n = len(bars)
    # trades[(indicator, exit)] = list of (entry_index, net %)
    trades: Dict[str, List[Tuple[int, float]]] = {f"{nm}|{ex}": [] for nm in names for ex in EXITS}
    busy: Dict[str, int] = {k: -1 for k in trades}
    for i in range(WARM, n - 2, step):
        win = bars[max(0, i + 1 - ix.LOOKBACK) : i + 1]
        for nm in names:
            try:
                sig = ix.INDICATORS[nm](win)
            except Exception:
                sig = None
            if not sig or sig[1] < ENTRY_CONF:
                continue
            side = int(sig[0])
            for ex, (tp, sl) in EXITS.items():
                key = f"{nm}|{ex}"
                if i <= busy[key]:
                    continue
                gross, end = _simulate(bars, i, side, tp, sl)
                busy[key] = end
                trades[key].append((i, gross - cost))
    return {"symbol": sym, "n": n, "trades": trades}


def _pf(vals: List[float]) -> Dict[str, float]:
    gp = sum(v for v in vals if v > 0)
    gl = -sum(v for v in vals if v < 0)
    pf = gp / gl if gl > 0 else (99.0 if gp > 0 else 0.0)
    return {"n": len(vals), "pf": round(pf, 4), "avg": round(sum(vals) / len(vals), 5) if vals else 0.0,
            "wr": round(sum(1 for v in vals if v > 0) / len(vals), 4) if vals else 0.0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--step", type=int, default=3)
    ap.add_argument("--cost", type=float, default=0.12)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--exits", choices=("legacy", "steps"), default="legacy")
    ap.add_argument("--only", default="", help="comma list of indicator names (default: all)")
    a = ap.parse_args()
    names = [x for x in a.only.split(",") if x] or list(ix.INDICATORS)
    exits = step_exits(a.cost) if a.exits == "steps" else dict(EXITS)
    jobs = []
    for f in sorted(glob.glob(os.path.join(a.data, "*.json"))):
        if os.path.basename(f).startswith("_"):
            continue
        rows = json.load(open(f))
        jobs.append((os.path.basename(f)[:-5], [[r[1], r[2], r[3], r[4], r[5]] for r in rows], a.step, a.cost, names, exits))
    with Pool(a.workers) as pool:
        res = pool.map(_one_symbol, jobs)
    parts: Dict[str, Dict[str, List[float]]] = {}
    for r in res:
        n = r["n"]
        cut1, cut2 = n // 3, 2 * n // 3
        for key, tr in r["trades"].items():
            d = parts.setdefault(key, {"A": [], "B": [], "S": []})
            for i, net in tr:
                d["A" if i < cut1 else "B" if i < cut2 else "S"].append(net)
    out = {k: {p: _pf(v) for p, v in d.items()} for k, d in parts.items()}
    json.dump({"cost": a.cost, "step": a.step, "symbols": [j[0] for j in jobs], "results": out}, open(a.out, "w"), indent=1)
    print(f"{len(out)} indicator x exit rows, {len(jobs)} symbols -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
