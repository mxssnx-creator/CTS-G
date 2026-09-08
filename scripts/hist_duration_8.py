#!/usr/bin/env python3
"""8-symbol historic duration + PF identity + progress monotonicity.

Times SetBook.replay_all across worker counts, then hist_calc 1h synth.
Picks the fastest worker count that keeps progress correct and last-15 PF
identical to last_n_cost_pf.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PULSE = os.path.join(ROOT, "server", "pulse")
sys.path.insert(0, PULSE)

from hist_calc import run_calc  # noqa: E402
from position_cost import last_n_cost_pf, is_positive_pf  # noqa: E402
from set_engine import SetBook, synth_trend, last_n_chrono  # noqa: E402

SYMBOLS = [
    "AAA-USDT", "BBB-USDT", "CCC-USDT", "DDD-USDT",
    "EEE-USDT", "FFF-USDT", "GGG-USDT", "HHH-USDT",
]
BARS = 180
OUT = os.path.join(ROOT, "reports", "hist-duration-8.json")


def _book() -> SetBook:
    book = SetBook()
    book.load({
        "histEnabled": True,
        "stratIndications": True,
        "stratGeneral": True,
        "stratTrailing": True,
        "stratBlock": True,
        "dcaEnabled": False,
        "slToTpRatios": [0.3, 0.6, 1.0],
        "setMinStep": 8,
        "setStepMax": 12,
        "histMinBars": 60,
        "histLookbackBars": BARS,
        "setMinPf": 1.10,
        "setMinSamples": 8,
    })
    for i, sym in enumerate(SYMBOLS):
        book.ingest_bars(sym, synth_trend(BARS, start=100.0 + i, step=0.08 + i * 0.01))
    return book


def _progress_ok(samples: List[Tuple[int, int, str]]) -> bool:
    if not samples:
        return False
    last = -1
    total = samples[-1][1]
    for done, tot, _phase in samples:
        if tot < total and total > 0:
            # total is allowed to stay fixed; shrinking is a bug
            if tot < 1:
                return False
        if done < last:
            return False
        last = done
    return samples[-1][0] >= min(8, samples[-1][1])


def _pf_identities(book: SetBook) -> Dict[str, Any]:
    checked = 0
    mismatch = 0
    validated = 0
    samples = []
    for st in book.by_idx:
        if int(st.last15_n or 0) < 8:
            continue
        checked += 1
        lib = last_n_cost_pf(last_n_chrono(st.hist, 15), 15, ordered=True)
        got = float(st.last15_ratio or 0)
        want = float(lib.get("ratio") or 0)
        if abs(got - want) > 1e-3:
            mismatch += 1
            if len(samples) < 4:
                samples.append({"id": st.id, "got": got, "lib": want, "n": st.last15_n})
        if is_positive_pf(got) and int(st.last15_n or 0) >= 8:
            validated += 1
    return {
        "checked": checked,
        "mismatch": mismatch,
        "validated": validated,
        "setCount": len(book.sets),
        "histFills": sum(s.n for s in book.sets.values()),
        "samples": samples,
        "ok": mismatch == 0 and checked > 0,
    }


def time_replay(workers: int) -> Dict[str, Any]:
    book = _book()
    samples: List[Tuple[int, int, str]] = []

    def on_step() -> None:
        p = book.progress
        samples.append((int(p.symbols_done or 0), int(p.symbols_total or 0), str(p.phase or "")))

    t0 = time.perf_counter()
    book.replay_all(symbols=list(SYMBOLS), workers=workers, merge=True, progress_total=8, on_step=on_step)
    wall = time.perf_counter() - t0
    pf = _pf_identities(book)
    return {
        "workers": workers,
        "wallS": round(wall, 3),
        "sets": len(book.sets),
        "ready": bool(book.progress.ready),
        "phase": book.progress.phase,
        "symbolsDone": int(book.progress.symbols_done or 0),
        "symbolsTotal": int(book.progress.symbols_total or 0),
        "progressSteps": len(samples),
        "progressMonotonic": _progress_ok(samples),
        "pf": pf,
        "ok": bool(book.progress.ready) and _progress_ok(samples) and pf["ok"],
    }


def time_hist_calc(workers: int) -> Dict[str, Any]:
    t0 = time.perf_counter()
    job = run_calc({
        "hours": 1,
        "symbols": list(SYMBOLS),
        "allSymbols": False,
        "allConfigs": True,
        "trailing": True,
        "stratBlock": True,
        "stratDca": False,
        "stratIndications": True,
        "stratGeneral": True,
        "workers": workers,
        "synth": True,
        "minStep": 8,
        "stepMax": 12,
        "slToTpRatios": [0.3, 0.6, 1.0],
    }, persist=False)
    wall = time.perf_counter() - t0
    cov = job.get("coverage") if isinstance(job.get("coverage"), dict) else {}
    sym_cov = cov.get("symbols") if isinstance(cov.get("symbols"), dict) else {}
    return {
        "workers": workers,
        "wallS": round(wall, 3),
        "phase": job.get("phase"),
        "error": job.get("error") or "",
        "sets": cov.get("setCount") or cov.get("product") or 0,
        "rows": job.get("rowCount") or 0,
        "validated": job.get("validatedCount") or 0,
        "replayMode": job.get("replayMode") or "",
        "symbolsCompleted": int(sym_cov.get("completed") or 0),
        "symbolsRequested": int(sym_cov.get("requested") or 0),
        "ok": (
            str(job.get("phase") or "") in ("ready", "done", "complete")
            and not job.get("error")
            and int(sym_cov.get("completed") or 0) >= 8
            and str(job.get("replayMode") or "") == "oneshot"
        ),
    }


def main() -> int:
    report: Dict[str, Any] = {"symbols": SYMBOLS, "bars": BARS, "replay": [], "histCalc": []}
    print("replay catalog...", flush=True)
    probe = _book()
    print(f"sets={len(probe.sets)} symbols=8 bars={BARS}", flush=True)
    best = None
    for w in (1, 2, 4, 8):
        row = time_replay(w)
        report["replay"].append(row)
        print(
            f"replay w={w} {row['wallS']}s ready={row['ready']} mono={row['progressMonotonic']} "
            f"pf={row['pf']['checked']} mismatch={row['pf']['mismatch']} fills={row['pf']['histFills']}",
            flush=True,
        )
        if row["ok"] and (best is None or row["wallS"] < best["wallS"]):
            best = {"kind": "replay", **row}
    print("hist_calc 1h synth...", flush=True)
    best_hist = None
    for w in (1, 2, 4, 8):
        try:
            row = time_hist_calc(w)
        except Exception as exc:
            row = {"workers": w, "ok": False, "error": str(exc)[:200], "wallS": None}
        report["histCalc"].append(row)
        print(
            f"hist_calc w={w} {row.get('wallS')}s phase={row.get('phase')} "
            f"mode={row.get('replayMode')} done={row.get('symbolsCompleted')}/{row.get('symbolsRequested')} "
            f"err={row.get('error')!s:.80}",
            flush=True,
        )
        if row.get("ok") and row.get("wallS") is not None:
            if best_hist is None or float(row["wallS"]) < float(best_hist.get("wallS") or 1e9):
                best_hist = {"kind": "histCalc", **row}
    report["bestReplay"] = best
    report["bestHistCalc"] = best_hist
    report["policy"] = {
        "replayDefault": "serial on <=2 CPU; min(cpu, n) otherwise — no extra 4/8 cap",
        "histCalcDefault": "oneshot for n<=32; workers min(cpu, n) even on fat catalogs",
        "blasThreads": 1,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(report, f, indent=2)
    print("wrote", OUT, flush=True)
    replay_ok = all(r["ok"] for r in report["replay"])
    hist_ok = all(r.get("ok") for r in report["histCalc"]) if report["histCalc"] else False
    return 0 if replay_ok and hist_ok and best else 1


if __name__ == "__main__":
    raise SystemExit(main())
