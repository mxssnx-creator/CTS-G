#!/usr/bin/env python3
"""Compare pooled Base evidence with same-Set, symbol and side evidence.

This is a diagnosis of a trade_sim reference run, not a simulated isolated gate:
it does not rerank Sets or model any resulting changes in entries and exits.
Automatic holds must be off and the cache must come from the same settings.
"""
from __future__ import annotations

import argparse
import bisect
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
    with open(args.result, encoding="utf-8") as fh:
        sim = json.load(fh)
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps({"histLookbackBars": sim["gate"]["lookback_bars"],
                                                     "histLookbackAuto": False, "holdEnabled": False})
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import stage_eval as sg

    names, _, times = sg.load_bars(args.data, len(sim["symbols"]))
    if names != sim["symbols"]:
        raise ValueError("data symbols differ from the simulation")
    # A cache is a pickle: accept only locally generated, trusted input.
    with open(args.cache, "rb") as fh:
        shadow = pickle.load(fh)
    book = sg.make_book()
    if (len(book.by_idx) != sim["sets"] or book.cost_pct != sim["config"]["cost_pct"]
            or book.gate_window != sim["gate"]["window"] or book.lookback != sim["gate"]["lookback_bars"]):
        raise ValueError("Set grid, gate window, lookback or cost differs from the simulation")
    tapes = sg.Tapes(book, names, times, shadow)
    by_id = {st.id: st for st in book.by_idx}
    details = []
    for trade in sim["trades"]:
        symbol, now = trade["symbol"], trade["t_in"]
        st = by_id[trade["set"]]
        if st.pack != trade["pack"] or st.kind != trade["kind"]:
            raise ValueError("recorded Set identity differs from the current grid")
        ts = times[symbol]
        i = bisect.bisect_left(ts, now - 60)
        if i >= len(ts) or ts[i] + 60 != now:
            raise ValueError("entry does not match a signal bar")
        dirs, confs = shadow[symbol][1:3]
        side = dirs[st.pack][i]
        if side not in (-1, 1) or confs[st.pack][i] < book.entry_conf:
            raise ValueError("recorded entry does not match a qualifying signal")
        pooled = sg.base_eval(book, tapes, st, now)
        if not sg.gate_metrics(book, tapes, st, now)[0]:
            raise ValueError("recorded entry no longer passes its pooled gate")
        ent, ex, returns = shadow[symbol][3][st.idx]
        # Keep only closes before the entry, within lookback, for the same side.
        # Sort using the same (exit, entry, gross return) keys as stage_eval.Tapes.
        closes = sorted((ts[j] + 60, ts[a] + 60, p) for a, j, p in zip(ent, ex, returns)
                        if now - book.lookback * 60 <= ts[j] + 60 < now and dirs[st.pack][a] == side)
        window = [p - tapes.cf for _, _, p in closes[-book.gate_window:]]
        gain = sum(x for x in window if x > 0)
        loss = -sum(x for x in window if x < 0)
        details.append({"symbol": symbol, "side": "LONG" if side > 0 else "SHORT", "set": st.id,
                        "entry": now, "context_n": len(window), "context_gain": gain, "context_loss": loss,
                        "context_pf": gain / loss if loss else None, "pooled_n": pooled["n"],
                        "pooled_pf": pooled["pf"], "floor": pooled["floor"]})
    summary = {"executions": len(details),
               "unique_contexts": len({(r["set"], r["symbol"], r["side"]) for r in details}),
               "below_admission_min": sum(r["context_n"] < book.gate_min for r in details),
               "below_collection_target": sum(r["context_n"] < book.lookback_sample_target for r in details),
               # The engine rounds the Base PF to four decimals before checking its floor.
               "context_pf_below_floor": sum(r["context_pf"] is not None and round(r["context_pf"], 4) + 1e-9 < r["floor"] for r in details),
               "context_no_losses": sum(r["context_n"] > 0 and r["context_loss"] == 0 for r in details),
               "context_no_closes": sum(r["context_n"] == 0 for r in details)}
    print(json.dumps({"summary": summary, "details": details,
                      "note": "same-Set, symbol and side Base PF comparison at each recorded entry; not an isolated-gate walk-forward"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
