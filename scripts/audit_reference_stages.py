#!/usr/bin/env python3
"""Report each passing stage's own net-return PF on trade_sim's hourly checkpoints.

Requires the same trusted cache, data and fixed, auto-holds-off settings as the
reference run. Counts and trade observations may repeat across checkpoints.
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--cache", required=True, help="trusted cache made by this version")
    ap.add_argument("--result", required=True, help="trade_sim JSON; automatic holds must be off")
    args = ap.parse_args()
    with open(args.result, encoding="utf-8") as fh:
        sim = json.load(fh)
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps({"histLookbackBars": sim["gate"]["lookback_bars"],
                                                     "histLookbackAuto": False, "holdEnabled": False,
                                                     "positionCostPct": sim["config"]["cost_pct"]})
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import stage_eval as sg
    names, bars, times = sg.load_bars(args.data, len(sim["symbols"]))
    if names != sim["symbols"]:
        raise ValueError("data symbols differ from the simulation")
    # A cache is a pickle: accept only locally generated, trusted input.
    with open(args.cache, "rb") as fh:
        shadow = pickle.load(fh)
    book = sg.make_book()
    if len(book.by_idx) != sim["sets"] or book.cost_pct != sim["config"]["cost_pct"]:
        raise ValueError("Set grid or cost differs from the simulation")
    tapes = sg.Tapes(book, names, times, shadow)
    keys = ("base", "main", "real", "realNeg")
    def blank():
        return {k: {"sets": 0, "gain": 0.0, "loss": 0.0, "orders": 0} for k in keys}
    total, hourly = blank(), []
    for checkpoint in sim["evaluations"]:
        stats = blank()
        def add(key, gain, loss, n):
            item = stats[key]
            item["sets"] += 1
            item["gain"] += gain
            item["loss"] += loss
            item["orders"] += n
        for st in book.by_idx:
            if not sg.gate_base(book, tapes, st, checkpoint["t"])[0]:
                continue
            base = sg.base_eval(book, tapes, st, checkpoint["t"])
            add("base", base["gp"], base["gl"], base["n"])
            w = sg.window_figures(book, tapes, st, checkpoint["t"])
            main, short, minimal, real, neg = w["mainLong"], w["mainShort"], w["mainMinimal"], w["real"], w["realNeg"]
            main_pass = (not book.main_on) or bool(main and main[0] and (short is None or short[0]) and (minimal is None or minimal[0]))
            if main is not None and main_pass:
                add("main", main[1], main[2], main[3])
            if main_pass and real and real[0]:
                add("real", real[1], real[2], real[3])
            if neg and neg[0]:
                add("realNeg", neg[1], neg[2], neg[3])
        for key in keys:
            for metric in ("sets", "gain", "loss", "orders"):
                total[key][metric] += stats[key][metric]
        hourly.append({"t": checkpoint["t"], **stats})
    denominator = len(hourly) * len(book.by_idx)
    summary = {key: {**value,
                     "pct_average": 100 * value["sets"] / denominator if denominator else 0,
                     "pf": value["gain"] / value["loss"] if value["loss"] else None}
               for key, value in total.items()}
    print(json.dumps({"summary": summary, "hourly": hourly,
                      "note": "hourly Set observations; trades may repeat; Real and Real Negative are separate lanes"}, indent=2))


if __name__ == "__main__":
    main()
