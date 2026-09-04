"""Independent, bounded baseline sweep for mandatory indication symbols.

Percent units at the boundary; decimal fractions only inside the replay.
PF is conventional NET gross-profit / gross-loss, not CTS's cost ratio.
No Block, DCA, trailing, scratch, or early-exit strategy participates.
Historical candidates are never evidence of exchange execution.
"""
from __future__ import annotations

from collections import deque
import hashlib
import json
import math
from typing import Any, Dict, List, Sequence

from position_cost import evaluation_windows, last_n_cost_pf
from set_engine import BAR_S, IND_KINDS, IND_TAG_KIND, indication_kind_votes

FORCED_SYMBOLS = ("XRP-USDT", "BCH-USDT", "SOL-USDT")
TP_GRID = tuple(v / 100 for v in range(40, 81, 5))
SL_GRID = tuple(v / 100 for v in range(10, 51, 5))
MIN_PF = 1.02
TOP_N = 5
MAX_TAPE = 80


def mandatory_symbols(names: Sequence[str]) -> List[str]:
    return list(dict.fromkeys([*names, *FORCED_SYMBOLS]))


def _pf(gain: float, loss: float) -> float:
    return gain / loss if loss > 0 else (99.0 if gain > 0 else 0.0)


def rank_key(row: Dict[str, Any]) -> tuple:
    # Only eligible rows reach selection. Prioritize observed throughput,
    # then smaller risk and stronger chronological holdout performance.
    return (-row["tradesPerHour"], row["slPct"], row["maxDrawdownR"],
            -row["holdoutPf"], -row["pf"], row["tpPct"], row["id"])


