#!/usr/bin/env python3
"""24h simulated trading on 5 symbols. Complete Block/Real/Overall calcs.

Public candles only. Does not start Live/mainnet or flatten positions.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "pulse"))

from hist_calc import fetch_klines  # noqa: E402
from hist_test import HOURS_DEFAULT, test_overlay  # noqa: E402
from position_cost import POSITIVE_PF, is_positive_pf, overall_last_pos_eval  # noqa: E402
from set_engine import IND_KINDS, SetBook  # noqa: E402

SYMBOLS = ["XRP-USDT", "BCH-USDT", "SOL-USDT", "BTC-USDT", "ETH-USDT"]
HOURS = 24
OUT = ROOT / "reports" / "sim-24h-5sym.json"
PROGRESS = ROOT / "reports" / "sim-24h-5sym.progress.json"


def write_progress(blob: Dict[str, Any]) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(blob)
    payload["t"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    PROGRESS.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def fetch_one(symbol: str, bars_n: int) -> tuple[str, List[List[float]], str]:
    try:
        rows = fetch_klines(symbol, bars_n)
        return symbol, rows if isinstance(rows, list) else [], ""
    except Exception as exc:
        return symbol, [], f"{type(exc).__name__}: {exc}"


def serialize_block(eval_map: Dict[tuple, Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for key, blob in eval_map.items():
        rows.append({
            "count": key[0],
            "indication": key[1],
            "setId": key[2],
            **{k: v for k, v in blob.items() if k != "realOverall"},
            "realOverall": blob.get("realOverall") or {},
        })
    rows.sort(key=lambda r: (int(r.get("count") or 0), str(r.get("indication") or ""), str(r.get("setId") or "")))
    return rows


def main() -> int:
    hours = int(os.environ.get("SIM_HOURS") or HOURS)
    symbols = [s.strip().upper() for s in (os.environ.get("SIM_SYMBOLS") or ",".join(SYMBOLS)).split(",") if s.strip()]
    min_pf = float(os.environ.get("SIM_MIN_PF") or POSITIVE_PF)
    overlay = test_overlay(hours, min_pf)
    overlay["blockEvalPosCount"] = 50
    overlay["blockOverall"] = True
    overlay["histSimulateBlock"] = True
    overlay["stratBlock"] = True
    lookback = int(overlay["histLookbackBars"])
    warmup = int(overlay.get("histWarmup") or 60)
    fetch_n = lookback + warmup
    t0 = time.monotonic()
    write_progress({
        "phase": "fetch", "pct": 2, "detail": f"fetch {len(symbols)} symbols × {fetch_n} 1m bars",
        "symbols": symbols, "hours": hours,
    })
    bars_by: Dict[str, List[List[float]]] = {}
    errors: Dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(5, len(symbols))) as pool:
        futs = [pool.submit(fetch_one, s, fetch_n) for s in symbols]
        for i, fut in enumerate(as_completed(futs), 1):
            symbol, rows, err = fut.result()
            if err or len(rows) < min(80, fetch_n // 3):
                errors[symbol] = err or f"bars {len(rows)}"
            else:
                bars_by[symbol] = rows
            write_progress({
                "phase": "fetch", "pct": 2 + int(18 * i / max(1, len(symbols))),
                "detail": f"fetched {symbol} · {len(rows)} bars",
                "done": i, "total": len(symbols), "errors": errors,
            })
    if not bars_by:
        write_progress({"phase": "error", "pct": 100, "detail": "no candles", "errors": errors})
        return 2

    book = SetBook()
    book.load(overlay)
    for symbol, rows in bars_by.items():
        book.ingest_bars(symbol, rows)
    names = list(bars_by)
    write_progress({
        "phase": "replay", "pct": 22,
        "detail": f"replay {len(names)} symbols · {len(book.by_idx)} sets · Block ON",
        "setCount": len(book.by_idx), "symbols": names,
    })

    def on_symbol(sym: str, done: int, total: int) -> None:
        write_progress({
            "phase": "replay",
            "pct": 22 + int(58 * done / max(1, total)),
            "detail": f"replay {sym} · {done}/{total}",
            "done": done, "total": total,
        })

    workers = max(1, min(4, (os.cpu_count() or 2)))
    book.replay_all(symbols=names, workers=workers, merge=False, score=True, on_symbol=on_symbol)
    write_progress({"phase": "score", "pct": 86, "detail": "Block Real/Overall independent eval"})
    block_eval = book.score_block_main()
    snap = book.snapshot(full=True)
    closed: List[Dict[str, Any]] = []
    for st in book.by_idx:
        closed.extend(list(st.hist or []))
    for tape in (book.strategy_hist or {}).values():
        closed.extend(list(tape or []))
    overall = overall_last_pos_eval(closed, max(30, int(book.pf_n or 30)), book.cost_pct)
    by_kind = defaultdict(list)
    by_pack = defaultdict(list)
    by_strat = defaultdict(list)
    for row in closed:
        kind = str(row.get("ind_kind") or "")
        if kind:
            by_kind[kind].append(row)
        by_pack[str(row.get("pack") or row.get("strategy") or "core")].append(row)
        by_strat[str(row.get("strategy") or "core")].append(row)

    def tape_pf(rows: List[Any], n: int = 50) -> Dict[str, Any]:
        if not rows:
            return {"n": 0, "pf": None, "validated": False}
        blob = overall_last_pos_eval(rows, n, book.cost_pct)
        pf = blob.get("ratio")
        count = int(blob.get("count") or 0)
        return {
            "n": count,
            "pf": None if pf is None else round(float(pf), 4),
            "validated": count >= n and is_positive_pf(pf),
            "liveOk": count < n or (is_positive_pf(pf) and float(pf or 0) + 1e-9 >= min_pf),
        }

    block_rows = serialize_block(block_eval)
    independent_ok = True
    reasons = []
    seen_keys = {(r["count"], r["indication"], r["setId"]) for r in block_rows}
    for kind in IND_KINDS:
        keys = [k for k in seen_keys if k[1] == kind]
        if keys and any(k[1] != kind for k in keys):
            independent_ok = False
            reasons.append(f"kind-mix {kind}")
    intern_only = [r for r in block_rows if r.get("internOnly")]
    live_ok = [r for r in block_rows if r.get("liveOk") and not r.get("internOnly")]
    elapsed = round(time.monotonic() - t0, 2)
    report = {
        "ok": not book.progress.error,
        "phase": "ready",
        "pct": 100,
        "hours": hours,
        "symbols": names,
        "fetchBars": {s: len(b) for s, b in bars_by.items()},
        "errors": errors,
        "elapsedS": elapsed,
        "workers": workers,
        "setCount": len(book.by_idx),
        "activeCount": sum(1 for st in book.by_idx if st.active),
        "histFills": sum(int(st.n or 0) for st in book.by_idx),
        "blockFills": len(list((book.strategy_hist or {}).get("block") or [])),
        "progress": {
            "phase": book.progress.phase,
            "ready": bool(book.progress.ready),
            "detail": book.progress.detail,
            "pct": book.progress.pct,
            "error": book.progress.error,
        },
        "overallReal": {
            "n": int(overall.get("count") or 0),
            "pf": overall.get("ratio"),
            "classicPf": overall.get("classicPf"),
            "scope": "overall-last-pos",
            "stage": "real",
        },
        "byIndication": {k: tape_pf(v) for k, v in sorted(by_kind.items())},
        "byPack": {k: tape_pf(v) for k, v in sorted(by_pack.items())},
        "byStrategy": {k: tape_pf(v) for k, v in sorted(by_strat.items())},
        "block": {
            "evalPosCount": int(book.block_eval_pos or 50),
            "independent": independent_ok,
            "reasons": reasons,
            "keys": len(block_rows),
            "liveOk": len(live_ok),
            "internOnly": len(intern_only),
            "insufficient": sum(1 for r in block_rows if r.get("reason") == "insufficient-sample"),
            "rows": block_rows[:80],
        },
        "coverage": {
            "indications": list(IND_KINDS),
            "kindsSeen": sorted({r.get("indication") for r in block_rows if r.get("indication")}),
            "countsSeen": sorted({int(r.get("count") or 0) for r in block_rows}),
            "setIds": sorted({r.get("setId") for r in block_rows if r.get("setId")})[:40],
        },
        "snapshotBlock": (snap.get("blockMainEval") or {}),
        "exitReasons": dict(Counter(str(r.get("reason") or "") for r in closed).most_common(16)),
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    write_progress({
        "phase": "ready", "pct": 100,
        "detail": f"done {elapsed}s · fills {report['histFills']} · block keys {len(block_rows)} · overall PF {overall.get('ratio')}",
        "elapsedS": elapsed, "out": str(OUT),
        "independent": independent_ok, "internOnly": len(intern_only),
    })
    print(f"wrote {OUT}", flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        write_progress({"phase": "error", "pct": 100, "detail": traceback.format_exc()[-400:]})
        raise SystemExit(1)
