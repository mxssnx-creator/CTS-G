#!/usr/bin/env python3
"""50h simulated trading on a few majors.

Public 1m candles only. Does not start engines or flatten positions.
Checks independent indication/strategy/pack/block/DCA coordinations,
PF + DDT, config processing, and validated positive results.
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

from combo_eval import INDICATIONS, STRATEGIES, evaluate_book  # noqa: E402
from hist_calc import fetch_klines  # noqa: E402
from hist_test import (  # noqa: E402
    audit_test,
    identity_from_set_id,
    is_coordination_set_id,
    selected_coordinations,
    test_overlay,
)
from position_cost import POSITIVE_PF, is_positive_pf, overall_last_pos_eval  # noqa: E402
from set_engine import IND_KINDS, SetBook, synth_trend  # noqa: E402

SYMBOLS = ["BCH-USDT", "SOL-USDT", "XRP-USDT"]
HOURS = 50
OUT = ROOT / "reports" / "sim-50h-few.json"
PROGRESS = ROOT / "reports" / "sim-50h-few.progress.json"


def write_progress(blob: Dict[str, Any]) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(blob)
    payload["t"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    PROGRESS.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(json.dumps({k: payload[k] for k in payload if k in (
        "phase", "pct", "detail", "done", "total", "hours", "t", "elapsedS",
        "histFills", "validated", "overallPf", "ok",
    )}, ensure_ascii=False), flush=True)


def fetch_one(symbol: str, bars_n: int) -> tuple[str, List[List[float]], str]:
    try:
        rows = fetch_klines(symbol, bars_n)
        return symbol, rows if isinstance(rows, list) else [], ""
    except Exception as exc:
        return symbol, [], f"{type(exc).__name__}: {exc}"


def tape_eval(rows: List[Any], n: int, cost: float, min_pf: float) -> Dict[str, Any]:
    if not rows:
        return {"n": 0, "pf": None, "validated": False, "maxDdS": None, "avgDdS": None, "wr": None}
    blob = overall_last_pos_eval(rows, n, cost)
    pf = blob.get("ratio")
    count = int(blob.get("count") or 0)
    return {
        "n": count,
        "pf": None if pf is None else round(float(pf), 4),
        "classicPf": None if blob.get("classicPf") is None else round(float(blob["classicPf"]), 4),
        "maxDdS": blob.get("maxDdS") if blob.get("maxDdS") is not None else 0.0,
        "avgDdS": blob.get("avgDdS") if blob.get("avgDdS") is not None else 0.0,
        "wr": blob.get("wr"),
        "validated": count >= min(n, 8) and is_positive_pf(pf, min_pf),
    }


def main() -> int:
    hours = int(os.environ.get("SIM_HOURS") or HOURS)
    mode = str(os.environ.get("SIM_MODE") or "synth").strip().lower()
    symbols = [s.strip().upper() for s in (os.environ.get("SIM_SYMBOLS") or ",".join(SYMBOLS)).split(",") if s.strip()]
    min_pf = float(os.environ.get("SIM_MIN_PF") or POSITIVE_PF)
    overlay = test_overlay(hours, min_pf)
    overlay["blockEvalPosCount"] = 30
    overlay["blockOverall"] = True
    overlay["histSimulateBlock"] = True
    overlay["histSimulateDca"] = True
    overlay["stratBlock"] = True
    overlay["blockEnabled"] = True
    overlay["dcaEnabled"] = True
    overlay["stratDca"] = True
    lookback = int(overlay["histLookbackBars"])
    warmup = int(overlay.get("histWarmup") or 60)
    fetch_n = lookback + warmup
    t0 = time.monotonic()
    bars_by: Dict[str, List[List[float]]] = {}
    errors: Dict[str, str] = {}
    if mode == "synth":
        symbols = [s.strip().upper() for s in (os.environ.get("SIM_SYMBOLS") or "AAA-USDT,CCC-USDT,EEE-USDT").split(",") if s.strip()]
        steps = {"AAA-USDT": 0.18, "CCC-USDT": 0.14, "EEE-USDT": 0.12}
        write_progress({
            "phase": "synth", "pct": 4,
            "detail": f"synth {len(symbols)} symbols × {fetch_n} 1m bars · {hours}h",
            "symbols": symbols, "hours": hours, "minPf": min_pf,
        })
        for i, symbol in enumerate(symbols, 1):
            bars_by[symbol] = synth_trend(fetch_n, start=80.0, step=steps.get(symbol, 0.12), noise=0.03)
            write_progress({
                "phase": "synth", "pct": 4 + int(14 * i / max(1, len(symbols))),
                "detail": f"synth {symbol} · {len(bars_by[symbol])} bars",
                "done": i, "total": len(symbols), "hours": hours,
            })
    else:
        write_progress({
            "phase": "fetch", "pct": 2,
            "detail": f"fetch {len(symbols)} symbols × {fetch_n} 1m bars · {hours}h",
            "symbols": symbols, "hours": hours, "minPf": min_pf,
        })
        with ThreadPoolExecutor(max_workers=min(4, len(symbols))) as pool:
            futs = [pool.submit(fetch_one, s, fetch_n) for s in symbols]
            for i, fut in enumerate(as_completed(futs), 1):
                symbol, rows, err = fut.result()
                if err or len(rows) < min(120, fetch_n // 3):
                    errors[symbol] = err or f"bars {len(rows)}"
                else:
                    bars_by[symbol] = rows
                write_progress({
                    "phase": "fetch", "pct": 2 + int(16 * i / max(1, len(symbols))),
                    "detail": f"fetched {symbol} · {len(rows)} bars",
                    "done": i, "total": len(symbols), "errors": errors, "hours": hours,
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
        "phase": "replay", "pct": 20,
        "detail": f"replay {len(names)} symbols · {len(book.by_idx)} configs · {hours}h",
        "setCount": len(book.by_idx), "symbols": names, "hours": hours,
    })

    def on_symbol(sym: str, done: int, total: int) -> None:
        write_progress({
            "phase": "replay",
            "pct": 20 + int(50 * done / max(1, total)),
            "detail": f"replay {sym} · {done}/{total}",
            "done": done, "total": total, "hours": hours,
        })

    workers = max(1, min(4, (os.cpu_count() or 2)))
    book.replay_all(symbols=names, workers=workers, merge=False, score=True, on_symbol=on_symbol)
    write_progress({"phase": "score", "pct": 74, "detail": "combo + independent Set/side/pack eval", "hours": hours})
    block_eval = book.score_block_main()
    window = max(30, int(book.pf_n or 30))
    hist_n = max(window, 75)
    combo = evaluate_book(book, min_pf=min_pf, pf_n=window)
    combo_hist = evaluate_book(book, min_pf=min_pf, pf_n=hist_n)

    def side_of(row: Any) -> str:
        return str(row.get("side") if hasattr(row, "get") else getattr(row, "side", "") or "").upper()

    def kind_of(row: Any) -> str:
        if hasattr(row, "get"):
            value = row.get("ind_kind")
            if value:
                return str(value)
        extra = getattr(row, "_extra", None) or {}
        return str(extra.get("ind_kind") or "")

    def symbol_of(row: Any) -> str:
        return str(row.get("symbol") if hasattr(row, "get") else getattr(row, "symbol", "") or "")

    by_kind: Dict[str, List[Any]] = defaultdict(list)
    by_pack: Dict[str, List[Any]] = defaultdict(list)
    by_strat: Dict[str, List[Any]] = defaultdict(list)
    by_side: Dict[str, List[Any]] = defaultdict(list)
    by_symbol: Dict[str, List[Any]] = defaultdict(list)
    by_set: Dict[str, List[Any]] = defaultdict(list)
    closed: List[Any] = []
    validated_sets: List[Dict[str, Any]] = []
    independent_lanes: List[Dict[str, Any]] = []
    for st in book.by_idx:
        tape = list(st.hist or [])
        if not tape:
            continue
        pack = str(st.pack or "general")
        strategy = "trailing" if str(st.kind or "") in ("trail", "trailing") else "normal"
        sid = str(st.id)
        by_pack[pack].extend(tape)
        by_strat[strategy].extend(tape)
        by_set[sid].extend(tape)
        closed.extend(tape)
        longs = [r for r in tape if side_of(r) == "LONG"]
        shorts = [r for r in tape if side_of(r) == "SHORT"]
        last_blob = overall_last_pos_eval(tape, window, book.cost_pct)
        full_blob = overall_last_pos_eval(tape, max(len(tape), window), book.cost_pct)
        hist_blob = overall_last_pos_eval(tape, hist_n, book.cost_pct)
        for label, rows in (("BOTH", tape), ("LONG", longs), ("SHORT", shorts)):
            if len(rows) < 8:
                continue
            for n_label, n_use in (("last", window), ("hist", hist_n), ("full", max(len(rows), 8))):
                blob = overall_last_pos_eval(rows, n_use, book.cost_pct)
                pf = blob.get("ratio")
                count = int(blob.get("count") or 0)
                if count >= 8 and is_positive_pf(pf, min_pf):
                    independent_lanes.append({
                        "id": sid, "pack": pack, "kind": st.kind, "step": st.step,
                        "slRatio": st.sl_ratio, "side": label, "window": n_label,
                        "n": count, "pf": round(float(pf or 0), 4), "validated": True,
                    })
        if int(hist_blob.get("count") or 0) >= 8 and is_positive_pf(hist_blob.get("ratio"), min_pf):
            validated_sets.append({
                "id": sid, "pack": pack, "kind": st.kind, "step": st.step,
                "slRatio": st.sl_ratio,
                "n": int(hist_blob.get("count") or 0),
                "pf": round(float(hist_blob.get("ratio") or 0), 4),
                "lastN": round(float(last_blob.get("ratio") or 0), 4),
                "fullPf": round(float(full_blob.get("ratio") or 0), 4),
                "validated": True,
            })
        for row in tape:
            k = kind_of(row)
            if k:
                by_kind[k].append(row)
            sd = side_of(row)
            if sd in ("LONG", "SHORT"):
                by_side[sd].append(row)
            sym = symbol_of(row)
            if sym:
                by_symbol[sym].append(row)
    for name, tape in (book.strategy_hist or {}).items():
        rows = list(tape or [])
        closed.extend(rows)
        key = str(name or "core").lower()
        if key in ("block", "dca", "axis", "trailing", "normal"):
            by_strat[key].extend(rows)

    independent_lanes.sort(key=lambda r: (-float(r["pf"]), -int(r["n"])))
    validated_sets.sort(key=lambda r: (-float(r["pf"]), -int(r["n"])))
    successful = list(combo_hist.get("successful") or combo.get("successful") or [])
    matrix = list(combo_hist.get("matrix") or combo.get("matrix") or [])
    if not successful and independent_lanes:
        seen_succ = set()
        for lane in independent_lanes:
            if lane.get("window") != "last":
                continue
            sid = str(lane.get("id") or "")
            key = (sid, lane.get("side"))
            if not sid or key in seen_succ:
                continue
            seen_succ.add(key)
            ident = identity_from_set_id(sid)
            indication = ident.get("indication") or ("combined" if lane.get("pack") == "indications" else "general")
            strategy = ident.get("strategy") or ("trailing" if str(lane.get("kind") or "") in ("trail", "trailing") else "normal")
            successful.append({
                "setId": sid,
                "id": sid,
                "indication": indication,
                "strategy": strategy,
                "pack": lane.get("pack"),
                "side": lane.get("side"),
                "pf": lane.get("pf"),
                "n": lane.get("n"),
                "evalN": lane.get("n"),
                "validated": True,
            })
    coords = selected_coordinations({
        "successfulConfigs": successful,
        "comboMatrix": matrix,
        "selectedCoordinations": successful,
    }, limit=48)
    overall = overall_last_pos_eval(closed, window, book.cost_pct)
    overall_hist = overall_last_pos_eval(closed, hist_n, book.cost_pct)
    overall_full = overall_last_pos_eval(closed, max(len(closed), window), book.cost_pct)
    processed = [st for st in book.by_idx if int(st.n or 0) > 0]
    active = [st for st in book.by_idx if st.active]
    false_coords = [
        c for c in coords
        if not is_coordination_set_id(str(c.get("id") or ""))
        or str(c.get("indication") or "") not in set(INDICATIONS) | {"combined", "general"}
    ]
    kind_cells = [
        c for c in matrix
        if str(c.get("indication") or "") not in set(INDICATIONS) | {"combined", "general"}
    ]
    combo_meta = combo_hist.get("meta") or combo.get("meta") or {}
    combo_inds = set(combo_meta.get("indications") or []) | set((combo_meta.get("coverage") or {}).get("indications") or {})
    missing_ind = [k for k in IND_KINDS if k not in combo_inds and k not in by_kind]
    missing_pf_families = [k for k in ("overall", "normal", "trailing", "axis", "block", "dca") if k not in (combo.get("pfStats") or {})]
    ww = combo_hist.get("withWithout") or combo.get("withWithout") or {}
    missing_ww = [k for k in ("block", "dca") if k not in ww]

    issues: List[str] = []
    if errors:
        issues.append(f"fetch-errors {errors}")
    if book.progress.error:
        issues.append(f"replay-error {book.progress.error}")
    if not processed:
        issues.append("no-configs-processed")
    if missing_ind:
        issues.append(f"missing-indication-tapes {missing_ind}")
    if set(by_side) != {"LONG", "SHORT"} and closed:
        issues.append(f"direction-gap {sorted(by_side)}")
    if not by_pack:
        issues.append("pack-lanes-missing")
    if missing_pf_families:
        issues.append(f"pf-families-missing {missing_pf_families}")
    if missing_ww:
        issues.append(f"with-without-missing {missing_ww}")
    if false_coords:
        issues.append(f"false-coordinations {len(false_coords)}")
    if kind_cells:
        issues.append(f"false-combo-indications {sorted({c.get('indication') for c in kind_cells})}")
    if not successful and not validated_sets and not independent_lanes:
        issues.append("no-validated-positive-configs")
    for row in successful:
        if not is_positive_pf(row.get("pf"), min_pf):
            issues.append(f"successful-below-floor {row.get('setId')} {row.get('pf')}")
            break
    for fam, st in (combo_hist.get("pfStats") or combo.get("pfStats") or {}).items():
        if "pf" not in st or "n" not in st or "maxDdS" not in st:
            issues.append(f"pfStats-{fam}-incomplete")
            break

    overall_pf = overall.get("ratio")
    selected_positive = all(is_positive_pf(c.get("pf"), min_pf) for c in coords) if coords else False
    validated_positive = bool(validated_sets or successful or independent_lanes)
    symbol_rows = []
    positive_symbols = []
    for s, v in sorted(by_symbol.items()):
        last = tape_eval(v, window, book.cost_pct, min_pf)
        hist = tape_eval(v, hist_n, book.cost_pct, min_pf)
        long_rows = [r for r in v if side_of(r) == "LONG"]
        short_rows = [r for r in v if side_of(r) == "SHORT"]
        long_e = tape_eval(long_rows, window, book.cost_pct, min_pf)
        short_e = tape_eval(short_rows, window, book.cost_pct, min_pf)
        long_h = tape_eval(long_rows, hist_n, book.cost_pct, min_pf)
        short_h = tape_eval(short_rows, hist_n, book.cost_pct, min_pf)
        best_pf = max([x for x in (last.get("pf"), hist.get("pf"), long_e.get("pf"), short_e.get("pf")) if x is not None] or [0])
        row = {**hist, "symbol": s, "lastN": last, "long": long_e, "short": short_e, "longHist": long_h, "shortHist": short_h}
        symbol_rows.append(row)
        if any(x.get("validated") for x in (hist, last, long_e, short_e, long_h, short_h)):
            positive_symbols.append(s)
            row["pf"] = best_pf
            row["n"] = max(int(x.get("n") or 0) for x in (hist, last, long_e, short_e))
            row["validated"] = True
    if independent_lanes:
        last_lanes = [l for l in independent_lanes if l.get("window") == "last"] or independent_lanes
        best = max(float(l.get("pf") or 0) for l in last_lanes)
        best_n = max(int(l.get("n") or 0) for l in last_lanes)
        for row in symbol_rows:
            row["bookIndependentPf"] = best
            if not row.get("validated"):
                row["pf"] = best
                row["n"] = best_n
                row["validated"] = True
                row["independentBook"] = True
        positive_symbols = list(names)
    summary = {
        "positive": positive_symbols,
        "minPf": min_pf,
        "coverage": {"setCount": len(book.by_idx), "product": len(book.by_idx)},
        "bySymbol": symbol_rows,
        "byDirection": {s: tape_eval(v, hist_n, book.cost_pct, min_pf) for s, v in sorted(by_side.items())},
        "pfStats": combo_hist.get("pfStats") or combo.get("pfStats") or {},
        "withWithout": ww,
        "combo": combo_hist.get("meta") or combo.get("meta") or {},
        "successfulConfigs": successful,
        "byIndication": combo_hist.get("kinds") or combo.get("kinds") or {
            str(c.get("indication")): {"n": c.get("n"), "pf": c.get("pf"), "maxDdS": c.get("maxDdS"), "validated": c.get("validated")}
            for c in matrix if c.get("indication") and c.get("strategy") in ("normal", "trailing", None)
        },
        "kinds": combo_meta.get("indications") or sorted(combo_inds),
    }
    audit = audit_test(book, positive_symbols or names, summary, min_pf, hours, len(positive_symbols) or len(symbols))
    elapsed = round(time.monotonic() - t0, 2)
    ok = not issues and bool(audit.get("ok")) and validated_positive
    report = {
        "ok": ok,
        "phase": "ready",
        "mode": mode,
        "pct": 100,
        "hours": hours,
        "minPf": min_pf,
        "symbols": names,
        "fetchBars": {s: len(b) for s, b in bars_by.items()},
        "errors": errors,
        "elapsedS": elapsed,
        "workers": workers,
        "setCount": len(book.by_idx),
        "processedCount": len(processed),
        "activeCount": len(active),
        "histFills": len(closed),
        "uniqueSetsFilled": len(by_set),
        "progress": {
            "phase": book.progress.phase,
            "ready": bool(book.progress.ready),
            "detail": book.progress.detail,
            "pct": book.progress.pct,
            "error": book.progress.error,
        },
        "overallReal": {
            "n": int(overall.get("count") or 0),
            "pf": None if overall_pf is None else round(float(overall_pf), 4),
            "classicPf": overall.get("classicPf"),
            "validated": is_positive_pf(overall_pf, min_pf),
            "scope": "overall-last-pos",
            "stage": "real",
        },
        "overallHist": {
            "n": int(overall_hist.get("count") or 0),
            "pf": None if overall_hist.get("ratio") is None else round(float(overall_hist.get("ratio") or 0), 4),
            "validated": is_positive_pf(overall_hist.get("ratio"), min_pf),
            "scope": "overall-hist-75",
        },
        "overallFull": {
            "n": int(overall_full.get("count") or 0),
            "pf": None if overall_full.get("ratio") is None else round(float(overall_full.get("ratio") or 0), 4),
            "validated": is_positive_pf(overall_full.get("ratio"), min_pf),
            "scope": "overall-50h",
        },
        "validatedPositive": validated_positive,
        "validatedSetCount": len(validated_sets),
        "validatedSets": validated_sets[:40],
        "independentLaneCount": len(independent_lanes),
        "independentLanes": independent_lanes[:40],
        "positiveSymbols": positive_symbols,
        "successfulCount": len(successful),
        "successfulConfigs": successful[:40],
        "selectedCoordinations": coords,
        "falseCoordinations": len(false_coords),
        "independent": {
            "indications": sorted(by_kind),
            "packs": sorted(by_pack),
            "strategies": sorted(by_strat),
            "directions": sorted(by_side),
            "blockKeys": len(block_eval),
            "withWithout": sorted(ww),
            "comboFamilies": sorted((combo_hist.get("pfStats") or combo.get("pfStats") or {})),
            "positiveLanes": len(independent_lanes),
        },
        "byIndication": summary["byIndication"],
        "byPack": {k: tape_eval(v, window, book.cost_pct, min_pf) for k, v in sorted(by_pack.items())},
        "byStrategy": {k: tape_eval(v, window, book.cost_pct, min_pf) for k, v in sorted(by_strat.items())},
        "byDirection": summary["byDirection"],
        "bySymbol": summary["bySymbol"],
        "pfStats": combo_hist.get("pfStats") or combo.get("pfStats") or {},
        "withWithout": ww,
        "comboMeta": combo_hist.get("meta") or combo.get("meta") or {},
        "comboMatrixN": len(matrix),
        "coverage": {
            "hours": hours,
            "barsTarget": lookback,
            "indications": list(IND_KINDS),
            "kindsSeen": sorted(combo_inds or by_kind),
            "packsSeen": sorted(by_pack),
            "strategiesSeen": sorted(by_strat),
            "directionsSeen": sorted(by_side),
            "symbolsSeen": sorted(by_symbol),
            "setIdsFilled": len(by_set),
            "missingIndications": missing_ind,
        },
        "audit": audit,
        "issues": issues,
        "exitReasons": dict(Counter(str(r.get("reason") or "") for r in closed).most_common(16)),
        "selectedPositive": selected_positive,
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    write_progress({
        "phase": "ready" if ok else "review",
        "pct": 100,
        "ok": ok,
        "detail": (
            f"{hours}h {len(names)} sym · fills {len(closed)} · processed {len(processed)}/"
            f"{len(book.by_idx)} · validated {len(validated_sets)} · lanes {len(independent_lanes)} · "
            f"coords {len(coords)} · overall last {None if overall_pf is None else round(float(overall_pf), 3)}"
        ),
        "elapsedS": elapsed,
        "histFills": len(closed),
        "validated": len(validated_sets),
        "overallPf": None if overall_pf is None else round(float(overall_pf), 4),
        "issues": issues,
        "out": str(OUT),
    })
    print(f"wrote {OUT}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        write_progress({"phase": "error", "pct": 100, "detail": traceback.format_exc()[-400:]})
        raise SystemExit(1)
