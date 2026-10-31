#!/usr/bin/env python3
"""Reconstruct stage sample sizes for Base observations in a saved forward audit."""
import argparse
import bisect
import collections
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PULSE = HERE.parent / "server" / "pulse"
sys.path.insert(0, str(PULSE))
import audit_causal_hold_forward as forward  # noqa: E402
import set_engine as se  # noqa: E402
import stage_eval as sg  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--preset", required=True)
    ap.add_argument("--audit", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    saved = json.loads(Path(args.audit).read_text())
    names, bars, times = sg.load_bars(args.data, len(saved["symbols"]))
    if names != saved["symbols"] or forward.fingerprint(args.data, names, Path(args.preset)) != saved["sha256"]:
        raise ValueError("audit inputs or source do not match")
    overlay = json.loads((PULSE / "overlay-bingx-x01.json").read_text())
    overlay.update(json.loads(Path(args.preset).read_text()))
    book = se.SetBook(); book.load(overlay)
    wanted = {r["set"] for r in saved["records"]}
    book.by_idx = [st for st in book.by_idx if st.id in wanted]
    book.sets = {st.id: st for st in book.by_idx}
    if set(book.sets) != wanted:
        raise ValueError("saved Set identities are not in this configuration")
    end = saved["end"]
    for sym in names:
        book.bars[sym], book.bar_t[sym] = bars[sym], times[sym][-1]
        out = {sid: [] for sid in book.sets}
        info = book._replay_symbol(sym, out, end)
        book._hist_rows[sym] = {sid: se.RowCols(rows) for sid, rows in out.items() if rows}
        book._hist_inputs[sym] = info
        print(f"candidate tapes: {sym}", flush=True)
    records = []
    for st in book.by_idx:
        source = book._tape_rows(st.id, names, float("-inf"), end)
        rows = book._choose_hold(st, source, names, float("-inf"), end)
        ts = [row[0] for row in rows]
        for record in (r for r in saved["records"] if r["set"] == st.id):
            now = record["t"]
            lo = bisect.bisect_left(ts, now - book.lookback * se.BAR_S)
            hi = bisect.bisect_left(ts, now)
            visible = rows[lo:hi]
            book.set_processed(st, visible)
            st.hist = [dict(zip(se._ROW_KEYS, row)) for row in visible]
            st.last_error = ""
            book._score_one(st, now=now)
            if abs(st.gate_pf - record["basePf"]) > 1e-9 or abs(st.main_pf - record["mainPf"]) > 1e-9:
                raise ValueError(f"saved stage PF does not reproduce: {st.id} at {now}")
            # Research-only comparison: reconstruct membership on all available
            # earlier closes, then score only the current rolling window. This
            # tests the boundary effect; it does not repair production history.
            prior = rows[:hi]
            pt = [row[0] for row in prior]
            pe = [row[0] - row[5] for row in prior]
            pn = [row[4] - se.cost_as_frac(book.cost_pct) for row in prior]
            params = book.stage_params(st.pack)
            full = se.stage_chain(pt, pe, pn, params)
            main_idx = full["main"]
            mt, me, mn = ([pt[k] for k in main_idx], [pe[k] for k in main_idx], [pn[k] for k in main_idx])
            ends, starts, passes = se._main_windows(mt, mn, me, params)
            cutoff = now - book.lookback * se.BAR_S
            selected, _ = se._main_short_choice(mn, ends, starts, params, passes,
                                                [t >= cutoff for t in mt], current_only=True)
            cm = [pn[k] for k in full["main"] if pt[k] >= cutoff]
            cr = [pn[k] for k in full["real"] if pt[k] >= cutoff]
            cq = [pn[k] for k in full["realneg"] if pt[k] >= cutoff]
            contextual = se.stage_verdicts(cm, cr, cq, selected, params)
            records.append({"t": now, "set": st.id, "baseN": st.gate_n, "basePf": st.gate_pf,
                            "mainN": st.main_n, "mainPf": st.main_pf, "mainPass": st.stage["main"],
                            "mainShortN": st.main_short_n, "mainShortPass": st.stage["short"],
                            "realN": st.exe_n, "realPf": st.exe_pf, "realPass": st.stage["real"],
                            "gateReason": book.gate_reason(st), "contextMainN": contextual["main_n"],
                            "contextMainPf": contextual["main_pf"], "contextMainPass": contextual["main"],
                            "contextRealN": contextual["real_n"], "contextRealPf": contextual["real_pf"],
                            "contextPosPass": contextual["pos"]})
    if len(records) != len(saved["records"]):
        raise ValueError("saved observations did not all reproduce")
    result = {"scope": saved["scope"], "mainRequired": book.main_n, "realRequired": book.realst_n,
              "observations": len(records), "reasons": dict(collections.Counter(r["gateReason"] for r in records)),
              "mainSampleCounts": dict(sorted(collections.Counter(str(r["mainN"]) for r in records).items(), key=lambda x: int(x[0]))),
              "contextMainSampleCounts": dict(sorted(collections.Counter(str(r["contextMainN"]) for r in records).items(), key=lambda x: int(x[0]))),
              "contextMainPasses": sum(r["contextMainPass"] for r in records),
              "contextPositivePasses": sum(r["contextPosPass"] for r in records),
              "records": records}
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "records"}), flush=True)


if __name__ == "__main__":
    main()
