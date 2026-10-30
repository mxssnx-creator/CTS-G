#!/usr/bin/env python3
"""Recount Base/Main/Real windows of a trusted fixed-hold trade_sim reference.

The shadow cache must be generated locally for the exact data, grid and exit
settings by trade_sim.py; an equal Set count alone does not prove identity.
No exchange or live account is accessed.
"""
from __future__ import annotations

import argparse
import array
import hashlib
import json
import os
import pickle
from pathlib import Path


class RestrictedShadow(pickle.Unpickler):
    """Only the array constructors used by the local shadow format are allowed."""
    def find_class(self, module, name):
        if module == "array" and name in ("array", "_array_reconstructor"):
            return getattr(array, name)
        raise pickle.UnpicklingError(f"unsupported shadow type {module}.{name}")


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def summarize(book, tapes, checkpoints):
    """Aggregate only passing windows; observations repeat across checkpoints."""
    stages = ("base", "main", "real", "realNeg")
    totals = {k: {"observations": 0, "gain": 0.0, "loss": 0.0, "orders": 0} for k in stages}
    hourly = []
    for now in checkpoints:
        counts = {k: 0 for k in stages}
        for st in book.by_idx:
            if not sg.gate_base(book, tapes, st, now)[0]:
                continue
            base = sg.base_eval(book, tapes, st, now)
            windows = sg.window_figures(book, tapes, st, now)
            main = windows["mainLong"]
            short = windows["mainShort"]
            selected = {
                "base": (True, base["gp"], base["gl"], base["n"]),
                "main": main if main and main[0] and (short is None or short[0]) else None,
                "real": windows["real"] if windows["real"] and windows["real"][0] else None,
                "realNeg": windows["realNeg"] if windows["realNeg"] and windows["realNeg"][0] else None,
            }
            for stage, win in selected.items():
                if win is None:
                    continue
                row = totals[stage]
                row["observations"] += 1
                row["gain"] += win[1]
                row["loss"] += win[2]
                row["orders"] += win[3]
                counts[stage] += 1
        hourly.append({"t": now, **counts})
    denominator = len(checkpoints) * len(book.by_idx)
    for row in totals.values():
        row["percent"] = 100.0 * row["observations"] / denominator if denominator else None
        row["pf"] = row["gain"] / row["loss"] if row["loss"] > 0 else None
        row["no_loss"] = row["loss"] == 0 and row["gain"] > 0
    return {"summary": totals, "checkpoints": hourly}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--reference", required=True, help="trade_sim JSON from the exact grid and data")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    reference = json.loads(Path(args.reference).read_text())
    cfg, gates = reference["config"], reference["gate"]
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps({
        "histLookbackBars": gates["lookback_bars"], "histLookbackAuto": False,
        "holdEnabled": False, "positionCostPct": cfg["cost_pct"],
        "extraIndicators": cfg.get("extra_indicators", []),
    })
    global sg
    import stage_eval as sg
    names, bars, times = sg.load_bars(args.data, len(reference["symbols"]))
    with open(args.cache, "rb") as f:
        shadow = RestrictedShadow(f).load()
    book = sg.make_book()
    if names != reference["symbols"] or len(book.by_idx) != reference["sets"]:
        raise ValueError("reference symbols or grid count differ")
    if any(len(shadow[s][3]) != len(book.by_idx) for s in names):
        raise ValueError("shadow cache grid count differs")
    if (book.gate_window != gates["window"] or book.base_short != gates["short"]
            or book.main_n != gates["main_n"] or book.gate_min != gates["min"]
            or book.lookback != gates["lookback_bars"] or book.min_pf != gates["min_pf"]
            or book.max_ddt_h != gates["max_ddt_h"]):
        raise ValueError("reference window settings differ")
    expected = {"honor_tp_trail": book.hist_honor_tp, "exit_sets": book.exit_sets,
                "time_bars": book.hist_time_bars, "scratch_bars": book.scratch_bars,
                "scratch_min_pct": round(book.scratch_min * 100, 3), "cost_pct": book.cost_pct}
    if any(cfg.get(k) != v for k, v in expected.items()):
        raise ValueError("reference exit or cost settings differ")
    checkpoints = [float(e["t"]) for e in reference["evaluations"]]
    if not checkpoints or checkpoints != sorted(set(checkpoints)):
        raise ValueError("reference checkpoints must be non-empty and unique, in time order")
    if checkpoints[0] < max(times[s][0] for s in names) or checkpoints[-1] > min(times[s][-1] for s in names):
        raise ValueError("reference checkpoints fall outside the common market data")
    result = summarize(book, sg.Tapes(book, names, times, shadow), checkpoints)
    result.update({
        "symbols": names, "sets": len(book.by_idx), "lookback_bars": book.lookback,
        "cost_pct": book.cost_pct, "history_context_target_bars": book.history_bars(),
        "pf_by_window_size": book.pf_by_size, "size_floors": list(book.size_floors),
        "long_floor": book.size_long_pf,
        "market_file_sha256": {s: digest(Path(args.data) / f"{s}.json") for s in names},
        "cache_sha256": digest(args.cache),
        "reference_sha256": digest(args.reference),
        "note": "Fixed-hold offline replay; overlapping Set windows are not independent. "
                "Grid count and recorded settings do not authenticate cache identity or every setting. "
                "No exchange data was accessed.",
    })
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
