#!/usr/bin/env python3
"""Continuous paper study of returns after Base/Main/Real checkpoints.

The candidate-hold tapes are counterfactual evidence only. The scored tape is
replayed through a single position per Set/symbol with past-only hold choices.
Stage membership uses available older entry-time context, but incomplete feeds
are reported rather than treated as full context. This is not an exchange
execution simulator or a sealed holdout.
"""
import argparse
import bisect
import hashlib
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PULSE = HERE.parent / "server" / "pulse"
sys.path.insert(0, str(PULSE))
import set_engine as se  # noqa: E402
import stage_eval as sg  # noqa: E402
import main_stage_audit as oracle  # noqa: E402


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fingerprint(data, names, preset):
    files = [HERE / "audit_causal_hold_forward.py", HERE / "stage_eval.py", HERE / "main_stage_audit.py",
             PULSE / "overlay-bingx-x01.json", preset]
    files += sorted(PULSE.glob("*.py"))
    files += [Path(data) / f"{name}.json" for name in names]
    return {str(p.relative_to(HERE.parent)) if p.is_relative_to(HERE.parent) else p.name: sha(p) for p in files}


def metric(rows, n):
    if len(rows) < n:
        return None
    vals = rows[:n]
    gains = sum(v for v in vals if v > 0)
    losses = -sum(v for v in vals if v < 0)
    return {"returns": vals, "gain": gains, "loss": losses}


