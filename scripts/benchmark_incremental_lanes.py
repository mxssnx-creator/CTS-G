#!/usr/bin/env python3
"""Research-only check of SetLane.scan in chunks and across a one-bar rotation.

This benchmarks lane state with precomputed signals. It does not implement or
validate incremental production history, stage scoring, cache invalidation,
multiple symbols, live orders or restart recovery.
"""
from __future__ import annotations

import argparse
import collections
import copy
import json
import os
import resource
import time
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
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(json.loads(args.patch))
    book = sg.make_book()
    names, all_bars, all_times = sg.load_bars(args.data, args.symbols)
    if args.symbol not in names or args.bars < 400:
        raise ValueError("symbol missing from loaded data or fewer than 400 bars requested")
    bars, times = all_bars[args.symbol][-args.bars:], all_times[args.symbol][-args.bars:]
    n = len(bars)
    if n < 400 or any(b - a != 60 for a, b in zip(times, times[1:])):
        raise ValueError("at least 400 contiguous one-minute candles are required")
    warm = min(book.warmup, max(16, n // 5))
    cfg = sg.lane_cfg(book)
    started = time.perf_counter()
    signals = se.pack_signals(bars, book.packs, book.ind_settings, times[0] + 60, warm)
    acc = se.BarIndex(bars)
    dirs = {p: [row[0] for row in signals[p]] for p in book.packs}
    confs = {p: [row[1] for row in signals[p]] for p in book.packs}
    cand = {p: [i for i in range(warm, n) if dirs[p][i] and confs[p][i] >= cfg["entry_conf"]] for p in book.packs}
    signal_seconds = time.perf_counter() - started

    full, chunked, rotated = {}, {}, []
    started = time.perf_counter()
    for st in book.by_idx:
        for hold in (0,) + book.auto_holds(st):
            lane = se.SetLane(st, args.symbol)
            lane.hold = hold
            rows = lane.scan(warm, n, bars, dirs[st.pack], confs[st.pack], True, cfg, cand[st.pack], acc)
            full[(st.id, hold)] = (rows, copy.deepcopy(lane.open), lane.cool)
    full_seconds = time.perf_counter() - started

    cuts = [n - 300, n - 290, n - 251, n - 250, n - 100, n - 25, n - 2, n - 1, n]
    started = time.perf_counter()
    for st in book.by_idx:
        for hold in (0,) + book.auto_holds(st):
            lane = se.SetLane(st, args.symbol)
            lane.hold = hold
            rows, start = [], warm
            for end in cuts:
                rows.extend(lane.scan(start, end, bars, dirs[st.pack], confs[st.pack], True, cfg, cand[st.pack], acc))
                start = end
            chunked[(st.id, hold)] = (rows, copy.deepcopy(lane.open), lane.cool)
    chunk_seconds = time.perf_counter() - started

    shifted_bars = bars[1:]
    shifted_acc = se.BarIndex(shifted_bars)
    shifted_dirs = {p: v[1:] for p, v in dirs.items()}
    shifted_confs = {p: v[1:] for p, v in confs.items()}
    shifted_cand = {p: [i - 1 for i in v if i >= 1] for p, v in cand.items()}
    for st in book.by_idx:
        for hold in (0,) + book.auto_holds(st):
            lane = se.SetLane(st, args.symbol)
            lane.hold = hold
            prior = lane.scan(warm, n - 1, bars, dirs[st.pack], confs[st.pack], True, cfg, cand[st.pack], acc)
            if lane.open is not None:
                lane.open["i"] -= 1
            last = lane.scan(n - 2, n - 1, shifted_bars, shifted_dirs[st.pack], shifted_confs[st.pack], True, cfg, shifted_cand[st.pack], shifted_acc)
            actual = [{**x, "entry_i": x["entry_i"] - 1, "exit_i": x["exit_i"] - 1} for x in prior] + last
            expected_rows, expected_open, expected_cool = full[(st.id, hold)]
            expected_rows = [{**x, "entry_i": x["entry_i"] - 1, "exit_i": x["exit_i"] - 1} for x in expected_rows]
            expected_open = {**expected_open, "i": expected_open["i"] - 1} if expected_open is not None else None
            if (actual, lane.open, lane.cool) != (expected_rows, expected_open, expected_cool):
                rotated.append([st.id, hold])
    unequal = [[key[0], key[1]] for key in full if full[key] != chunked[key]]
    directions = collections.Counter(row["side"] for rows, _open, _cool in full.values() for row in rows)
    exits = collections.Counter(row["reason"] for rows, _open, _cool in full.values() for row in rows)
    result = {"symbol": args.symbol, "sets": len(book.by_idx), "lanes": len(full), "bars": n,
              "signal_build_seconds": signal_seconds, "full_scan_seconds": full_seconds,
              "chunked_scan_seconds": chunk_seconds, "chunk_mismatches": unequal,
              "one_bar_rotation_mismatches": rotated, "directions": dict(directions), "exits": dict(exits),
              "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              "note": "Precomputed signals; excludes stage scoring, revision detection, full runtime memory, "
                      "independent hold splicing, account orders and restart recovery."}
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v if k not in ("chunk_mismatches", "one_bar_rotation_mismatches") else len(v) for k, v in result.items()}))
    return 1 if unequal or rotated else 0


if __name__ == "__main__":
    raise SystemExit(main())
