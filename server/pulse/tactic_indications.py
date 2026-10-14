"""Causal, regime-aware Tactic indication configurations.

Each timeframe × tactic × range is a separate indication identity. These are
entry candidates; Base/Main/Real return-PF evaluation still decides admission.
No historical win rate or profitability is inferred from an entry target.
"""
from __future__ import annotations

import math
import statistics
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

from position_cost import net_return_pf, normalize_position_cost_pct

TACTIC_TIMEFRAMES = ("1m", "5m", "15m", "30m")
TACTIC_METHODS = ("trend", "breakout", "pullback", "move", "mean_reversion")
TACTIC_DEFAULTS = {
    "typeTactic": True,
    "tacticRanges": [12, 20, 32],
    "tacticMethods": list(TACTIC_METHODS),
    "tacticTf1m": True, "tacticTf5m": True,
    "tacticTf15m": True, "tacticTf30m": True,
    "tacticCombined": True, "tacticMinAgree": 2,
    "tacticTrendEfficiency": 0.55,
    "tacticRevertEfficiencyMax": 0.40,
    "tacticRevertZ": 1.5,
    "tacticMoveAtr": 0.8,
    "tacticMoveVolume": 1.15,
    "tacticAtrMinPct": 0.02,
    "tacticMinTargetReturnPf": 1.10,
}


def _number(settings: Dict[str, Any], key: str) -> float:
    try:
        value = float(settings.get(key, TACTIC_DEFAULTS[key]))
        if math.isfinite(value):
            return value
    except (ValueError, TypeError):
        pass
    return float(TACTIC_DEFAULTS[key])


def closed_rows(rows: Sequence, minutes: int, now_s: float) -> List[List[float]]:
    """Normalize timestamped rows, or caller-confirmed closed OHLCV rows.

    Timestamp-free engine tapes end at the latest complete UTC bucket. For
    historical replay the caller must supply the actual decision time.
    """
    step = minutes * 60
    end = math.floor(now_s / step) * step
    result = {}
    for i, row in enumerate(rows):
        try:
            if len(row) == 2 and isinstance(row[1], (list, tuple)):
                stamp, vals = float(row[0]), [float(x) for x in row[1][:5]]
            elif len(row) >= 6:
                stamp, vals = float(row[0]), [float(x) for x in row[1:6]]
            elif len(row) == 5:
                stamp, vals = end - (len(rows) - i) * step, [float(x) for x in row]
            else:
                continue
            if stamp > 1e11:
                stamp /= 1000.0
            if len(vals) != 5 or not all(math.isfinite(x) for x in [stamp, *vals]):
                continue
            o, h, l, c, v = vals
            if min(o, h, l, c) <= 0 or v < 0 or h < max(o, c) or l > min(o, c):
                continue
            if stamp % step != 0 or stamp + step > now_s:
                continue
            result[stamp] = [stamp, *vals]
        except (TypeError, ValueError, IndexError):
            continue
    return [result[t] for t in sorted(result)]


def aggregate_closed_1m(rows: Sequence, minutes: int, now_s: float) -> List[List[float]]:
    """UTC OHLCV buckets; reject partial, forming and gapped higher bars."""
    one = closed_rows(rows, 1, now_s)
    if minutes == 1:
        return one
    groups = defaultdict(list)
    span = minutes * 60
    for row in one:
        groups[math.floor(row[0] / span) * span].append(row)
    out = []
    for start, values in sorted(groups.items()):
        if len(values) != minutes or values[0][0] != start or values[-1][0] != start + span - 60:
            continue
        if any(b[0] - a[0] != 60 for a, b in zip(values, values[1:])) or start + span > now_s:
            continue
        out.append([start, values[0][1], max(r[2] for r in values), min(r[3] for r in values),
                    values[-1][4], sum(r[5] for r in values)])
    return out


