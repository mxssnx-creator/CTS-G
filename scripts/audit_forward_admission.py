#!/usr/bin/env python3
"""Compare fixed-checkpoint shadow returns with causal per-entry stage admission.

This is an offline diagnostic of the ordinary CTS-G packs, not CTS-A-O Signals,
a portfolio simulator, venue execution, or an untouched holdout. It preserves
the original selected checkpoint identities and the complete candidate tape.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PULSE = HERE.parent / "server" / "pulse"
sys.path.insert(0, str(PULSE))
import set_engine as se  # noqa: E402
import stage_eval as sg  # noqa: E402


def positive_decisions(times, entries, nets, params):
    """Indices accepted by Base, Base+Main, and Base+Main+Real at each entry.

    Paper Main/Real membership is built on all prior candidate closes, not on
    future outcomes. The optional Negative lane is intentionally unsupported.
    """
    if params.neg_on:
        raise ValueError("positive-only diagnostic requires Negative lane disabled")
    if not (len(times) == len(entries) == len(nets)) or any(a > b for a, b in zip(times, times[1:])):
        raise ValueError("tape columns must align in exit-time order")
    if any(not math.isfinite(x) for col in (times, entries, nets) for x in col):
        raise ValueError("nonfinite evidence cannot qualify")
    if any(entry > end for entry, end in zip(entries, times)):
        raise ValueError("an exit cannot precede its entry")
    chain = se.stage_chain(times, entries, nets, params)
    base = set(chain["main"])
    main = set(chain["real"])
    if not params.real_on:
        return {"base": base, "main": main, "real": set(main)}
    real_indices = chain["real"]
    real_times = [times[i] for i in real_indices]
    allowed = set()
    for i in real_indices:
        entry = entries[i]
        end = bisect.bisect_left(real_times, entry)
        start = max(bisect.bisect_left(real_times, entry - params.lookback_s), end - params.real_n)
        sample = [nets[real_indices[j]] for j in range(start, end)]
        required = params.real_n if params.full_window else 1
        if len(sample) < required:
            continue
        gp = sum(value for value in sample if value > 0)
        gl = -sum(value for value in sample if value < 0)
        pf = round(se.pf_of(gp, gl), 4)
        if pf + 1e-9 >= params.floor_for(params.real_n):
            allowed.add(i)
    return {"base": base, "main": main, "real": allowed}


def stats(values):
    gain = sum(v for v in values if v > 0)
    loss = -sum(v for v in values if v < 0)
    return {"n": len(values), "wins": sum(v > 0 for v in values), "losses": sum(v < 0 for v in values),
            "gain": gain, "loss": loss, "pf": gain / loss if loss else None,
            "pfState": "ratio" if loss else "no-loss" if gain else "empty" if not values else "flat"}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--preset", required=True)
    ap.add_argument("--reference", required=True, help="saved fixed-checkpoint study")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    reference = json.loads(Path(args.reference).read_text())
    names, bars, times = sg.load_bars(args.data, len(reference["symbols"]))
    if names != reference["symbols"]:
        raise ValueError("symbol identities differ from the reference")
    for symbol in names:
        if sha(Path(args.data) / f"{symbol}.json") != reference["sha256"][f"{symbol}.json"]:
            raise ValueError(f"market input differs: {symbol}")
    if sha(args.preset) != reference["sha256"][Path(args.preset).name]:
        raise ValueError("preset differs from the reference")
    overlay = json.loads((PULSE / "overlay-bingx-x01.json").read_text())
    overlay.update(json.loads(Path(args.preset).read_text()))
    book = se.SetBook()
    book.load(overlay)
    records = [r for r in reference["records"] if "base" in r["cohorts"]]
    wanted = {r["set"] for r in records}
    book.by_idx = [st for st in book.by_idx if st.id in wanted]
    book.sets = {st.id: st for st in book.by_idx}
    if set(book.sets) != wanted:
        raise ValueError("selected Set identities differ from the reference")
    end = reference["end"]
    for symbol in names:
        book.bars[symbol], book.bar_t[symbol] = bars[symbol], times[symbol][-1]
        rows = {sid: [] for sid in book.sets}
        info = book._replay_symbol(symbol, rows, end)
        book._hist_rows[symbol] = {sid: se.RowCols(vals) for sid, vals in rows.items() if vals}
        book._hist_inputs[symbol] = info
        print(f"candidate tapes: {symbol}", flush=True)
    output = []
    for st in book.by_idx:
        source = book._tape_rows(st.id, names, float("-inf"), end)
        tape = book._choose_hold(st, source, names, float("-inf"), end)
        ts = [r[0] for r in tape]
        entries = [r[0] - r[5] for r in tape]
        nets = [r[4] - se.cost_as_frac(book.cost_pct) for r in tape]
        decision = positive_decisions(ts, entries, nets, book.stage_params(st.pack))
        for original in (r for r in records if r["set"] == st.id):
            checkpoint = original["t"]
            lo = bisect.bisect_left(ts, checkpoint - book.lookback * se.BAR_S)
            context_lo = bisect.bisect_left(ts, checkpoint - book.history_bars() * se.BAR_S)
            hi = bisect.bisect_left(ts, checkpoint)
            book.set_processed(st, tape[context_lo:hi], now=checkpoint)
            st.hist = [dict(zip(se._ROW_KEYS, row)) for row in tape[lo:hi][-max(40, book.gate_window):]]
            st.last_error = ""
            book._score_one(st, now=checkpoint)
            reason = book.base_validation_reason(st)
            future = sorted((i for i, entry in enumerate(entries) if entry >= checkpoint),
                            key=lambda i: (entries[i], tape[i][1], ts[i]))
            unfiltered = [nets[i] for i in future[:8]]
            old = (original.get("next8") or {}).get("returns")
            same = old is not None and len(old) == len(unfiltered) and all(abs(a - b) < 1e-12 for a, b in zip(old, unfiltered))
            counts = {stage: sum(i in decision[stage] for i in future) for stage in ("base", "main", "real")}
            next8 = {stage: [nets[i] for i in future if i in decision[stage]][:8]
                     for stage in ("base", "main", "real")}
            output.append({"t": checkpoint, "set": st.id, "pack": st.pack, "referenceBasePf": original["basePf"],
                           "currentBasePf": st.gate_pf, "currentBaseReason": reason, "rawNext8Matches": same,
                           "futureCandidateCount": len(future), "stageAcceptedCounts": counts,
                           "rawNext8": unfiltered, "stageNext8": next8})
    if len(output) != len(records):
        raise ValueError("not every selected checkpoint was reconstructed")
    summary = {}
    for stage in ("raw", "base", "main", "real"):
        cohorts = [(r["rawNext8"] if stage == "raw" else r["stageNext8"][stage]) for r in output]
        mature = [v for v in cohorts if len(v) == 8]
        summary[stage] = {"maturedCheckpoints": len(mature),
                          "completeNext8": stats([x for v in mature for x in v]),
                          "availableAcrossAllCheckpoints": stats([x for v in cohorts for x in v])}
    result = {"scope": "fixed selected ordinary CTS-G Set/checkpoints; overlapping shadow candidates; not CTS-A-O Signals, a funded portfolio, venue fills or an unseen holdout",
              "method": "Each future candidate is rechecked against causal Base, Main and Real shadow histories at its entry. Missing complete next-8 cohorts remain unavailable. The shadow lane can suppress opportunities even when an unqualified trade would not actually open; this is not an executable portfolio replay.",
              "symbols": names, "costPct": book.cost_pct, "reference": str(Path(args.reference).name),
              "referenceSha256": sha(args.reference), "selectedCheckpoints": len(output),
              "changedSelection": sum(bool(r["currentBaseReason"]) for r in output),
              "changedRawNext8": sum(not r["rawNext8Matches"] for r in output),
              "summary": summary, "records": output}
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "records"}), flush=True)


if __name__ == "__main__":
    main()