def independent_visible_chain(ts, entries, nets, params, cutoff):
    """Plain full-context reconstruction, then restrict today's vote/window."""
    full = oracle.naive_chain(ts, entries, nets, params)
    main = full["main"]
    short_n = params.main_n
    if params.main_on and params.short_on:
        def eligible_results(window):
            selected = []
            for i in main:
                if ts[i] < cutoff:
                    continue
                prior = [nets[j] for j in main
                         if entries[i] - params.lookback_s <= ts[j] < entries[i]][-window:]
                need = window if params.full_window else 1
                if len(prior) >= need and round(oracle._pf(prior), 4) + 1e-9 >= params.floor_for(window):
                    selected.append(nets[i])
            return selected

        reference = oracle._pf(eligible_results(params.main_n))
        best = reference
        for window in params.short_windows:
            values = eligible_results(int(window))
            pf = oracle._pf(values)
            if (len(values) >= params.short_min and pf > best + se.SHORT_EPS
                    and pf >= reference * params.short_margin - se.SHORT_EPS):
                best, short_n = pf, int(window)
    keep = [j for j, i in enumerate(main) if ts[i] >= cutoff]
    return {"main": [main[j] for j in keep],
            "real": [i for i in full["real"] if ts[i] >= cutoff],
            "realneg": [i for i in full["realneg"] if ts[i] >= cutoff],
            "short_n": short_n,
            "short_by_order": [full["short_by_order"][j] for j in keep],
            "main_context": main}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--preset", required=True)
    ap.add_argument("--start", required=True, type=int)
    ap.add_argument("--hours", type=int, default=12)
    ap.add_argument("--symbols", type=int, default=8)
    ap.add_argument("--max-sets", type=int, default=0, help="optional bounded smoke-test subset; 0 studies the full grid")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    names, bars, times = sg.load_bars(args.data, args.symbols)
    if len(names) != args.symbols:
        raise ValueError("requested symbols unavailable")
    for sym in names:
        if any(y - x != 60 for x, y in zip(times[sym], times[sym][1:])):
            raise ValueError(f"noncontiguous bars: {sym}")
        if times[sym][-1] != times[names[0]][-1]:
            raise ValueError("symbol end times differ")
    inputs = fingerprint(args.data, names, Path(args.preset))
    overlay = json.loads((PULSE / "overlay-bingx-x01.json").read_text())
    overlay.update(json.loads(Path(args.preset).read_text()))
    book = se.SetBook(); book.load(overlay)
    if args.max_sets > 0:
        book.by_idx = book.by_idx[:args.max_sets]
        book.sets = {st.id: st for st in book.by_idx}
    if not book.hold_auto:
        raise ValueError("this study requires the preset's automatic holds")
    end = times[names[0]][-1] + se.BAR_S
    checkpoints = [args.start + i * 3600 for i in range(args.hours)]
    if checkpoints[0] < times[names[0]][0] + book.lookback * se.BAR_S or checkpoints[-1] >= end:
        raise ValueError("requested checkpoints lack prehistory or forward data")
    started = time.monotonic()
    for sym in names:
        # Keep the full archived prehistory and forward period for the offline
        # walk; hold-choice windows are still limited to book.lookback.
        book.bars[sym] = bars[sym]
        book.bar_t[sym] = times[sym][-1]
        out = {sid: [] for sid in book.sets}
        info = book._replay_symbol(sym, out, end)
        book._hist_rows[sym] = {sid: se.RowCols(rows) for sid, rows in out.items() if rows}
        book._hist_inputs[sym] = info
        print(f"candidate tapes: {sym}", flush=True)
    records = []
    dynamic_rows = oracle_checked = 0
    for i, st in enumerate(book.by_idx):
        source = book._tape_rows(st.id, names, float("-inf"), end)
        dynamic = book._choose_hold(st, source, names, float("-inf"), end)
        previous = {}
        for row in dynamic:
            entry = row[0] - row[5]
            if row[1] in previous and entry <= previous[row[1]]:
                raise RuntimeError(f"overlapping dynamic lane: {st.id} / {row[1]}")
            previous[row[1]] = row[0]
        dynamic_rows += len(dynamic)
        times_closed = [row[0] for row in dynamic]
        for checkpoint in checkpoints:
            # Strictly earlier closes: no same-clock evidence for the decision.
            cutoff = checkpoint - book.lookback * se.BAR_S
            lo = bisect.bisect_left(times_closed, cutoff)
            context_lo = bisect.bisect_left(times_closed, checkpoint - book.history_bars() * se.BAR_S)
            hi = bisect.bisect_left(times_closed, checkpoint)
            visible = dynamic[lo:hi]
            context = dynamic[context_lo:hi]
            book.set_processed(st, context, now=checkpoint)
            st.hist = [dict(zip(se._ROW_KEYS, row)) for row in visible[-max(40, book.gate_window):]]
            st.last_error = ""
            book._score_one(st, now=checkpoint)
            if book.base_validation_reason(st):
                continue
            ts = [row[0] for row in context]
            entries = [row[0] - row[5] for row in context]
            nets = [row[4] - se.cost_as_frac(book.cost_pct) for row in context]
            params = book.stage_params(st.pack)
            expected = independent_visible_chain(ts, entries, nets, params, cutoff)
            actual = se.stage_chain(ts, entries, nets, params, window_start=cutoff)
            if expected != actual or (st.proc_total, st.exe_total, st.neg_total) != tuple(len(expected[k]) for k in ("main", "real", "realneg")):
                raise RuntimeError(f"stage chain mismatch: {st.id} at {checkpoint}")
            oracle_checked += 1
            cohorts = ["base"]
            if book._main_pf_pass(st):
                cohorts.append("main")
            if st.stage.get("pos"):
                cohorts.append("real")
            future = sorted((row for row in dynamic if row[0] - row[5] >= checkpoint),
                            key=lambda row: (row[0] - row[5], row[1], row[0]))
            values = [float(row[4]) - se.cost_as_frac(book.cost_pct) for row in future]
            records.append({"t": checkpoint, "set": st.id, "pack": st.pack,
                            "basePf": st.gate_pf, "mainPf": st.main_pf,
                            "realPf": st.exe_pf, "cohorts": cohorts,
                            "next8": metric(values, 8), "next15": metric(values, 15)})
        if (i + 1) % 100 == 0:
            print(f"Sets studied: {i + 1}/{len(book.by_idx)}", flush=True)
    summary = {}
    for cohort in ("base", "main", "real"):
        selected = [r for r in records if cohort in r["cohorts"]]
        item = {"selectedSetCheckpoints": len(selected)}
        for n in (8, 15):
            mature = [r[f"next{n}"] for r in selected if r[f"next{n}"] is not None]
            gain = sum(r["gain"] for r in mature)
            loss = sum(r["loss"] for r in mature)
            item[f"next{n}"] = {"maturedSetCheckpoints": len(mature), "returns": len(mature) * n,
                                 "gain": gain, "loss": loss,
                                 "pf": gain / loss if loss else None,
                                 "noLoss": bool(gain > 0 and not loss)}
        summary[cohort] = item
    if inputs != fingerprint(args.data, names, Path(args.preset)):
        raise RuntimeError("inputs changed during the study")
    missing_context = [t for t in checkpoints if any(times[sym][0] > t - book.history_bars() * se.BAR_S for sym in names)]
    result = {"scope": "continuous pooled Set/symbol paper lanes; historical checkpoints; available entry-time context; not venue execution or sealed holdout",
              "timingLimitation": "The study reselects holds at each paper entry; the live engine refreshes choices periodically. Decisions exclude closes at the checkpoint, while the current runtime scorer includes them. No live refresh schedule, fills, spread or venue latency are reproduced.",
              "symbols": names, "sets": len(book.by_idx), "costPct": book.cost_pct,
              "lookbackBars": book.lookback, "contextBars": book.history_bars(),
              "checkpointsWithShortContext": missing_context, "start": checkpoints[0], "end": end,
              "elapsedSeconds": round(time.monotonic() - started, 3), "sha256": inputs,
              "dynamicRowsCheckedForOverlap": dynamic_rows, "overlappingRows": 0,
              "baseStageChainsMatchedOracle": oracle_checked,
              "summary": summary, "records": records}
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
