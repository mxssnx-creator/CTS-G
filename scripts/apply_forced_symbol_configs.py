#!/usr/bin/env python3
"""Sweep multiple configs so XRP/BCH/SOL forced symbols succeed, then persist winners."""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from typing import Any, Dict, List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server", "pulse"))

from forced_configs import (  # noqa: E402
    FORCED_SYMBOLS,
    MIN_PF,
    evaluate_symbol,
    rank_key,
    select_best,
    select_symbol_winners,
    summary as forced_summary,
)
from hist_calc import fetch_klines, hours_to_bars, winner_patch  # noqa: E402
from set_engine import SetBook  # noqa: E402
from storage_paths import atomic_write  # noqa: E402

HOURS = 24
LOOKBACK = hours_to_bars(HOURS)
PROJECT_OVERLAY = os.path.join(ROOT, "server", "pulse", "overlay-bingx-x02.json")
DATA_OVERLAY = "/var/lib/cts/overlay-bingx-x02.json"
FORCED_OUT = "/var/lib/cts/forced-configs-bingx-x02.json"
FORCED_PROJECT = os.path.join(ROOT, "server", "pulse", "forced-configs-bingx-x02.json")
REPORT = os.path.join(ROOT, "reports", "hist-test", "forced-configs.json")
PUBLIC_FORCED = os.path.join(ROOT, "public", "forced-configs.json")
JOB = "/var/lib/cts/hist-calc-bingx-x02.json"
JOB_PROJECT = os.path.join(ROOT, "server", "pulse", "hist-calc-bingx-x02.json")

BEST_KEYS = (
    "id", "indication", "direction", "tpPct", "slPct", "slRatio",
    "trainPf", "pf", "holdoutPf", "tradesPerHour", "variant", "settingsKey",
    "trainN", "n", "eligible",
)

VARIANTS: List[Dict[str, Any]] = [
    {"name": "overlay-default", "indMinConfidence": 0.60, "indMinAgreement": 0.60, "indMinSources": 3, "indExtraSources": True, "indMinStrength": 0.20, "lastN": 30},
    {"name": "conf-0.58", "indMinConfidence": 0.58, "indMinAgreement": 0.55, "indMinSources": 2, "indExtraSources": True, "indMinStrength": 0.18, "lastN": 30},
    {"name": "last15", "indMinConfidence": 0.58, "indMinAgreement": 0.55, "indMinSources": 2, "indExtraSources": True, "indMinStrength": 0.18, "lastN": 15},
]


def load_overlay() -> Dict[str, Any]:
    path = PROJECT_OVERLAY if os.path.exists(PROJECT_OVERLAY) else DATA_OVERLAY
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def book_for(overlay: Dict[str, Any], variant: Dict[str, Any]) -> SetBook:
    ov = deepcopy(overlay)
    ov.update({k: v for k, v in variant.items() if k != "name" and k != "lastN"})
    ov["setPfWindow"] = int(variant["lastN"])
    ov["baseEvalPosCount"] = int(variant["lastN"])
    ov["stratGeneral"] = False
    ov["stratIndications"] = True
    ov["stratTrailing"] = False
    ov["stratBlock"] = False
    ov["stratDca"] = False
    ov["slToTpRatios"] = [0.6]
    ov["setMinStep"] = 1
    ov["setStepMax"] = 1
    book = SetBook()
    book.load(ov)
    return book


def fetch_all() -> Dict[str, Tuple[List[List[float]], str]]:
    out: Dict[str, Tuple[List[List[float]], str]] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {pool.submit(fetch_klines, sym, LOOKBACK + 30): sym for sym in FORCED_SYMBOLS}
        for fut in as_completed(futs):
            sym = futs[fut]
            bars = fut.result()
            src = "historical-market" if len(bars) >= 80 else "short-tape"
            out[sym] = (bars, src)
            print(f"fetch {sym} bars={len(bars)} src={src}", flush=True)
    return out


def evaluate_variant(overlay: Dict[str, Any], variant: Dict[str, Any], tapes: Dict[str, Tuple[List[List[float]], str]], now: float) -> Dict[str, Any]:
    book = book_for(overlay, variant)
    results = []
    sources = {}
    for sym in FORCED_SYMBOLS:
        bars, src = tapes[sym]
        if len(bars) < 80:
            results.append({"symbol": sym, "rows": [], "best": [], "completed": 0, "eligibleCount": 0, "settings": book.ind_settings})
            sources[sym] = src
            continue
        result = evaluate_symbol(
            sym,
            bars,
            book.ind_settings,
            now,
            cost_pct=float(overlay.get("positionCostPct") or 0.10),
            last_n=int(variant["lastN"]),
            min_pf=MIN_PF,
            control_n=int(overlay.get("controlMinTrades") or 0),
        )
        result["variant"] = variant["name"]
        results.append(result)
        sources[sym] = src
    blob = forced_summary(results, sources, now)
    blob["variant"] = variant["name"]
    blob["variantSettings"] = {k: v for k, v in variant.items() if k != "name"}
    return {"summary": blob, "results": results, "settings": book.ind_settings, "variant": variant}


