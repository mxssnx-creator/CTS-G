#!/usr/bin/env python3
"""Complete coverage + PF identity audit for historic calc (8 symbols)."""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "pulse"))

from hist_calc import run_calc  # noqa: E402
from position_cost import EVALUATION_WINDOWS, last_n_cost_pf, evaluation_windows  # noqa: E402
from set_engine import SetBook, last_n_chrono  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "reports", "coverage-audit.json")
SYMBOLS = ["AAA-USDT", "BBB-USDT", "CCC-USDT", "DDD-USDT", "EEE-USDT", "FFF-USDT", "GGG-USDT", "HHH-USDT"]


def rec(rows: List[Dict[str, Any]], name: str, ok: bool, detail: Any = "") -> None:
    rows.append({"name": name, "ok": bool(ok), "detail": detail})


def main() -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    t0 = time.perf_counter()
    job = run_calc(
        {
            "synth": True,
            "hours": 1,
            "minStep": 8,
            "stepMax": 10,
            "trailing": True,
            "stratBlock": True,
            "stratDca": True,
            "stratIndications": True,
            "stratGeneral": True,
            "allConfigs": True,
            "symbols": list(SYMBOLS),
        },
        persist=False,
    )
    wall = round(time.perf_counter() - t0, 3)
    cov = job.get("coverage") or {}
    listings = job.get("listings") or {}
    relative = listings.get("relative") or {}
    product = int(cov.get("product") or 0)
    rec(rows, "ready", job.get("phase") == "ready" and not job.get("error"), job.get("error") or job.get("phase"))
    rec(rows, "replay-oneshot", job.get("replayMode") == "oneshot", job.get("replayMode"))
    rec(rows, "symbols-complete", float((cov.get("symbols") or {}).get("coveragePct") or 0) == 100.0, cov.get("symbols"))
    rec(rows, "sets-complete", float((cov.get("sets") or {}).get("coveragePct") or 0) == 100.0, cov.get("sets"))
    rec(rows, "evaluations-complete", float((cov.get("evaluations") or {}).get("coveragePct") or 0) == 100.0, cov.get("evaluations"))
    rec(rows, "tasks-complete", float((cov.get("tasks") or {}).get("coveragePct") or 0) == 100.0, cov.get("tasks"))
    rec(rows, "bars-complete", float((cov.get("bars") or {}).get("coveragePct") or 0) == 100.0, cov.get("bars"))
    rec(rows, "product-indexed", product >= 20 and bool(cov.get("indexed")), product)
    rec(rows, "sl-tp-cover", bool(cov.get("slTpCover")) and bool(cov.get("trailSlTpCover")), {k: cov.get(k) for k in ("slTpCover", "trailSlTpCover", "slCover", "trailCover")})
    rec(rows, "independent-axes", all(bool(cov.get(k)) for k in ("independentConfigs", "independentDirection", "independentStrategy", "independentSlTp")), {k: cov.get(k) for k in ("independentConfigs", "independentDirection", "independentStrategy", "independentIndication")})
    rec(rows, "listings-ranked-complete", len(listings.get("rankedIds") or []) == product, {"ranked": len(listings.get("rankedIds") or []), "product": product})
    rec(rows, "listings-index-complete", len(listings.get("indexById") or {}) == product, len(listings.get("indexById") or {}))
    rec(rows, "listings-relative", int(relative.get("sets") or 0) == product and relative.get("packs") and relative.get("kinds"), relative)
    rec(rows, "by-direction", set((job.get("byDirection") or {}).keys()) == {"LONG", "SHORT"}, list((job.get("byDirection") or {}).keys()))
    rec(rows, "by-strategy-packs", "general" in (job.get("byStrategy") or {}) and "indications" in (job.get("byStrategy") or {}), sorted((job.get("byStrategy") or {}).keys())[:12])
    rec(rows, "by-symbol", len(job.get("bySymbol") or []) >= len(SYMBOLS), len(job.get("bySymbol") or []))
    rec(rows, "winner", bool(job.get("winner")) and "last15Ratio" in (job.get("winner") or {}), (job.get("winner") or {}).get("id"))
    rec(rows, "kinds", len(job.get("kinds") or {}) >= 6, sorted((job.get("kinds") or {}).keys()))

    win_ok = 0
    win_n = 0
    pf_ok = 0
    pf_n = 0
    for r in job.get("rows") or []:
        ew = r.get("evaluationWindows") or {}
        if not ew:
            continue
        win_n += 1
        if set(ew) >= {"last5", "last15"}:
            win_ok += 1
        last15 = (ew.get("last15") or {}).get("pf")
        if last15 is None:
            continue
        pf_n += 1
        if abs(float(r.get("last15Ratio") or 0) - float(last15)) < 1e-3:
            pf_ok += 1
    rec(rows, "row-windows", win_n > 0 and win_ok == win_n, {"ok": win_ok, "n": win_n})
    rec(rows, "row-last15-identity", pf_n > 0 and pf_ok == pf_n, {"ok": pf_ok, "n": pf_n})

    # Independent book replay: every set last15 matches last_n_cost_pf(hist).
    book = SetBook()
    book.load({
        "histEnabled": True,
        "stratGeneral": True,
        "stratIndications": True,
        "stratTrailing": True,
        "stratBlock": True,
        "stratDca": True,
        "slToTpRatios": [0.6, 1.0],
        "setMinStep": 8,
        "setStepMax": 10,
    })
    from set_engine import synth_trend
    names = list(SYMBOLS)
    for i, sym in enumerate(names):
        book.ingest_bars(sym, synth_trend(180, start=100.0 + i, step=0.08 + i * 0.01))
    book.replay_all(symbols=names, workers=1, merge=True, score=True)
    mismatch = 0
    checked = 0
    for st in book.by_idx:
        lib = last_n_cost_pf(st.hist, book.pf_n, book.cost_pct, ordered=True)
        checked += 1
        if abs(float(st.last15_ratio or 0) - float(lib.get("ratio") or 0)) > 1e-6:
            mismatch += 1
            if mismatch <= 4:
                rec(rows, f"pf-mismatch-{st.id}", False, {"set": st.last15_ratio, "lib": lib.get("ratio"), "n": st.last15_n})
    rec(rows, "book-pf-identity", mismatch == 0 and checked == len(book.by_idx), {"checked": checked, "mismatch": mismatch, "sets": len(book.by_idx), "fills": sum(s.n for s in book.by_idx)})
    rec(rows, "book-listings", len(book.relative_listings().get("rankedIds") or []) == len(book.by_idx), book.relative_listings().get("relative"))
    rec(rows, "book-progress-coverage", "coverage" in (book.progress.detail or "") and "0/" not in (book.progress.detail or "").split("active")[0], book.progress.detail)
    rec(rows, "book-cover-replay", int((book.coverage() or {}).get("replaySymbols") or 0) == len(names), book.coverage().get("replaySymbols"))

    failed = [r for r in rows if not r["ok"]]
    out = {
        "wallS": wall,
        "pass": len(rows) - len(failed),
        "fail": len(failed),
        "failed": [r["name"] for r in failed],
        "job": {
            "phase": job.get("phase"),
            "replayMode": job.get("replayMode"),
            "workers": job.get("workers"),
            "rowCount": job.get("rowCount"),
            "validatedCount": job.get("validatedCount"),
            "product": product,
            "relative": relative,
            "symbols": cov.get("symbols"),
            "sets": cov.get("sets"),
            "tasks": cov.get("tasks"),
        },
        "rows": rows,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    print(json.dumps({"pass": out["pass"], "fail": out["fail"], "failed": out["failed"], "wallS": wall, "product": product, "detail": book.progress.detail}, indent=2))
    return out


if __name__ == "__main__":
    result = main()
    sys.exit(0 if result["fail"] == 0 else 1)
