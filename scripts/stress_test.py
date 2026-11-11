#!/usr/bin/env python3
"""Overall stress test of the Set engine: every pack on (indications, general, Short Range and the extra packs, with composite
relation names and 30m names in their variant lists), N symbols of stored 1m history, repeated full replay passes.

Per pass: wall time, the process's peak RSS, the Python heap traced now and at its peak, the sizes of the caches that live
across passes (the signal memo per symbol, the indicator memo, the relation compile cache, every Set's tapes and stage lanes),
and the figures a pass must keep stable (Sets, valid Sets, pooled PF after Base). A cache or tape that grows from pass to pass
with the same bars is a leak; the pass time after the first must fall (memo hits).

usage: stress_test.py --data DIR [--symbols 20] [--bars 1920] [--passes 4] [--out reports/stress_test.json]
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import resource
import sys
import time
import tracemalloc

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server", "pulse"))

COMPOSITES = ["rsirev_n8_t1@T15", "bbrev_n14_t5@N15@C", "donch_n21_t5@M30", "macdh_n8_t1@I@t5", "kelt_n8_t5@K", "adx_n14_t30", "trend_x10"]


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def sizes(book) -> dict:
    import indication_lib as lib
    import indication_rel as rel
    memo = getattr(lib._tls, "memo", {}) or {}
    return {"sig_cache_entries": sum(len(v) for v in book._sig_cache.values()),
            "indicator_memo": len(memo),
            "relation_compiled": len(rel._cache),
            "tape_rows": sum(len(st.hist or []) for st in book.by_idx),
            "stage_rows": sum(len(st.proc or []) + len(st.exe or []) + len(getattr(st, "neg", None) or []) for st in book.by_idx),
            "hist_rows": sum(sum(len(v) for v in (d or {}).values()) for d in book._hist_rows.values())}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--bars", type=int, default=1920)
    ap.add_argument("--passes", type=int, default=4)
    ap.add_argument("--out", default=os.path.join(HERE, "..", "reports", "stress_test.json"))
    a = ap.parse_args()
    from set_engine import SetBook
    ov = json.load(open(os.path.join(HERE, "..", "server", "pulse", "overlay-bingx-x02.json")))
    ov.update({"stratShortRange": True, "stratTrend": True, "stratBreakout": True, "stratReversion": True, "stratFlow": True,
               "stratMulti": True, "trendVariants": ["trend_c10", "trend_x10", "adx_n14_t30"], "breakoutVariants": COMPOSITES,
               "extraIndicators": ["rsirev_n8_t1@T15", "kelt_n8_t5"], "histLookbackBars": a.bars, "histLookbackAuto": False})
    files = sorted(f for f in os.listdir(a.data) if f.endswith(".json"))[: a.symbols]
    tracemalloc.start()
    book = SetBook()
    book.load(ov)
    for f in files:
        raw = json.load(open(os.path.join(a.data, f)))[-(a.bars + 1):-1]
        book.bars[f[:-5]] = [[float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in raw]
        book.bar_t[f[:-5]] = float(raw[-1][0]) / 1000.0
    now = float(raw[-1][0]) / 1000.0 + 60.0
    out = {"symbols": len(files), "bars": a.bars, "packs": list(book.packs), "sets": len(book.by_idx), "passes": []}
    print(f"symbols {len(files)} bars {a.bars} packs {book.packs} Sets {len(book.by_idx)}", flush=True)
    for k in range(a.passes):
        t0 = time.time()
        book.replay_all(now=now)
        dt = time.time() - t0
        gc.collect()
        cur, peak = tracemalloc.get_traced_memory()
        snap = book.snapshot()
        pb = snap.get("pfBase") or {}
        row = {"pass": k + 1, "seconds": round(dt, 1), "rss_peak_mb": round(rss_mb(), 1), "heap_mb": round(cur / 1e6, 1),
               "heap_peak_mb": round(peak / 1e6, 1), **sizes(book), "valid": pb.get("valid"), "pf_base": (pb.get("base") or {}).get("pf"),
               "validated_pct": pb.get("validatedPct"), "errors": sum(1 for st in book.by_idx if st.last_error)}
        out["passes"].append(row)
        print(json.dumps(row), flush=True)
    p = out["passes"]
    grow = {k: [r[k] for r in p] for k in ("sig_cache_entries", "indicator_memo", "tape_rows", "stage_rows", "hist_rows", "heap_mb")}
    out["stable"] = {k: (len(set(v[1:])) <= 1 if k != "heap_mb" else max(v[1:]) <= v[1] * 1.15 + 5) for k, v in grow.items()}
    out["same_result"] = len({(r["valid"], r["pf_base"]) for r in p}) == 1
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump(out, open(a.out, "w"), indent=1)
    print("stable across passes:", out["stable"], "same result every pass:", out["same_result"])
    return 0 if all(out["stable"].values()) and out["same_result"] else 1


if __name__ == "__main__":
    sys.exit(main())