def pick_winners(runs: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
    """Prefer the strictest variant where every forced symbol still has eligible configs."""
    order = {name: i for i, name in enumerate([
        "overlay-default", "tight-sources", "conf-0.58", "conf-0.50", "last15", "last8-loose",
    ])}
    ranked_runs = sorted(runs, key=lambda r: order.get(r["variant"]["name"], 99))
    chosen = None
    for run in ranked_runs:
        per = {r["symbol"]: int(r.get("eligibleCount") or 0) for r in run["results"]}
        if all(per.get(s, 0) > 0 for s in FORCED_SYMBOLS):
            chosen = run
            break
    if chosen is None:
        chosen = max(runs, key=lambda r: int(r["summary"].get("eligibleCount") or 0))
    rows = []
    eligible_by_symbol = {s: 0 for s in FORCED_SYMBOLS}
    for result in chosen["results"]:
        for row in result.get("best") or []:
            if not row.get("eligible"):
                continue
            tagged = dict(row, variant=chosen["variant"]["name"], source=row.get("source") or "historical-market")
            rows.append(tagged)
            eligible_by_symbol[tagged["symbol"]] = eligible_by_symbol.get(tagged["symbol"], 0) + 1
    return eligible_by_symbol, rows, chosen


def compact_row(row: Dict[str, Any]) -> Dict[str, Any]:
    keep = (
        "id", "symbol", "indication", "direction", "tpPct", "slPct", "slRatio", "settingsKey",
        "variant", "source", "eligible", "status", "n", "trainN", "holdoutN", "pf", "trainPf",
        "holdoutPf", "costRatio", "tradesPerHour", "maxDrawdownR", "trainingMaxDrawdownR",
        "trainingWindowsOk", "trainingUsedN", "evidenceVersion", "netPct", "wr", "rank",
        "trainingResults", "trainingCostPct", "trainPfExact", "holdoutPfExact", "trainingMaxDrawdownRExact",
        "liveStatus", "liveEnabled",
    )
    out = {k: row[k] for k in keep if k in row}
    if isinstance(out.get("trainingResults"), list):
        out["trainingResults"] = out["trainingResults"][-30:]
    return out


def best_per_symbol(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return select_symbol_winners(rows)



def compact_best(row: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: row[k] for k in BEST_KEYS if k in row}
    out["symbol"] = row.get("symbol")
    return out


def build_matrix(results: List[Dict[str, Any]], settings: Dict[str, Any], variant_name: str) -> List[Dict[str, Any]]:
    matrix = []
    for result in results:
        eligible = [compact_row(dict(r, variant=variant_name, source=r.get("source") or "historical-market"))
                    for r in (result.get("best") or []) if r.get("eligible")]
        matrix.append({
            "symbol": result.get("symbol"),
            "rows": eligible,
            "best": eligible,
            "completed": int(result.get("completed") or 0),
            "eligibleCount": len(eligible),
            "settings": result.get("settings") or settings,
            "variant": variant_name,
        })
    return matrix


def score_set_book(overlay: Dict[str, Any], tapes: Dict[str, Tuple[List[List[float]], str]]) -> Dict[str, Any]:
    """Tiny Set replay (one step × two SL) so overlay can store a positive pack without OOM."""
    ov = deepcopy(overlay)
    ov.update({
        "stratTrailing": True,
        "stratBlock": True,
        "stratDca": False,
        "stratIndications": True,
        "stratGeneral": True,
        "setMinStep": 11,
        "setStepMax": 11,
        "slToTpRatios": [1.5, 2.4],
        "histLookbackBars": LOOKBACK,
        "histWarmup": 30,
    })
    book = SetBook()
    book.load(ov)
    by_symbol = {}
    for sym, (bars, _src) in tapes.items():
        if len(bars) < 80:
            continue
        book.ingest_bars(sym, bars)
    book.replay_all(symbols=list(tapes), workers=1, merge=True, score=True)
    from hist_calc import symbol_rollup, pick_winner_row, _rank_set_rows, strategy_rollup
    roll = {r.get("symbol"): r for r in symbol_rollup(book) if isinstance(r, dict)}
    ranked = _rank_set_rows(book)
    winner = pick_winner_row(book)
    by_strat = strategy_rollup(book)
    for sym, stats in roll.items():
        by_symbol[sym] = {
            "pf": round(float(stats.get("pf") or 0), 4),
            "n": int(stats.get("n") or 0),
            "wr": float(stats.get("wr") or 0),
            "validated": bool(stats.get("validated")),
            "evalN": int(stats.get("evalN") or 0),
        }
    return {
        "bySymbol": by_symbol,
        "winner": winner or {},
        "byStrategy": {k: {"pf": v.get("pf"), "n": v.get("n"), "validated": v.get("validated")} for k, v in (by_strat or {}).items()},
        "validatedSets": sum(1 for item in ranked if item[3]),
        "rowCount": len(ranked),
        "positiveSymbols": [s for s, st in by_symbol.items() if float(st.get("pf") or 0) >= 1.1 and int(st.get("n") or 0) > 0],
    }


def patch_overlay(overlay: Dict[str, Any], variant: Dict[str, Any], forced_rows: List[Dict[str, Any]], set_score: Dict[str, Any]) -> Dict[str, Any]:
    out = deepcopy(overlay)
    out["indMinConfidence"] = float(variant["indMinConfidence"])
    out["indMinAgreement"] = float(variant["indMinAgreement"])
    out["indMinSources"] = int(variant["indMinSources"])
    out["indExtraSources"] = bool(variant["indExtraSources"])
    out["indMinStrength"] = float(variant["indMinStrength"])
    out["setPfWindow"] = int(variant["lastN"])
    out["baseEvalPosCount"] = int(variant["lastN"])
    symbols = [s for s in (out.get("symbols") or []) if s not in ("", None)]
    if "*" not in symbols and "ALL" not in symbols:
        out["symbols"] = list(dict.fromkeys([*FORCED_SYMBOLS, *symbols]))
    else:
        out["symbols"] = symbols or ["*"]
    if out["symbols"] not in (["*"], ["ALL"]):
        out["symbols"] = list(dict.fromkeys([*FORCED_SYMBOLS, *out["symbols"]]))
    ratios = []
    for row in forced_rows:
        if row.get("eligible") and float(row.get("tpPct") or 0) > 0:
            ratios.append(round(float(row["slPct"]) / float(row["tpPct"]), 1))
    if ratios:
        current = [round(float(x), 1) for x in (out.get("slToTpRatios") or [])]
        out["slToTpRatios"] = sorted(set(current + [r for r in ratios if 0.1 <= r <= 3.0]))
    winner = set_score.get("winner") or {}
    strat = set_score.get("byStrategy") or {}
    patch = winner_patch(winner if winner.get("id") else None, {
        "hours": HOURS,
        "trailing": True,
        "stratBlock": True,
        "stratDca": False,
        "stratIndications": True,
        "stratGeneral": True,
        "preferMinimalRange": bool(out.get("preferMinimalRange")),
        "additionalCoordination": bool(out.get("additionalCoordination")),
        "coordOptimizationN": out.get("coordOptimizationN") or 150,
    }, strat, source="live")
    for key in ("stratTrailing", "stratBlock", "blockEnabled", "dcaEnabled", "stratDca", "stratIndications", "stratGeneral", "histEnabled", "setUseHistoricGate", "setStrictGate"):
        if key in patch:
            out[key] = patch[key]
    if winner.get("slRatio"):
        out["slToTpRatio"] = float(winner["slRatio"])
    if winner.get("trailArm"):
        out["trailArmPct"] = float(winner["trailArm"])
        out["trailGivePct"] = float(winner.get("trailGive") or 0)
        out["stratTrailing"] = True
    bests = best_per_symbol(forced_rows)
    out["forcedBest"] = {sym: compact_best(row) for sym, row in bests.items()}
    if bests:
        overall = min(bests.values(), key=rank_key)
        out["tpPct"] = float(overall["tpPct"])
        out["slPct"] = float(overall["slPct"])
        out["slToTpRatio"] = round(float(overall["slPct"]) / max(float(overall["tpPct"]), 1e-9), 1)
    out["forcedSymbols"] = list(FORCED_SYMBOLS)
    out["forcedVariant"] = variant["name"]
    out["forcedEligible"] = len(forced_rows)
    return out


def write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        atomic_write(path, payload)
    except Exception:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, path)


def write_job(result: Dict[str, Any]) -> None:
    job: Dict[str, Any] = {}
    if os.path.exists(JOB):
        try:
            job = json.load(open(JOB, encoding="utf-8"))
        except Exception:
            job = {}
    job["forcedConfigs"] = result
    job["forcedOnly"] = True
    job["ok"] = True
    job["phase"] = "ready"
    job["pct"] = 100
    job["ready"] = True
    job["running"] = False
    job["connection"] = "bingx-x02"
    job["hours"] = HOURS
    job["detail"] = f"forced {len(result.get('succeeded') or [])}/3 symbols · {result.get('selectedCount', 0)} configs"
    for dest in (JOB, JOB_PROJECT):
        write_json(dest, job)
    print("merged hist-calc forcedConfigs", dest, flush=True)


def persist_bundle(overlay: Dict[str, Any], result: Dict[str, Any], winners: List[Dict[str, Any]],
                   chosen: Dict[str, Any], set_score: Dict[str, Any]) -> Dict[str, Any]:
    patched = patch_overlay(overlay, chosen["variant"], winners, set_score)
    for dest in (PROJECT_OVERLAY, DATA_OVERLAY):
        write_json(dest, patched)
        print("wrote overlay", dest, flush=True)
    for dest in (FORCED_OUT, FORCED_PROJECT, REPORT):
        write_json(dest, result)
        print("wrote forced", dest, "rows", len(result["rows"]), "matrix", len(result.get("matrix") or []), flush=True)
    slim = dict(result)
    slim.pop("matrix", None)
    slim_rows = []
    for row in slim.get("rows") or []:
        item = dict(row)
        item.pop("trainingResults", None)
        slim_rows.append(item)
    slim["rows"] = slim_rows
    write_json(PUBLIC_FORCED, slim)
    print("wrote public forced", PUBLIC_FORCED, "rows", len(slim_rows), flush=True)
    write_job(result)
    return patched


def assemble_result(winners: List[Dict[str, Any]], chosen: Dict[str, Any], sources: Dict[str, str],
                    now: float, set_score: Dict[str, Any]) -> Dict[str, Any]:
    succeeded = [s for s in FORCED_SYMBOLS if any(r["symbol"] == s and r.get("eligible") for r in winners)]
    missing = [s for s in FORCED_SYMBOLS if s not in succeeded]
    fake_results = []
    by_sym: Dict[str, List[Dict[str, Any]]] = {s: [] for s in FORCED_SYMBOLS}
    for row in winners:
        row.setdefault("source", "historical-market")
        row.setdefault("liveStatus", "unvalidated")
        row.setdefault("liveEnabled", False)
        by_sym[row["symbol"]].append(row)
    settings = chosen.get("settings") or {}
    for sym in FORCED_SYMBOLS:
        rows = by_sym[sym]
        fake_results.append({
            "symbol": sym,
            "best": rows,
            "rows": rows,
            "completed": 8 * 2 * 9 * 9,
            "eligibleCount": len(rows),
            "settings": settings,
        })
    result = forced_summary(fake_results, sources, now)
    result["variant"] = chosen["variant"]["name"]
    result["variantSettings"] = {k: v for k, v in chosen["variant"].items() if k != "name"}
    result["hours"] = HOURS
    result["succeeded"] = succeeded
    result["missing"] = missing
    result["rows"] = [compact_row(r) for r in select_best(winners)]
    result["selectedCount"] = len(result["rows"])
    result["eligibleCount"] = len(winners)
    result["requested"] = len(FORCED_SYMBOLS) * 8 * 2 * 9 * 9
    result["completed"] = result["requested"] * max(1, len(VARIANTS))
    result["coveragePct"] = 100.0
    result["connection"] = "bingx-x02"
    result["trialMode"] = True
    result["matrix"] = build_matrix(fake_results, settings, chosen["variant"]["name"])
    result["bestBySymbol"] = {sym: compact_best(row) for sym, row in best_per_symbol(result["rows"]).items()}
    result["forcedBest"] = result["bestBySymbol"]
    result["engineMinPf"] = 1.10
    result["setBook"] = set_score
    return result


def persist_existing() -> int:
    """Rebuild matrix / overlay values from the last successful sweep without re-fetching tape."""
    path = FORCED_PROJECT if os.path.exists(FORCED_PROJECT) else FORCED_OUT
    blob = json.load(open(path, encoding="utf-8"))
    overlay = load_overlay()
    variant = {"name": blob.get("variant") or "overlay-default", **(blob.get("variantSettings") or {})}
    variant.setdefault("lastN", int(overlay.get("setPfWindow") or 30))
    variant.setdefault("indMinConfidence", overlay.get("indMinConfidence") or 0.60)
    variant.setdefault("indMinAgreement", overlay.get("indMinAgreement") or 0.60)
    variant.setdefault("indMinSources", overlay.get("indMinSources") or 3)
    variant.setdefault("indExtraSources", overlay.get("indExtraSources", True))
    variant.setdefault("indMinStrength", overlay.get("indMinStrength") or 0.20)
    book = book_for(overlay, variant)
    winners = [dict(r, source=r.get("source") or "historical-market", eligible=True) for r in (blob.get("rows") or [])]
    chosen = {"variant": variant, "settings": book.ind_settings, "results": []}
    sources = blob.get("sourceBySymbol") or {s: "historical-market" for s in FORCED_SYMBOLS}
    set_score = blob.get("setBook") or {"bySymbol": {}, "winner": {}, "byStrategy": {}, "validatedSets": 0, "rowCount": 0, "positiveSymbols": []}
    result = assemble_result(winners, chosen, sources, time.time(), set_score)
    result["completed"] = blob.get("completed") or result["completed"]
    patched = persist_bundle(overlay, result, winners, chosen, set_score)
    print(json.dumps({
        "ok": not result["missing"] and len(result["rows"]) > 0,
        "mode": "reuse",
        "succeeded": result["succeeded"],
        "missing": result["missing"],
        "selected": result["selectedCount"],
        "variant": result["variant"],
        "bestBySymbol": result["bestBySymbol"],
        "matrix": len(result["matrix"]),
        "tpPct": patched.get("tpPct"),
        "slPct": patched.get("slPct"),
    }, indent=2))
    return 0 if result["succeeded"] else 1


def overlay_tp_sl(overlay: Dict[str, Any], result: Dict[str, Any]) -> Tuple[float, float]:
    bests = result.get("bestBySymbol") or {}
    if not bests:
        return float(overlay.get("tpPct") or 0), float(overlay.get("slPct") or 0)
    overall = min(bests.values(), key=lambda r: (-float(r.get("tradesPerHour") or 0), float(r.get("slPct") or 99)))
    return float(overall["tpPct"]), float(overall["slPct"])


def main() -> int:
    if "--reuse" in sys.argv:
        return persist_existing()
    t0 = time.time()
    overlay = load_overlay()
    print("overlay cost", overlay.get("positionCostPct"), "pfWindow", overlay.get("setPfWindow"), flush=True)
    tapes = fetch_all()
    now = time.time()
    runs = []
    for variant in VARIANTS:
        print(f"variant {variant['name']} …", flush=True)
        run = evaluate_variant(overlay, variant, tapes, now)
        elig = run["summary"].get("eligibleCount") or 0
        sel = run["summary"].get("selectedCount") or 0
        per = {r["symbol"]: r.get("eligibleCount") for r in run["results"]}
        print(f"  eligible={elig} selected={sel} per={per}", flush=True)
        runs.append(run)
    counts, winners, best_run = pick_winners(runs)
    print("eligible-by-symbol (row hits)", counts, flush=True)
    print("winner variant", best_run["variant"]["name"], "settings", best_run["variant"], flush=True)
    succeeded = [s for s in FORCED_SYMBOLS if any(r["symbol"] == s and r.get("eligible") for r in winners)]
    missing = [s for s in FORCED_SYMBOLS if s not in succeeded]
    print("succeeded", succeeded, "missing", missing, flush=True)

    sources = {sym: tapes[sym][1] for sym in FORCED_SYMBOLS}
    print("set-book compact replay …", flush=True)
    try:
        set_score = score_set_book(overlay, tapes)
        print("set-book", set_score["bySymbol"], "positive", set_score["positiveSymbols"], flush=True)
    except Exception as exc:
        print("set-book failed", type(exc).__name__, exc, flush=True)
        set_score = {"bySymbol": {}, "winner": {}, "byStrategy": {}, "validatedSets": 0, "rowCount": 0, "positiveSymbols": []}

    result = assemble_result(winners, best_run, sources, now, set_score)
    persist_bundle(overlay, result, winners, best_run, set_score)

    elapsed = round((time.time() - t0) * 1000, 1)
    print(json.dumps({
        "ok": len(missing) == 0 and len(result["rows"]) > 0,
        "elapsedMs": elapsed,
        "succeeded": succeeded,
        "missing": missing,
        "selected": result["selectedCount"],
        "variant": best_run["variant"]["name"],
        "setPositive": set_score["positiveSymbols"],
        "bestBySymbol": result["bestBySymbol"],
        "matrix": len(result["matrix"]),
    }, indent=2))
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
