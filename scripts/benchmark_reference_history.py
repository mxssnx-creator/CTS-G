#!/usr/bin/env python3
"""Measure a SetBook replay on reference data (Linux peak RSS)."""
from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--result", required=True, help="trade_sim JSON; automatic holds must be off")
    select = ap.add_mutually_exclusive_group(required=True)
    select.add_argument("--symbol")
    select.add_argument("--symbols", type=int, help="replay the first N sorted reference symbols")
    args = ap.parse_args()
    with open(args.result, encoding="utf-8") as fh:
        sim = json.load(fh)
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps({"histLookbackBars": sim["gate"]["lookback_bars"],
                                                     "histLookbackAuto": False, "holdEnabled": False,
                                                     "positionCostPct": sim["config"]["cost_pct"]})
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import stage_eval as sg
    names, bars, times = sg.load_bars(args.data, len(sim["symbols"]))
    book = sg.make_book()
    if names != sim["symbols"] or (args.symbol is not None and args.symbol not in names):
        raise ValueError("data symbols differ from the simulation")
    if args.symbols is not None and not 1 <= args.symbols <= len(names):
        raise ValueError("symbol count is outside the reference universe")
    if len(book.by_idx) != sim["sets"] or book.cost_pct != sim["config"]["cost_pct"]:
        raise ValueError("Set grid or cost differs from the simulation")
    now = float(sim["evaluations"][0]["t"])
    chosen = [args.symbol] if args.symbol is not None else names[:args.symbols]
    for symbol in chosen:
        i = times[symbol].index(int(now))
        if i < book.lookback:
            raise ValueError("not enough prehistory before first checkpoint")
        book.bars[symbol] = bars[symbol][i - book.lookback:i]
        book.bar_t[symbol] = times[symbol][i - 1]
    start = time.monotonic()
    book.replay_all(now=now)
    result = {"input_symbols_loaded": len(names), "replayed_symbols": len(chosen), "symbols": chosen,
              "sets": len(book.by_idx), "bars_per_symbol": book.lookback, "holds_enabled": book.hold_on,
              "elapsed_s": round(time.monotonic() - start, 3),
              "peak_rss_kib_linux": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              "phase": book.progress.phase, "errors": book.progress.errors,
              "history_rows": sum(len(rows) for symbol in chosen for rows in book._hist_rows.get(symbol, {}).values()),
              "note": "shared host; automatic holds off, not a production live-cycle benchmark"}
    print(json.dumps(result, indent=2))
    return 1 if book.progress.errors or book.progress.phase != "ready" else 0


if __name__ == "__main__":
    sys.exit(main())
