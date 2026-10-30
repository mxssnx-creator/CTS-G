#!/usr/bin/env python3
"""Independently check fixed-TP/SL model fills from a trade_sim reference run.

The input cache must come from the same code and settings as the supplied result.
Trailing fills are reported as unaudited rather than silently accepted.
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--cache", required=True, help="trusted trade_sim cache made by this version")
    ap.add_argument("--result", required=True, help="trade_sim JSON; automatic holds must be off")
    args = ap.parse_args()
    sim = json.load(open(args.result, encoding="utf-8"))
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps({"histLookbackBars": sim["gate"]["lookback_bars"],
                                                     "histLookbackAuto": False, "holdEnabled": False,
                                                     "positionCostPct": sim["config"]["cost_pct"]})
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import stage_eval as sg
    names, bars, times = sg.load_bars(args.data, len(sim["symbols"]))
    if names != sim["symbols"]:
        raise ValueError("data symbols differ from the simulation")
    # Only read a locally generated, trusted cache: pickle is not a safe interchange format.
    with open(args.cache, "rb") as fh:
        shadow = pickle.load(fh)
    book = sg.make_book()
    if len(book.by_idx) != sim["sets"] or book.cost_pct != sim["config"]["cost_pct"]:
        raise ValueError("Set grid or cost differs from the simulation")
    tapes = sg.Tapes(book, names, times, shadow)
    hours = [x["t"] for x in sim["evaluations"]]
    hi = hours[-1] + 3600
    rows = [r for r in sg.execute(book, names, bars, times, shadow, tapes, hours, refresh_s=0)
            if "tr" in r and hours[0] <= r["t_in"] < hi]
    by_id = {s.id: s for s in book.by_idx}
    bad, unaudited, sides, nets = [], [], {"LONG": 0, "SHORT": 0}, []
    actual = sorted((r["symbol"], r["set"], r["t_in"], r["t"], r["reason"], round(100 * r["net"], 4)) for r in rows)
    recorded = sorted((r["symbol"], r["set"], r["t_in"], r["t_out"], r["reason"], r["net_pct"]) for r in sim["trades"])
    if actual != recorded:
        bad.append("recomputed trade identities or returns differ from the result")
    for r in rows:
        st, tr, series = by_id[r["set"]], r["tr"], bars[r["symbol"]]
        i, side = tr["entry_i"], tr["dir"]
        sides["LONG" if side > 0 else "SHORT"] += 1
        nets.append(r["net"])
        if st.kind not in ("base", "exit"):
            unaudited.append({"symbol": r["symbol"], "set": st.id})
            continue
        entry = series[i][3]
        cost = book.cost_pct / 100.0
        sl_fraction = max((1 + book.sl_min_steps) * cost, st.tp_pct * st.sl_ratio)
        tp_fraction = max(2 * cost, st.tp_pct)
        sl, tp = entry * (1 - side * sl_fraction), entry * (1 + side * tp_fraction)
        result = None
        for j in range(i + 1, len(series)):
            high, low, close = series[j][1:4]
            if low <= sl if side > 0 else high >= sl:
                result = (j, "sl", sl)
            elif st.kind == "base" and (high >= tp if side > 0 else low <= tp):
                result = (j, "tp", tp)
            elif j - i >= book.hist_time_bars:
                result = (j, "time", close)
            elif j - i >= book.scratch_bars and (close - entry) / entry * side >= book.scratch_min:
                result = (j, "scratch+", close)
            if result:
                break
        if (result is None or result[0] != tr["exit_i"] or result[1] != tr["reason"]
                or abs(result[2] - tr["exit_px"]) > 1e-9
                or abs(r["net"] - ((tr["exit_px"] - entry) / entry * side - cost)) > 1e-10):
            bad.append({"symbol": r["symbol"], "set": st.id, "error": "exit or cost mismatch"})
        if not sg.gate_metrics(book, tapes, st, r["t_in"])[0]:
            bad.append({"symbol": r["symbol"], "set": st.id, "error": "not validated at entry"})
    gain = sum(x for x in nets if x > 0)
    loss = -sum(x for x in nets if x < 0)
    print(json.dumps({"closes": len(rows), "directions": sides, "mismatches": bad, "unaudited_trailing": unaudited,
                      "gain": gain, "loss": loss, "pf": gain / loss if loss else None, "cost_pct": book.cost_pct}, indent=2))
    return 1 if bad or unaudited else 0


if __name__ == "__main__":
    sys.exit(main())