def select_best(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for row in rows:
        if row.get("eligible"):
            groups.setdefault((row["symbol"], row["indication"]), []).append(row)
    return [dict(row, rank=i + 1) for key in sorted(groups)
            for i, row in enumerate(sorted(groups[key], key=rank_key)[:TOP_N])]


def _replay(bars, signals, side, tp_pct, sl_pct, warmup, now, cost_pct, need, floor, dd_limit):
    """One independent lane. Fixed 80-close tape; full-period exact totals.

    Enter at the signal candle close; exits start on the next candle.
    Gaps through a stop fill at the worse open; ambiguous TP+SL uses SL.
    Reset at the 70/30 split so no training position leaks into holdout.
    """
    nbar = len(bars)
    split = warmup + int((nbar - warmup) * .7)
    tp, sl, cost = tp_pct / 100, sl_pct / 100, cost_pct / 100
    entry = 0.0
    entered = 0
    gp = gl = train_gp = train_gl = test_gp = test_gl = 0.0
    n = train_n = test_n = censored = 0
    equity = peak = drawdown = hold_sum = 0.0
    tape: deque = deque(maxlen=MAX_TAPE)
    for i in range(warmup, nbar):
        if i == split and entry:
            censored += 1
            entry = 0.0
        op, hi, lo, close = (float(v) for v in bars[i][:4])
        if not all(math.isfinite(v) and v > 0 for v in (op, hi, lo, close)):
            continue
        if entry:
            stop = entry * (1 - side * sl)
            target = entry * (1 + side * tp)
            hit_sl = lo <= stop if side == 1 else hi >= stop
            hit_tp = hi >= target if side == 1 else lo <= target
            if hit_sl or hit_tp:
                px = (min(op, stop) if side == 1 else max(op, stop)) if hit_sl else target
                raw = side * (px - entry) / entry
                net = raw - cost
                gain, loss = max(0.0, net), max(0.0, -net)
                gp += gain
                gl += loss
                n += 1
                if i < split:
                    train_n += 1
                    train_gp += gain
                    train_gl += loss
                else:
                    test_n += 1
                    test_gp += gain
                    test_gl += loss
                equity += net
                peak = max(peak, equity)
                drawdown = max(drawdown, peak - equity)
                hold_sum += (i - entered) * BAR_S
                tape.append({"t": now - (nbar - 1 - i) * BAR_S, "pnl": net,
                             "pnl_pct": raw, "costPct": cost_pct,
                             "hold_s": (i - entered) * BAR_S})
                entry = 0.0
                # No exit-bar re-entry: conservatively avoid artificial churn.
                continue
        direction, conf = signals[i]
        if not entry and direction == side and conf >= .58:
            entry, entered = close, i
    windows = evaluation_windows(list(tape), cost_pct, required_samples=need)
    windows_ok = all(m["classicPf"] > 1 and m["netAvg"] > 0
                     for m in windows.values() if m["n"] >= m["requiredSamples"])
    pf, train_pf, test_pf = _pf(gp, gl), _pf(train_gp, train_gl), _pf(test_gp, test_gl)
    dd_r = drawdown / max(sl + cost, 1e-12)
    enough = train_n >= need and test_n >= need
    eligible = enough and min(pf, train_pf, test_pf) > floor and windows_ok and dd_r <= dd_limit
    reason = ("candidate" if eligible else "insufficient-samples" if not enough else
              "baseline-pf" if min(pf, train_pf, test_pf) <= floor else
              "negative-window" if not windows_ok else "drawdown-limit")
    hours = max(BAR_S, (nbar - warmup) * BAR_S) / 3600
    return {"n": n, "trainN": train_n, "holdoutN": test_n,
            "pf": round(pf, 6), "trainPf": round(train_pf, 6), "holdoutPf": round(test_pf, 6),
            "costRatio": last_n_cost_pf(list(tape), 15, cost_pct)["ratio"],
            "netPct": round((gp - gl) * 100, 6), "netAvgPct": round((gp - gl) * 100 / max(1, n), 6),
            "maxDrawdownR": round(dd_r, 6), "tradesPerHour": round(n / hours, 6),
            "avgHoldS": round(hold_sum / max(1, n), 2), "openUnresolved": int(bool(entry)),
            "splitCensored": censored, "eligible": eligible, "status": reason,
            "evaluationWindows": windows}


def evaluate_symbol(symbol: str, bars: Sequence[Sequence[float]], settings: Dict[str, Any],
                    now: float, *, cost_pct: float = .15, required_samples: int = 8,
                    min_pf: float = MIN_PF, max_drawdown_r: float = 6.0) -> Dict[str, Any]:
    if symbol not in FORCED_SYMBOLS:
        return {"symbol": symbol, "rows": [], "best": [], "completed": 0}
    need = max(8, int(required_samples))
    floor = max(MIN_PF, float(min_pf))
    # Force every indication on independently of the normal strategy toggles.
    settings = dict(settings, **{f"type{k}": True for k in
                    ("State", "Signals", "Active", "Direction", "Move", "Common", "Trend", "Break")})
    settings_key = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:16]
    signals = {kind: [(0, 0.0)] * len(bars) for kind in IND_KINDS}
    warmup = min(60, max(16, len(bars) // 5))
    for i in range(warmup, len(bars)):
        ts = now - (len(bars) - 1 - i) * BAR_S
        for direction, conf, tag in indication_kind_votes(bars[max(0, i - 59):i + 1], settings, ts):
            kind = IND_TAG_KIND.get(tag)
            if kind:
                signals[kind][i] = direction, conf
    rows = []
    for kind in IND_KINDS:
        for side, direction in ((1, "LONG"), (-1, "SHORT")):
            for tp in TP_GRID:
                for sl in SL_GRID:
                    metrics = _replay(bars, signals[kind], side, tp, sl, warmup, now,
                                      cost_pct, need, floor, max_drawdown_r)
                    rows.append({"id": f"forced:{symbol}:{kind}:{direction}:tp{tp:.2f}:sl{sl:.2f}:{settings_key}",
                                 "symbol": symbol, "indication": kind, "direction": direction,
                                 "tpPct": tp, "slPct": sl, "slRatio": round(sl / tp, 8),
                                 "settingsKey": settings_key, **metrics})
    best = select_best(rows)
    # Complete scalar coverage, with detailed windows for selected configs only.
    compact = [{k: v for k, v in row.items() if k != "evaluationWindows"} for row in rows]
    return {"symbol": symbol, "rows": compact, "best": best, "completed": len(rows),
            "eligibleCount": sum(bool(r["eligible"]) for r in rows), "settings": settings,
            "settingsKey": settings_key, "bars": len(bars), "requiredSamples": need,
            "minPf": floor, "maxDrawdownR": max_drawdown_r, "costPct": cost_pct}


def summary(results: Sequence[Dict[str, Any]], sources: Dict[str, str], now: float) -> Dict[str, Any]:
    rows = [dict(row, source=sources.get(result["symbol"], "unknown"),
                 liveStatus="unvalidated", liveEnabled=False)
            for result in results for row in result.get("best", [])]
    completed = sum(int(r.get("completed", 0)) for r in results)
    requested = len(FORCED_SYMBOLS) * len(IND_KINDS) * 2 * len(TP_GRID) * len(SL_GRID)
    return {"version": 1, "symbols": list(FORCED_SYMBOLS), "tpGrid": list(TP_GRID), "slGrid": list(SL_GRID),
            "minPf": MIN_PF, "pfDefinition": "net-gross-profit / net-gross-loss",
            "baselineOnly": True, "additionalStrategies": [], "topPerSymbolIndication": TOP_N,
            "requiredSamplesPerSplit": 8, "split": "70/30 chronological; reset at split",
            "sourceBySymbol": sources, "costSource": "configured-replay-cost",
            "priority": "positive PF + windows + drawdown, then trades/hour, lower SL",
            "requested": requested, "completed": completed,
            "coveragePct": round(100 * completed / requested, 2), "rows": rows,
            "eligibleCount": sum(int(r.get("eligibleCount", 0)) for r in results),
            "selectedCount": len(rows), "updatedAt": now,
            "mainnetReady": False, "liveGate": "requires independent confirmed VST roundtrips"}
