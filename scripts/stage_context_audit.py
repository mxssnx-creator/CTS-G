#!/usr/bin/env python3
"""Recount Base/Main/Real windows of a trusted fixed-hold trade_sim reference.

The shadow cache must be generated locally for the exact data, grid and exit
settings by trade_sim.py; an equal Set count alone does not prove identity.
No exchange or live account is accessed.
"""
from __future__ import annotations

import argparse
import array
import bisect
import hashlib
import json
import math
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


def compare_runtime_boundary(book, tapes, checkpoints):
    """Compare the simulator and runtime Base gates where a close equals a checkpoint.

    Uses the same local paper rows for both. Both decision paths should exclude
    equal-time closes; this does not establish live exchange event ordering.
    """
    if book.fresh_s:
        raise ValueError("boundary comparison requires freshness checks disabled")
    result = {"set_observations": 0, "closes": 0, "different_n_or_pf": 0,
              "strict_pass_runtime_fail": [], "strict_fail_runtime_pass": []}
    for now in checkpoints:
        for st in book.by_idx:
            tx, te, pp = tapes.by_set[st.idx]
            before, through = bisect.bisect_left(tx, now), bisect.bisect_right(tx, now)
            if before == through:
                continue
            result["set_observations"] += 1
            result["closes"] += through - before
            lo = bisect.bisect_left(tx, now - book.history_bars() * 60.0)
            original_hist, original_live, original_proc = st.hist, st.live, st.proc_replayed
            st.hist = [{"t": float(t), "pnl": float(p) - tapes.cf, "pnl_pct": float(p),
                        "hold_s": float(t - ent), "reason": "x"}
                       for t, ent, p in zip(tx[lo:through], te[lo:through], pp[lo:through])]
            st.live, st.proc_replayed = [], False
            book._score_one(st, now=now)
            strict, _, strict_n, strict_pf = sg.gate_base(book, tapes, st, now)
            reason = book.base_validation_reason(st)
            runtime = not reason and (not book.base_min_n or st.base_min_ok)
            if strict_n != st.gate_n or abs(strict_pf - st.gate_pf) > 1e-6:
                result["different_n_or_pf"] += 1
            if bool(strict) != bool(runtime):
                key = "strict_pass_runtime_fail" if strict else "strict_fail_runtime_pass"
                result[key].append({"t": now, "set": st.id, "strict_n": strict_n, "strict_pf": strict_pf,
                                    "runtime_n": st.gate_n, "runtime_pf": st.gate_pf,
                                    "runtime_reason": reason or ("minimal" if not runtime else "")})
            st.hist, st.live, st.proc_replayed = original_hist, original_live, original_proc
    result["note"] = ("Same paper rows, Base only: both decision paths exclude closes at the checkpoint. "
                      "This is not a live execution test.")
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--reference", required=True, help="trade_sim JSON from the exact grid and data")
    ap.add_argument("--out", required=True)
    ap.add_argument("--compare-runtime-boundary", action="store_true",
                    help="compare simulator and runtime Base decision scoring at equal timestamps")
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
    for symbol in names:
        if any(b - a != 60 for a, b in zip(times[symbol], times[symbol][1:])):
            raise ValueError(f"non-contiguous one-minute candles for {symbol}")
        for o, h, low, c, volume in bars[symbol]:
            if (not all(math.isfinite(v) for v in (o, h, low, c, volume))
                    or min(o, h, low, c) <= 0 or volume < 0
                    or not low <= min(o, c) <= max(o, c) <= h):
                raise ValueError(f"invalid OHLCV candle for {symbol}")
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
    tapes = sg.Tapes(book, names, times, shadow)
    result = summarize(book, tapes, checkpoints)
    if args.compare_runtime_boundary:
        result["boundary_comparison"] = compare_runtime_boundary(book, tapes, checkpoints)
    target = book.history_bars()
    first_counts = {s: bisect.bisect_left(times[s], checkpoints[0]) for s in names}
    last_counts = {s: bisect.bisect_left(times[s], checkpoints[-1]) for s in names}
    result.update({
        "symbols": names, "sets": len(book.by_idx), "lookback_bars": book.lookback,
        "cost_pct": book.cost_pct, "history_context_target_bars": target,
        "market_rows": {s: len(bars[s]) for s in names}, "contiguous_valid_ohlcv": True,
        "market_bars_before_first_checkpoint": first_counts,
        "market_bars_before_last_checkpoint": last_counts,
        "context_count_full_at_first_checkpoint": all(v >= target for v in first_counts.values()),
        "context_count_full_at_last_checkpoint": all(v >= target for v in last_counts.values()),
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
