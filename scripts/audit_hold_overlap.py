#!/usr/bin/env python3
"""Audit selected automatic-hold paper rows for same-symbol overlap.

This one-symbol research check uses the existing candidate-tape splicing and
does not verify pooled multi-symbol choices, live positions, cooldown, missing
entries, profitability, or a corrected hold implementation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import stage_eval as sg
import set_engine as se


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--symbols", type=int, default=8)
    ap.add_argument("--bars", type=int, default=2910)
    ap.add_argument("--patch", default="{}", help="SetBook overlay overrides as JSON")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    patch = json.loads(args.patch)
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(patch)
    book = sg.make_book()
    names, all_bars, all_times = sg.load_bars(args.data, args.symbols)
    if not book.hold_auto or args.symbol not in names or args.bars < 400:
        raise ValueError("automatic holds, a loaded symbol and at least 400 bars are required")
    bars, times = all_bars[args.symbol][-args.bars:], all_times[args.symbol][-args.bars:]
    n = len(bars)
    if n < 400 or any(b - a != 60 for a, b in zip(times, times[1:])):
        raise ValueError("at least 400 contiguous one-minute candles are required")
    warm = min(book.warmup, max(16, n // 5))
    cfg = sg.lane_cfg(book)
    cfg["time_bars"] = max(8, min(book.hist_time_bars, max(8, n - warm - 1)))
    signals = se.pack_signals(bars, book.packs, book.ind_settings, times[0] + 60, warm)
    acc = se.BarIndex(bars)
    dirs = {p: [r[0] for r in signals[p]] for p in book.packs}
    confs = {p: [r[1] for r in signals[p]] for p in book.packs}
    cand = {p: [i for i in range(warm, n) if dirs[p][i] and confs[p][i] >= cfg["entry_conf"]]
            for p in book.packs}
    affected, lanes = [], 0
    cutoff = times[-1] + 60 - book.lookback * 60
    for st in book.by_idx:
        candidates = []
        holds = book.auto_holds(st)
        for hold in ((0,) + holds if holds else (None,)):
            lane = se.SetLane(st, args.symbol)
            if hold is not None:
                lane.hold = hold
            out = lane.scan(warm, n, bars, dirs[st.pack], confs[st.pack], True, cfg, cand[st.pack], acc)
            rows = [(times[r["exit_i"]] + 60, args.symbol, r["side"], r["pnl"], r["pnl_pct"],
                     r["hold_s"], r["reason"], st.id) for r in out]
            candidates.append((int(lane.hold), rows))
            lanes += 1
        merged = se.causal_hold_tape(candidates, book.hold_auto_max, book.hold_auto_min,
                                     se.HOLD_AUTO_MARGIN, sg.cost_as_frac(book.cost_pct), book.lookback * 60)
        events = sorted((r[0] - r[5], r[0]) for r in merged)
        latest_end = float("-inf")
        overlaps = []
        for entry, end in events:
            if entry < latest_end:
                overlaps.append((entry, end, latest_end))
            latest_end = max(latest_end, end)
        if overlaps:
            affected.append({"set": st.id, "selected": len(events), "overlap_count": len(overlaps),
                             "within_score_window": sum(end >= cutoff and prior_end >= cutoff
                                                        for _, end, prior_end in overlaps),
                             "first_example": list(overlaps[0])})
    market_file = Path(args.data) / f"{args.symbol}.json"
    result = {
        "symbol": args.symbol, "market_sha256": hashlib.sha256(market_file.read_bytes()).hexdigest(),
        "patch": patch, "sets": len(book.by_idx), "lanes": lanes, "bars": n,
        "first_bar": times[0], "last_bar": times[-1], "score_cutoff": cutoff,
        "affected_sets": len(affected), "overlapping_rows": sum(a["overlap_count"] for a in affected),
        "affected_sets_in_score_window": sum(a["within_score_window"] > 0 for a in affected),
        "overlapping_rows_in_score_window": sum(a["within_score_window"] for a in affected),
        "examples": affected[:5],
        "note": "One-symbol fixed-candidate paper lanes; counts do not verify pooled multi-symbol choices, "
                "actual positions, cooldown, omitted entries, or a corrected hold implementation.",
    }
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