def _features(rows: List[List[float]], period: int, minutes: int) -> Optional[Dict[str, float]]:
    if len(rows) < period + 2:
        return None
    window = rows[-period - 2:]
    if any(b[0] - a[0] != minutes * 60 for a, b in zip(window, window[1:])):
        return None
    prior, last = window[:-1], window[-1]
    closes = [r[4] for r in prior]
    steps = [b - a for a, b in zip(closes, closes[1:])]
    travel = sum(abs(x) for x in steps)
    change = closes[-1] - closes[0]
    efficiency = abs(change) / travel if travel > 0 else 0.0
    mean = statistics.fmean(closes)
    sigma = statistics.pstdev(closes)
    atr = statistics.fmean(max(r[2] - r[3], abs(r[2] - prev[4]), abs(r[3] - prev[4]))
                          for prev, r in zip(prior, prior[1:]))
    px = last[4]
    return {"price": px, "prev": closes[-1], "mean": mean,
            "fast": statistics.fmean(closes[-max(3, period // 3):]),
            "sigma": sigma, "atr": atr, "atrPct": atr / px * 100,
            "efficiency": efficiency, "trend": 1.0 if change > 0 else (-1.0 if change < 0 else 0.0),
            "step": px - closes[-1], "z": (px - mean) / sigma if sigma > 0 else 0.0,
            "high": max(r[2] for r in prior), "low": min(r[3] for r in prior),
            "volumeRatio": last[5] / statistics.fmean(r[5] for r in prior)
            if any(r[5] > 0 for r in prior) else 0.0,
            "closeTime": last[0] + minutes * 60}


def _direction(method: str, f: Dict[str, float], settings: Dict[str, Any]) -> int:
    trend = int(f["trend"])
    trending = f["efficiency"] >= _number(settings, "tacticTrendEfficiency")
    aligned = trend != 0 and trend * f["step"] > 0
    if method == "trend":
        return trend if trending and aligned and trend * (f["price"] - f["fast"]) > 0 else 0
    if method == "breakout":
        sign = 1 if f["price"] > f["high"] else (-1 if f["price"] < f["low"] else 0)
        return sign if trending and sign == trend and aligned else 0
    if method == "pullback":
        reclaimed = trend * (f["prev"] - f["fast"]) <= 0 < trend * (f["price"] - f["fast"])
        return trend if trending and aligned and reclaimed else 0
    if method == "move":
        sign = 1 if f["step"] > 0 else (-1 if f["step"] < 0 else 0)
        enough = abs(f["step"]) >= f["atr"] * _number(settings, "tacticMoveAtr")
        return sign if enough and sign and f["volumeRatio"] >= _number(settings, "tacticMoveVolume") and (not trending or sign == trend) else 0
    if method == "mean_reversion":
        sign = -1 if f["z"] > 0 else 1
        ranging = f["efficiency"] <= _number(settings, "tacticRevertEfficiencyMax")
        turned = sign * f["step"] > 0
        return sign if ranging and turned and abs(f["z"]) >= _number(settings, "tacticRevertZ") else 0
    return 0


def _ranges(settings: Dict[str, Any]) -> List[int]:
    try:
        return sorted({max(6, min(240, int(n))) for n in settings.get("tacticRanges", TACTIC_DEFAULTS["tacticRanges"])})
    except (TypeError, ValueError, OverflowError):
        return list(TACTIC_DEFAULTS["tacticRanges"])


def _methods(settings: Dict[str, Any]) -> List[str]:
    methods = settings.get("tacticMethods", TACTIC_METHODS)
    if not isinstance(methods, (list, tuple)):
        methods = TACTIC_METHODS
    return list(dict.fromkeys(m for m in methods if isinstance(m, str) and m in TACTIC_METHODS))


def _combine_tactic_rows(symbol: str, candidates: List[Any], settings: Dict[str, Any]) -> List[Any]:
    """One shared live/replay combination rule over current closed TF states."""
    if not settings.get("tacticCombined", True):
        return []
    from indication_engine import _kind_indication
    grouped = defaultdict(list)
    for row in candidates:
        bits = row.mode.split(":")
        grouped[(bits[1], bits[2])].append(row)
    minimum = max(2, min(4, int(settings.get("tacticMinAgree", 2))))
    out = []
    for (method, period), rows in grouped.items():
        for direction in ("long", "short"):
            wins = [r for r in rows if r.direction == direction]
            if len(wins) < minimum or len(wins) <= len(rows) - len(wins):
                continue
            latest = max(wins, key=lambda r: r.t)
            sl = max(r.stop_loss_pct for r in wins)
            target = min(r.take_profit_pct for r in wins)
            if target < sl * 1.1:
                continue
            row = _kind_indication(symbol, "tactic", direction,
                statistics.fmean(r.strength for r in wins), latest.last_price, settings,
                [f"tactic:{method}:{period}:combined", *[f"tf:{r.timeframe}" for r in wins]],
                sl_pct=sl, tp_pct=target, agreement=len(wins) / len(rows), timeframe="combined",
                mode=f"tactic:{method}:{period}:combined")
            row.t = latest.t
            row.votes_long = sum(r.direction == "long" for r in rows)
            row.votes_short = sum(r.direction == "short" for r in rows)
            if method == "mean_reversion":
                row.exit_tactic, row.exit_level = latest.exit_tactic, latest.exit_level
            out.append(row)
    return out


def evaluate_tactic_configs(symbol: str, bars: Sequence, settings: Dict[str, Any],
                            bars_by_tf: Optional[Dict[str, Sequence]] = None,
                            now_s: Optional[float] = None) -> List[Any]:
    """Return independent Tactic candidates; no PF admission gate is bypassed."""
    if not settings.get("typeTactic", True):
        return []
    from indication_engine import _kind_indication
    now = time.time() if now_s is None else float(now_s)
    if not math.isfinite(now):
        return []
    ranges, methods = _ranges(settings), _methods(settings)
    tf_map = bars_by_tf or {}
    one = closed_rows(tf_map.get("1m") or bars, 1, now)
    cost = normalize_position_cost_pct(settings.get("positionCostPct", 0.1))
    out = []
    for tf in TACTIC_TIMEFRAMES:
        if not settings.get("tacticTf" + tf, True):
            continue
        minutes = int(tf[:-1])
        supplied = tf_map.get(tf)
        rows = closed_rows(supplied, minutes, now) if supplied else aggregate_closed_1m(one, minutes, now)
        if rows and rows[-1][0] + minutes * 120 <= now:
            continue  # an entire expected higher bucket is missing: no stale entry
        for period in ranges:
            f = _features(rows, period, minutes)
            if f is None or f["atrPct"] < max(_number(settings, "tacticAtrMinPct"), cost * 0.35):
                continue
            for method in methods:
                sign = _direction(method, f, settings)
                if not sign:
                    continue
                sl_min = float(settings.get("stopLossMinPct", 0.4))
                sl = max(sl_min, f["atrPct"] * 1.2)
                sl = min(sl, max(sl_min, float(settings.get("stopLossMaxPct", 1.5))))
                target = sl * float(settings.get("takeProfitRewardRisk", 1.8))
                if method == "mean_reversion":
                    potential = abs(f["mean"] - f["price"]) / f["price"] * 100
                    if potential < target:
                        continue
                    target = min(target, potential)
                target = min(target, float(settings.get("takeProfitMaxPct", 5.0)))
                target_pf = net_return_pf((target - cost) / 100.0, cost)
                if target_pf < _number(settings, "tacticMinTargetReturnPf") or target < sl * 1.1:
                    continue
                strength = min(1.0, max(f["efficiency"], abs(f["z"]) / 4.0, abs(f["step"]) / max(f["atr"], 1e-12) / 3.0))
                row = _kind_indication(symbol, "tactic", "long" if sign > 0 else "short", strength,
                    f["price"], settings,
                    [f"tactic:{method}:{period}:{tf}", f"regime:efficiency={f['efficiency']:.3f}",
                     f"targetReturnPf={target_pf:.4f}"], sl_pct=sl, tp_pct=target,
                    timeframe=tf, mode=f"tactic:{method}:{period}")
                row.t = f["closeTime"]
                if method == "mean_reversion":
                    row.exit_tactic, row.exit_level = "mean_reversion", f["mean"]
                out.append(row)
    out.extend(_combine_tactic_rows(symbol, out, settings))
    return out


class TacticReplaySignals(dict):
    """Signal lanes plus sparse per-entry price exits, like SetBook KindSignals."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.exits: Dict[str, Dict[int, Tuple[str, float]]] = {}


def prepare_tactic_signals(symbol: str, bars: Sequence, settings: Dict[str, Any],
                           now_s: float, end_open_ts: Optional[float] = None) -> Dict[str, List[Tuple[int, float]]]:
    """Chronological full-grid replay lanes, with cached higher-TF evaluations.

    ``now_s`` is the final decision/close bound. ``end_open_ts`` optionally
    anchors the last timestamp-free input candle's OPEN (seconds or ms).
    Keys are ``tactic:method:range:1m`` etc. Combined already ends in combined.
    Every lane has exactly len(bars) samples, including warmup and missing data.
    """
    n = len(bars)
    output = TacticReplaySignals()
    if not settings.get("typeTactic", True) or not n or not math.isfinite(float(now_s)):
        return output
    now = float(now_s)
    anchor = math.floor(now / 60) * 60 - 60 if end_open_ts is None else float(end_open_ts)
    if anchor > 1e11:
        anchor /= 1000.0
    stamped, original_index = [], {}
    for i, raw in enumerate(bars):
        row = [anchor - (n - 1 - i) * 60, *raw] if len(raw) == 5 else raw
        parsed = closed_rows([row], 1, now)
        if parsed:
            stamped.append(parsed[0])
            original_index[parsed[0][0]] = i
    one = closed_rows(stamped, 1, now)
    ranges, methods = _ranges(settings), _methods(settings)
    enabled = [tf for tf in TACTIC_TIMEFRAMES if settings.get("tacticTf" + tf, True)]
    for method in methods:
        for period in ranges:
            for tf in [*enabled, *(["combined"] if settings.get("tacticCombined", True) else [])]:
                output[f"tactic:{method}:{period}:{tf}"] = [(0, 0.0)] * n
    maximum = max(ranges, default=32) + 2
    frames = {tf: aggregate_closed_1m(one, int(tf[:-1]), now) for tf in enabled}
    counts = {tf: 0 for tf in enabled}
    current = {tf: [] for tf in enabled}
    lane_settings = {
        tf: {**settings, "tacticCombined": False,
             **{"tacticTf" + other: other == tf for other in TACTIC_TIMEFRAMES}}
        for tf in enabled
    }
    for base in one:
        decision = base[0] + 60
        index = original_index[base[0]]
        candidates = []
        for tf in enabled:
            minutes, rows = int(tf[:-1]), frames[tf]
            previous = count = counts[tf]
            while count < len(rows) and rows[count][0] + minutes * 60 <= decision:
                count += 1
            if count != previous:
                counts[tf] = count
                recent = rows[max(0, count - maximum):count]
                current[tf] = evaluate_tactic_configs(symbol, [], lane_settings[tf],
                    bars_by_tf={tf: recent}, now_s=decision)
            if count and rows[count - 1][0] + minutes * 120 <= decision:
                current[tf] = []
            candidates.extend(current[tf])
        candidates.extend(_combine_tactic_rows(symbol, candidates, settings))
        for row in candidates:
            key = row.mode if row.timeframe == "combined" else row.mode + ":" + row.timeframe
            direction = 1 if row.direction == "long" else -1
            previous = output[key][index]
            if row.confidence >= previous[1]:
                output[key][index] = (direction, row.confidence)
                if row.exit_tactic and row.exit_level > 0:
                    output.exits.setdefault(key, {})[index] = (row.exit_tactic, row.exit_level)
    return output
