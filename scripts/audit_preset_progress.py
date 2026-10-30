#!/usr/bin/env python3
"""Re-score frozen completed-bar snapshots with the actual SetBook replay.

This is historical validation only, not a forward execution simulator. Each
snapshot starts with its own bounded history and runs the configured hold
selection. Sets and snapshots overlap, so their counts are not independent.
"""
import argparse
import bisect
import collections
import hashlib
import json
import os
import resource
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PULSE = os.path.join(HERE, "..", "server", "pulse")
sys.path.insert(0, PULSE)
from set_engine import SetBook, stage_chain  # noqa: E402
from position_cost import cost_as_frac  # noqa: E402
import stage_eval as sg  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--preset", required=True)
    ap.add_argument("--start", type=int, required=True, help="first UTC checkpoint in seconds")
    ap.add_argument("--hours", type=int, default=12)
    ap.add_argument("--symbols", type=int, default=8)
    ap.add_argument("--audit-chains", type=int, default=0, help="compare this many Base-passing Sets with the plain stage oracle")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    with open(args.preset, "rb") as f:
        preset_bytes = f.read()
    overlay = json.load(open(os.path.join(PULSE, "overlay-bingx-x01.json"), encoding="utf-8"))
    overlay.update(json.loads(preset_bytes))
    names, bars, times = sg.load_bars(args.data, args.symbols)
    if len(names) != args.symbols:
        raise ValueError("requested number of market files is unavailable")
    results = []
    for hour in range(args.hours):
        now = args.start + hour * 3600
        book = SetBook(); book.load(overlay)
        for symbol in names:
            end = bisect.bisect_left(times[symbol], now)
            first = end - book.lookback
            if first < 0 or end == 0 or times[symbol][end - 1] != now - 60:
                raise ValueError(f"insufficient completed 1m candles for {symbol} at {now}")
            window_t = times[symbol][first:end]
            if any(b - a != 60 for a, b in zip(window_t, window_t[1:])):
                raise ValueError(f"noncontiguous candles for {symbol} at {now}")
            book.ingest_bars(symbol, bars[symbol][first:end], last_t=window_t[-1])
        before = time.monotonic()
        book.replay_all(now=now, expected_symbols=names)
        if not book.progress.ready or book.progress.errors or book.progress.symbols_done != len(names):
            raise RuntimeError(f"incomplete replay: {book.progress}")
        reasons = collections.Counter(book.base_validation_reason(st) or "pass" for st in book.by_idx)
        readiness = collections.Counter(book.gate_reason(st) or "pass" for st in book.by_idx)
        passing = [st for st in book.by_idx if not book.base_validation_reason(st)]
        gain = sum(st.gate_gp for st in passing); loss = sum(st.gate_gl for st in passing)
        main_passing = [st for st in passing if book._main_pf_pass(st)]
        groups = {}
        for pack in book.packs:
            group = [st for st in book.by_idx if st.pack == pack]
            groups[pack] = {"sets": len(group), "basePassed": sum(not book.base_validation_reason(st) for st in group),
                            "atLeast12": sum(st.gate_n >= 12 for st in group),
                            "atLeast25": sum(st.gate_n >= 25 for st in group)}
        row = {"t": now, "sets": len(book.by_idx), "symbols": len(names), "baseReasons": dict(reasons),
               "entryReasons": dict(readiness), "basePassed": len(passing),
               "basePf": gain / loss if loss else None, "baseNoLoss": bool(gain > 0 and not loss),
               "baseOrders": sum(st.gate_n for st in passing),
               "atLeast12": sum(st.gate_n >= 12 for st in book.by_idx),
               "atLeast25": sum(st.gate_n >= 25 for st in book.by_idx),
               "baseLongPassed": sum(st.base_long_ok for st in book.by_idx),
               "baseShortPassed": sum(st.base_short_ok for st in book.by_idx),
               "autoHoldSelected": sum(st.hold_auto and st.hold_bars > 0 for st in book.by_idx),
               "mainPassingWithFullRealWindow": sum(st.exe_n >= book.realst_n for st in main_passing),
               "mainPassingRealWindowSamples": {str(n): sum(st.exe_n == n for st in main_passing)
                                                for n in sorted({st.exe_n for st in main_passing})},
               "stageCounts": book.stage_counts(), "byPack": groups,
               "elapsedSeconds": round(time.monotonic() - before, 3)}
        if args.audit_chains:
            import main_stage_audit as oracle
            checked, differences = 0, []
            cutoff = now - book.lookback * 60
            order = sorted(book._hist_rows)
            for st in passing[:args.audit_chains]:
                full = book._tape_rows(st.id, order, cutoff, until=now)
                full = book._choose_hold(st, full, order, cutoff, until=now)
                ts = [r[0] for r in full]
                entries = [r[0] - r[5] for r in full]
                nets = [r[4] - cost_as_frac(book.cost_pct) for r in full]
                params = book.stage_params(st.pack)
                expected = oracle.naive_chain(ts, entries, nets, params)
                actual = stage_chain(ts, entries, nets, params)
                counts_match = (st.proc_total, st.exe_total, st.neg_total) == tuple(len(expected[k]) for k in ("main", "real", "realneg"))
                real_values = [nets[i] for i in expected["real"]][-book.realst_n:]
                gain = sum(x for x in real_values if x > 0)
                loss = -sum(x for x in real_values if x < 0)
                real_pf = round(gain / loss, 4) if loss else (99.0 if gain else 0.0)
                real_match = len(real_values) == st.exe_n and abs(real_pf - st.exe_pf) < 1e-9
                if expected != actual or not counts_match or not real_match:
                    differences.append({"id": st.id, "chain": expected == actual,
                                        "counts": counts_match, "realWindow": real_match})
                checked += 1
            row["independentChainAudit"] = {"sets": checked, "differences": differences,
                                             "scope": "same pooled Set tape; not per-symbol-side or venue parity"}
        results.append(row)
        print(json.dumps(row), flush=True)
    result = {"presetSha256": hashlib.sha256(preset_bytes).hexdigest(), "symbols": names,
              "lookbackBars": book.lookback, "holdAuto": book.hold_auto,
              "costPct": book.cost_pct, "checkpoints": results,
              "maxRssKiB": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              "scope": "historical SetBook snapshots only; not modeled execution or exchange fills"}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
