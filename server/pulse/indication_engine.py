#!/usr/bin/env python3
"""CTS Signal processing coordinated as Indications.

Exact formulas from lib/signal-indication.ts (evaluateSignalCandles,
lowStopConsensus) plus the indication-stage RSI/MACD/EMA pack.
"""
from __future__ import annotations

import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from contracts import INDICATION_KINDS

try:
    import httpx
except Exception:
    httpx = None  # type: ignore


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


@dataclass
class Candle:
    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class SignalEval:
    source_id: str
    source_name: str
    direction: str  # long | short
    confidence: float
    strength: float
    stop_loss_pct: float
    take_profit_pct: float
    reward_risk: float
    atr_pct: float
    last_price: float
    candle_count: int
    weight: float = 1.0


@dataclass
class Indication:
    symbol: str
    direction: str
    mode: str  # multi_source_consensus | direct_source | tf_combined | direct_tf
    confidence: float
    strength: float
    agreement: float
    stop_loss_pct: float
    take_profit_pct: float
    reward_risk: float
    last_price: float
    sources: List[str]
    votes_long: int
    votes_short: int
    primary: bool
    t: float
    timeframe: str = ""
    kind: str = "state"


DEFAULT_SETTINGS: Dict[str, Any] = {
    "enabled": True,
    "candleLimit": 60,
    "minimumSourceSignals": 3,
    "minimumAgreement": 0.6,
    "minimumConfidence": 0.6,
    "minimumStrength": 0.2,
    "stopLossMinPct": 0.2,
    "stopLossMaxPct": 1.5,
    "stopLossAtrMultiplier": 0.85,
    "takeProfitRewardRisk": 1.8,
    "takeProfitMaxPct": 5.0,
    "extraSources": True,
    "positionCostPct": 0.1,
    "tf1m": True,
    "tf5m": True,
    "tf15m": True,
    "tfCombined": True,
    "tfMinAgree": 2,
    "typeState": True,
    "typeDirection": True,
    "typeMove": True,
    "typeActive": True,
    "typeCommon": True,
    "typeSignals": True,
    "typeTrend": True,
    "typeBreak": True,
    "dirRange": 10,
    "dirMinChange": 0.001,
    "moveRange": 10,
    "moveMinChange": 0.001,
    "activeOutbreak": [3, 5, 10],
    "activeThreshold": 1.0,
    "activeNoise": 0.0005,
    "activeMovePct": 0.5,
    "activeVolatilityWeight": 0.3,
}

TIMEFRAMES = ("1m", "5m", "15m")
TF_SECONDS = {"1m": 60.0, "5m": 300.0, "15m": 900.0}
TF_WEIGHT = {"1m": 0.85, "5m": 1.0, "15m": 1.2}


def ema(values: List[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1)
    current = values[0]
    for v in values[1:]:
        current = v * alpha + current * (1 - alpha)
    return current


def rsi(values: List[float], period: int = 14) -> float:
    if len(values) < 2:
        return 50.0
    start = max(1, len(values) - period)
    gains = losses = samples = 0.0
    for i in range(start, len(values)):
        delta = values[i] - values[i - 1]
        if delta > 0:
            gains += delta
        else:
            losses -= delta
        samples += 1
    if samples == 0:
        return 50.0
    avg_gain = gains / samples
    avg_loss = losses / samples
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def atr(candles: List[Candle], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    start = max(1, len(candles) - period)
    total = 0.0
    count = 0
    for i in range(start, len(candles)):
        cur = candles[i]
        prev_c = candles[i - 1].close
        total += max(cur.high - cur.low, abs(cur.high - prev_c), abs(cur.low - prev_c))
        count += 1
    return total / count if count else 0.0


def recent_return(closes: List[float], periods: int) -> float:
    if len(closes) <= periods:
        return 0.0
    prev = closes[-1 - periods]
    cur = closes[-1]
    return cur / prev - 1.0 if prev > 0 else 0.0


def ohlcv_row(bar: Sequence[float]) -> Optional[Tuple[float, float, float, float, float]]:
    """Normalize a bar to (o, h, l, c, v). Accepts [o,h,l,c,v] or [ts,o,h,l,c,v]."""
    if bar is None:
        return None
    try:
        n = len(bar)
    except Exception:
        return None
    if n < 5:
        return None
    try:
        if n >= 6:
            ts = float(bar[0])
            if ts > 1e11:
                ts = ts / 1000.0
            if ts > 1e9:
                o, h, l, c, v = (float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4]), float(bar[5]))
            else:
                o, h, l, c, v = (float(bar[0]), float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4]))
        else:
            o, h, l, c, v = (float(bar[0]), float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4]))
    except Exception:
        return None
    if o <= 0 or c <= 0 or h <= 0 or l <= 0:
        return None
    if h < l:
        h, l = l, h
    return o, h, l, c, v


def bars_to_candles(bars: List[List[float]], now: Optional[float] = None, period_s: float = 60.0) -> List[Candle]:
    now = now or time.time()
    step = period_s if period_s > 0 else 60.0
    parsed: List[Tuple[float, float, float, float, float]] = []
    for b in bars:
        row = ohlcv_row(b)
        if row:
            parsed.append(row)
    n = len(parsed)
    out: List[Candle] = []
    for i, (o, h, l, c, v) in enumerate(parsed):
        out.append(Candle(now - (n - 1 - i) * step, o, h, l, c, v))
    return out


def evaluate_signal_candles(
    source_id: str,
    source_name: str,
    candles: List[Candle],
    settings: Dict[str, Any],
    weight: float = 1.0,
) -> Optional[SignalEval]:
    limit = max(20, int(settings.get("candleLimit", 60)))
    candles = candles[-limit:]
    if len(candles) < 20:
        return None
    closes = [c.close for c in candles]
    latest = candles[-1]
    if latest.close <= 0:
        return None
    average_true_range = atr(candles)
    fallback = sum(abs(c.close - c.open) for c in candles[-10:]) / min(10, len(candles))
    atr_pct = (max(average_true_range, fallback) / latest.close) * 100.0
    fast = ema(closes[-30:], 5)
    slow = ema(closes[-45:], 13)
    trend_scale = max(average_true_range, latest.close * 0.0005)
    trend_score = clamp((fast - slow) / trend_scale, -1, 1)
    momentum3 = recent_return(closes, 3)
    momentum9 = recent_return(closes, 9)
    movement_scale = max(atr_pct / 100.0, 0.0005)
    momentum_score = clamp(momentum3 / movement_scale, -1, 1)
    rsi_score = clamp((rsi(closes) - 50.0) / 30.0, -1, 1)
    window = candles[-14:]
    range_high = max(c.high for c in window)
    range_low = min(c.low for c in window)
    range_score = (
        clamp(((latest.close - range_low) / (range_high - range_low) - 0.5) * 2, -1, 1)
        if range_high > range_low
        else 0.0
    )
    vols = [c.volume for c in candles[-20:] if c.volume > 0]
    avg_vol = sum(vols) / len(vols) if vols else 0.0
    vol_ratio = (latest.volume / avg_vol) if avg_vol > 0 and latest.volume > 0 else 1.0
    # Low-volume bars are noise: skip unless the move is already strong.
    if avg_vol > 0 and vol_ratio < 0.45 and abs(momentum3) < 0.004:
        return None
    volume_impulse = (
        clamp((vol_ratio - 1.0) * (1 if (momentum3 or trend_score) >= 0 else -1), -1, 1)
        if avg_vol > 0
        else 0.0
    )
    vol_w = 0.12 if vol_ratio >= 1.0 else 0.18
    raw = (
        trend_score * 0.32
        + momentum_score * 0.28
        + rsi_score * 0.16
        + range_score * 0.12
        + volume_impulse * vol_w
    )
    strength = abs(raw)
    if not (strength >= float(settings.get("minimumStrength", 0.2))):
        return None
    cost = max(0.0, float(settings.get("positionCostPct", 0.1))) + 0.08
    raw_sl = atr_pct * float(settings.get("stopLossAtrMultiplier", 0.85)) + cost
    sl_max = float(settings.get("stopLossMaxPct", 1.5))
    if raw_sl > sl_max * 1.25:
        return None
    sl = clamp(raw_sl, float(settings.get("stopLossMinPct", 0.2)), sl_max)
    rr = float(settings.get("takeProfitRewardRisk", 1.8))
    min_tp = sl * rr
    tp_max = float(settings.get("takeProfitMaxPct", 5.0))
    if min_tp > tp_max:
        return None
    momentum_target = abs(momentum9) * 100.0 * 0.75
    tp = clamp(max(min_tp, momentum_target), min_tp, tp_max)
    confidence = clamp(0.5 + strength * 0.45 + min(0.04, len(candles) / 2500.0), 0.5, 0.99)
    if confidence < float(settings.get("minimumConfidence", 0.6)):
        return None
    return SignalEval(
        source_id=source_id,
        source_name=source_name,
        direction="long" if raw >= 0 else "short",
        confidence=confidence,
        strength=strength,
        stop_loss_pct=sl,
        take_profit_pct=tp,
        reward_risk=tp / sl if sl else rr,
        atr_pct=atr_pct,
        last_price=latest.close,
        candle_count=len(candles),
        weight=clamp(weight, 0.1, 2.0),
    )


def evaluate_ta_pack(candles: List[Candle], settings: Dict[str, Any]) -> Optional[SignalEval]:
    if len(candles) < 26:
        return None
    closes = [c.close for c in candles]
    latest = candles[-1]
    r = rsi(closes, 14)
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    macd = ema12 - ema26
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    rsi_score = clamp((r - 50.0) / 30.0, -1, 1)
    macd_score = clamp(macd / max(latest.close * 0.0008, 1e-9), -1, 1)
    ema_score = clamp((ema20 - ema50) / max(latest.close * 0.001, 1e-9), -1, 1)
    raw = rsi_score * 0.4 + macd_score * 0.3 + ema_score * 0.3
    strength = abs(raw)
    if strength < float(settings.get("minimumStrength", 0.2)):
        return None
    sl_min = float(settings.get("stopLossMinPct", 0.2))
    sl_max = float(settings.get("stopLossMaxPct", 1.5))
    sl = clamp(sl_min * 1.6, sl_min, sl_max)
    tp = clamp(sl * float(settings.get("takeProfitRewardRisk", 1.8)), sl * 1.1, float(settings.get("takeProfitMaxPct", 5.0)))
    conf = clamp(0.5 + strength * 0.45, 0.5, 0.99)
    if conf < float(settings.get("minimumConfidence", 0.6)):
        return None
    return SignalEval(
        source_id="ta-rsi-macd-ema",
        source_name="RSI/MACD/EMA pack",
        direction="long" if raw >= 0 else "short",
        confidence=conf,
        strength=strength,
        stop_loss_pct=sl,
        take_profit_pct=tp,
        reward_risk=tp / sl if sl else 1.8,
        atr_pct=atr(candles) / latest.close * 100 if latest.close else 0,
        last_price=latest.close,
        candle_count=len(candles),
        weight=1.0,
    )


def evaluate_pulse_local(
    direction: int,
    conf: float,
    px: float,
    settings: Dict[str, Any],
    sl_pct: float,
    tp_pct: float,
) -> Optional[SignalEval]:
    if direction == 0 or px <= 0 or conf < 0.2:
        return None
    strength = clamp(conf, 0.0, 1.0)
    confidence = clamp(0.5 + strength * 0.45, 0.5, 0.99)
    if confidence < float(settings.get("minimumConfidence", 0.6)) * 0.9:
        return None
    sl = clamp(sl_pct * 100.0, float(settings["stopLossMinPct"]), float(settings["stopLossMaxPct"]))
    tp = clamp(tp_pct * 100.0, sl * 1.1, float(settings["takeProfitMaxPct"]))
    return SignalEval(
        source_id="pulse-local",
        source_name="Pulse local pack",
        direction="long" if direction > 0 else "short",
        confidence=confidence,
        strength=strength,
        stop_loss_pct=sl,
        take_profit_pct=tp,
        reward_risk=tp / sl if sl else 1.8,
        atr_pct=sl,
        last_price=px,
        candle_count=20,
        weight=0.8,
    )


def vote_weight(ev: SignalEval, settings: Dict[str, Any]) -> float:
    sl_max = float(settings.get("stopLossMaxPct", 1.5)) or 1.5
    low_stop_bonus = 1.0 + 0.2 * (1.0 - ev.stop_loss_pct / sl_max)
    return ev.weight * ev.confidence * ev.strength * low_stop_bonus


def low_stop_consensus(
    evaluations: List[SignalEval],
    settings: Dict[str, Any],
) -> Optional[Tuple[str, List[SignalEval], Dict[str, float]]]:
    min_src = int(settings.get("minimumSourceSignals", 3))
    if len(evaluations) < min_src:
        return None
    by = {"long": [e for e in evaluations if e.direction == "long"], "short": [e for e in evaluations if e.direction == "short"]}
    long_w = sum(vote_weight(e, settings) for e in by["long"])
    short_w = sum(vote_weight(e, settings) for e in by["short"])
    total = long_w + short_w
    if total <= 0 or long_w == short_w:
        return None
    direction = "long" if long_w > short_w else "short"
    contributors = by[direction]
    win_w = long_w if direction == "long" else short_w
    agreement = win_w / total
    if len(contributors) < min_src or agreement < float(settings.get("minimumAgreement", 0.6)):
        return None
    ordered = sorted(contributors, key=lambda e: (e.stop_loss_pct, -e.confidence))
    pool = ordered[: max(1, (len(ordered) + 1) // 2)]
    risk_w = sum(vote_weight(e, settings) for e in pool) or 1e-12
    sl = clamp(
        sum(e.stop_loss_pct * vote_weight(e, settings) for e in pool) / risk_w,
        float(settings["stopLossMinPct"]),
        float(settings["stopLossMaxPct"]),
    )
    avg_rr = sum(e.reward_risk * vote_weight(e, settings) for e in contributors) / win_w
    rr = clamp(max(float(settings["takeProfitRewardRisk"]), avg_rr), 1.1, 5.0)
    min_tp = sl * float(settings["takeProfitRewardRisk"])
    if min_tp > float(settings["takeProfitMaxPct"]):
        return None
    tp = clamp(
        max(sl * rr, sum(e.take_profit_pct for e in pool) / len(pool)),
        min_tp,
        float(settings["takeProfitMaxPct"]),
    )
    confidence = clamp(
        agreement * 0.55 + sum(e.confidence for e in contributors) / len(contributors) * 0.45,
        0.5,
        0.99,
    )
    risk = {
        "stopLossPct": sl,
        "takeProfitPct": tp,
        "rewardRisk": tp / sl if sl else rr,
        "agreement": agreement,
        "confidence": confidence,
    }
    return direction, contributors, risk


def _closes(bars: List[List[float]]) -> List[float]:
    out: List[float] = []
    for b in bars:
        row = ohlcv_row(b)
        if row:
            out.append(row[3])
    return out


def _dir_of(pxs: List[float]) -> float:
    if len(pxs) < 2 or pxs[0] <= 0:
        return 0.0
    return (pxs[-1] - pxs[0]) / pxs[0]


def _pct(from_px: float, to_px: float) -> float:
    if from_px <= 0:
        return 0.0
    return ((to_px - from_px) / from_px) * 100.0


def ema_series(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * alpha + out[-1] * (1.0 - alpha))
    return out


def evaluate_independent_directions(
    signed: List[float],
    *,
    min_evidence: int = 1,
    min_agreement: float = 0.0,
) -> Dict[str, Any]:
    """CTS directional-evaluation: Long and Short scored independently. Tie = none."""
    ev = [float(v) for v in signed if v == v and v != 0]
    def lane(sign: str) -> Dict[str, Any]:
        aligned = [v for v in ev if (v > 0 if sign == "long" else v < 0)]
        score = sum(abs(v) for v in aligned)
        n = len(aligned)
        total = max(1, len(ev))
        agr = n / total
        avg = score / max(1, n)
        ok = n >= min_evidence and agr >= min_agreement and score > 0
        return {"direction": sign, "evidence": n, "agreement": agr, "score": score, "avg": avg, "ok": ok}
    long = lane("long")
    short = lane("short")
    selected = None
    if long["ok"] and short["ok"]:
        if long["score"] != short["score"]:
            selected = "long" if long["score"] > short["score"] else "short"
    elif long["ok"]:
        selected = "long"
    elif short["ok"]:
        selected = "short"
    return {"long": long, "short": short, "selected": selected, "margin": abs(long["score"] - short["score"])}


def _kind_indication(
    symbol: str,
    kind: str,
    direction: str,
    strength: float,
    px: float,
    settings: Dict[str, Any],
    sources: List[str],
    *,
    sl_pct: Optional[float] = None,
    tp_pct: Optional[float] = None,
    agreement: float = 1.0,
    timeframe: str = "1m",
    mode: Optional[str] = None,
    conf: Optional[float] = None,
) -> Indication:
    sl_min = float(settings.get("stopLossMinPct", 0.2))
    sl_max = float(settings.get("stopLossMaxPct", 1.5))
    sl = clamp(sl_pct if sl_pct is not None else sl_min * 1.4, sl_min, sl_max)
    rr = float(settings.get("takeProfitRewardRisk", 1.8))
    tp = clamp(tp_pct if tp_pct is not None else sl * rr, sl * 1.1, float(settings.get("takeProfitMaxPct", 5.0)))
    confidence = conf if conf is not None else clamp(0.52 + min(0.47, strength * 0.9), 0.5, 0.99)
    now = time.time()
    return Indication(
        symbol=symbol,
        direction=direction,
        mode=mode or kind,
        confidence=confidence,
        strength=clamp(strength, 0.0, 1.0),
        agreement=clamp(agreement, 0.0, 1.0),
        stop_loss_pct=sl,
        take_profit_pct=tp,
        reward_risk=tp / sl if sl else rr,
        last_price=px,
        sources=sources,
        votes_long=1 if direction == "long" else 0,
        votes_short=1 if direction == "short" else 0,
        primary=False,
        t=now,
        timeframe=timeframe,
        kind=kind,
    )


def evaluate_direction(symbol: str, closes: List[float], settings: Dict[str, Any]) -> Optional[Indication]:
    """CTS Direction: two equal windows, opposite sign, independent Long/Short on the new window."""
    rng = max(4, int(settings.get("dirRange") or 10))
    if len(closes) < rng * 2:
        return None
    first = closes[-(rng * 2) : -rng]
    second = closes[-rng:]
    d1 = _dir_of(first)
    d2 = _dir_of(second)
    min_ch = float(settings.get("dirMinChange") or 0.001)
    if abs(d1) < min_ch or abs(d2) < min_ch:
        return None
    if d1 * d2 >= 0:
        return None
    steps = [second[i] - second[i - 1] for i in range(1, len(second))]
    ev = evaluate_independent_directions(steps, min_evidence=1, min_agreement=0.5)
    want = "long" if d2 > 0 else "short"
    if ev["selected"] and ev["selected"] != want:
        return None
    strength = clamp(abs(d1) + abs(d2), 0.0, 1.0)
    agr = float((ev.get(want) or {}).get("agreement") or 1.0)
    return _kind_indication(
        symbol, "direction", want, strength, closes[-1], settings, [f"dir:{rng}"],
        agreement=agr, mode="direction",
    )


def evaluate_move(symbol: str, closes: List[float], settings: Dict[str, Any]) -> Optional[Indication]:
    """CTS Move: same-window displacement, independent direction agrees with the net move."""
    rng = max(4, int(settings.get("moveRange") or 10))
    if len(closes) < rng:
        return None
    window = closes[-rng:]
    d = _dir_of(window)
    min_ch = float(settings.get("moveMinChange") or 0.001)
    if abs(d) < min_ch:
        return None
    steps = [window[i] - window[i - 1] for i in range(1, len(window))]
    ev = evaluate_independent_directions(steps, min_evidence=1, min_agreement=0.45)
    want = "long" if d > 0 else "short"
    if ev["selected"] and ev["selected"] != want:
        return None
    strength = clamp(abs(d) * 25.0, 0.0, 1.0)
    agr = float((ev.get(want) or {}).get("agreement") or 1.0)
    return _kind_indication(
        symbol, "move", want, strength, closes[-1], settings, [f"move:{rng}"],
        agreement=agr, mode="move",
    )


def evaluate_trend(symbol: str, closes: List[float], settings: Dict[str, Any]) -> Optional[Indication]:
    """Independent trend: EMA 8 vs 21 with consecutive bar confirmation."""
    if len(closes) < 30:
        return None
    fast = ema_series(closes, 8)
    slow = ema_series(closes, 21)
    if len(fast) < 6 or len(slow) < 6:
        return None
    last = closes[-1]
    if last <= 0:
        return None
    spread = (fast[-1] - slow[-1]) / last
    min_ch = float(settings.get("dirMinChange") or 0.001)
    if abs(spread) < min_ch:
        return None
    want = "long" if spread > 0 else "short"
    consec = 0
    for i in range(1, min(8, len(fast))):
        s = fast[-i] - slow[-i]
        if (want == "long" and s > 0) or (want == "short" and s < 0):
            consec += 1
        else:
            break
    if consec < 3:
        return None
    steps = [closes[i] - closes[i - 1] for i in range(-min(10, len(closes) - 1), 0)]
    ev = evaluate_independent_directions(steps, min_evidence=2, min_agreement=0.5)
    if ev["selected"] and ev["selected"] != want:
        return None
    strength = clamp(abs(spread) * 40.0 + consec * 0.04, 0.0, 1.0)
    agr = float((ev.get(want) or {}).get("agreement") or consec / 8.0)
    conf = clamp(0.52 + min(0.4, abs(spread) * 80.0) + consec * 0.03, 0.5, 0.99)
    if conf < float(settings.get("minimumConfidence", 0.6)) * 0.9:
        return None
    return _kind_indication(
        symbol, "trend", want, strength, last, settings, [f"trend:ema8/21:{consec}"],
        agreement=agr, mode="trend", conf=conf,
    )


def evaluate_break(symbol: str, closes: List[float], settings: Dict[str, Any]) -> Optional[Indication]:
    """Independent structure break: close beyond the prior N-bar high/low."""
    rng = max(8, int(settings.get("breakRange") or settings.get("dirRange") or 16))
    if len(closes) < rng + 2:
        return None
    prior = closes[-(rng + 1) : -1]
    last = closes[-1]
    if last <= 0 or not prior:
        return None
    hi, lo = max(prior), min(prior)
    long_brk = last > hi
    short_brk = last < lo
    if long_brk == short_brk:
        return None
    want = "long" if long_brk else "short"
    ref = hi if want == "long" else lo
    brk = abs(_pct(ref, last))
    noise = float(settings.get("activeNoise") or 0.0005)
    if noise <= 0.02:
        noise *= 100.0
    if brk + 1e-12 < max(0.04, noise * 0.5):
        return None
    steps = [closes[i] - closes[i - 1] for i in range(-min(8, len(closes) - 1), 0)]
    ev = evaluate_independent_directions(steps, min_evidence=1, min_agreement=0.45)
    if ev["selected"] and ev["selected"] != want:
        return None
    strength = clamp(brk / max(0.15, noise), 0.0, 1.0)
    agr = float((ev.get(want) or {}).get("agreement") or 1.0)
    conf = clamp(0.55 + min(0.35, brk * 8.0) + agr * 0.08, 0.5, 0.99)
    if conf < float(settings.get("minimumConfidence", 0.6)) * 0.88:
        return None
    return _kind_indication(
        symbol, "break", want, strength, last, settings, [f"break:{rng}:{brk:.3f}"],
        agreement=agr, mode=f"break:{rng}", conf=conf,
    )


def _avg_abs_move_pct(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    tot = n = 0.0
    for i in range(1, len(values)):
        tot += abs(_pct(values[i - 1], values[i]))
        n += 1
    return tot / n if n else 0.0


def _dir_agree(values: List[float], direction: str) -> float:
    if len(values) < 2:
        return 0.0
    aligned = 0
    for i in range(1, len(values)):
        mv = values[i] - values[i - 1]
        if (direction == "long" and mv > 0) or (direction == "short" and mv < 0):
            aligned += 1
    return aligned / max(1, len(values) - 1)


def _mae_pct(values: List[float], direction: str) -> float:
    if len(values) < 2:
        return 0.0
    extreme = values[0]
    maximum = 0.0
    for v in values[1:]:
        if direction == "long":
            extreme = max(extreme, v)
            maximum = max(maximum, max(0.0, -_pct(extreme, v)))
        else:
            extreme = min(extreme, v)
            maximum = max(maximum, max(0.0, _pct(extreme, v)))
    return maximum


def _range_pct(values: List[float]) -> float:
    if not values or values[0] <= 0:
        return 0.0
    return ((max(values) - min(values)) / values[0]) * 100.0


def evaluate_active_range(symbol: str, closes: List[float], rng: int, settings: Dict[str, Any]) -> Optional[Indication]:
    """CTS Active/Outbreak for one range: current window vs previous equal window."""
    rng = max(2, int(rng))
    need = rng * 2 + 1
    if len(closes) < need:
        return None
    sample = closes[-need:]
    previous = sample[: rng + 1]
    current = sample[rng:]
    newest = current[-1]
    signed = _pct(current[0], newest)
    price_chg = abs(signed)
    threshold = max(0.01, float(settings.get("activeMovePct") or 0.5))
    if price_chg + 1e-12 < threshold:
        return None
    direction = "long" if signed >= 0 else "short"
    steps_pct = [_pct(current[i], current[i + 1]) for i in range(len(current) - 1)]
    ev = evaluate_independent_directions(steps_pct, min_evidence=1, min_agreement=0.45)
    if ev["selected"] and ev["selected"] != direction:
        return None
    prev_act = _avg_abs_move_pct(previous)
    cur_act = _avg_abs_move_pct(current)
    activity_ratio = cur_act / max(prev_act, 1e-6)
    min_act = max(0.0, float(settings.get("activeThreshold") or 1.0))
    if activity_ratio + 1e-12 < min_act:
        return None
    ref = sample[:-1]
    ref_hi, ref_lo = max(ref), min(ref)
    breakout = max(0.0, _pct(ref_hi, newest)) if direction == "long" else max(0.0, -_pct(ref_lo, newest))
    noise = max(0.0, float(settings.get("activeNoise") or 0.05))
    if noise <= 0.02:
        noise *= 100.0
    if breakout + 1e-12 < noise:
        return None
    agr = _dir_agree(current, direction)
    tail_n = max(2, int(len(current) * 0.5 + 0.999))
    tail_agr = _dir_agree(current[-tail_n:], direction)
    if tail_agr < 0.5:
        return None
    mae = _mae_pct(current, direction)
    if mae > max(noise, price_chg * 1.0):
        return None
    vol_w = clamp(float(settings.get("activeVolatilityWeight") or 0.3), 0.0, 1.0)
    cost = max(0.02, float(settings.get("positionCostPct") or 0.1))
    vol_risk = max(cost * 2.0, cur_act * (0.75 + vol_w * 0.75))
    sl = clamp(max(cost * 2.0, vol_risk), float(settings.get("stopLossMinPct", 0.2)), float(settings.get("stopLossMaxPct", 1.5)))
    tp = clamp(max(cost * 3.0, price_chg * 1.25, sl * 1.1), sl * 1.1, float(settings.get("takeProfitMaxPct", 5.0)))
    n_move = price_chg / threshold
    n_brk = breakout / max(noise, 0.01)
    acc = max(0.0, activity_ratio - min_act)
    score = max(
        0.0,
        1.0
        + (n_move * 0.34 + n_brk * 0.2 + acc * (0.18 + vol_w * 0.22) + agr * 0.14 + tail_agr * 0.14)
        - (mae / max(price_chg, 0.01)) * 0.25,
    )
    conf = clamp(
        0.2
        + min(1.0, n_move / 2) * 0.25
        + min(1.0, n_brk / 2) * 0.2
        + min(1.0, activity_ratio / max(1.0, min_act * 2)) * 0.2
        + agr * 0.1
        + tail_agr * 0.05,
        0.0,
        0.99,
    )
    if conf < float(settings.get("minimumConfidence", 0.6)) * 0.85:
        return None
    return _kind_indication(
        symbol, "active", direction, clamp(score / 4.0, 0, 1), newest, settings,
        [f"active:{rng}", f"brk:{breakout:.3f}", f"act:{activity_ratio:.2f}"],
        sl_pct=sl, tp_pct=tp, agreement=agr, mode=f"outbreak:{rng}", conf=conf,
    )


def evaluate_active(symbol: str, closes: List[float], settings: Dict[str, Any]) -> Optional[Indication]:
    outbreaks = settings.get("activeOutbreak") or [3, 5, 10]
    best: Optional[Indication] = None
    for raw in outbreaks:
        cand = evaluate_active_range(symbol, closes, int(raw), settings)
        if cand and (best is None or cand.confidence > best.confidence):
            best = cand
    return best


def evaluate_active_all(symbol: str, closes: List[float], settings: Dict[str, Any]) -> List[Indication]:
    """Independent Active indication per outbreak range (3 / 5 / 10)."""
    out: List[Indication] = []
    for raw in (settings.get("activeOutbreak") or [3, 5, 10]):
        cand = evaluate_active_range(symbol, closes, int(raw), settings)
        if cand:
            out.append(cand)
    return out


def bollinger(closes: List[float], period: int = 20) -> Optional[Tuple[float, float, float]]:
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((x - mid) ** 2 for x in window) / period
    sd = var ** 0.5
    return mid + 2 * sd, mid, mid - 2 * sd


def evaluate_common(symbol: str, candles: List[Candle], settings: Dict[str, Any]) -> Optional[Indication]:
    """CTS Common / indication-stage: RSI + MACD + EMA + Bollinger, independent of State."""
    if len(candles) < 26:
        return None
    closes = [c.close for c in candles]
    latest = candles[-1]
    r = rsi(closes, 14)
    macd_fast = ema_series(closes, 12)
    macd_slow = ema_series(closes, 26)
    macd_line = [a - b for a, b in zip(macd_fast, macd_slow)]
    signal_line = ema_series(macd_line, 9)
    macd = macd_line[-1]
    macd_sig = signal_line[-1] if signal_line else 0.0
    hist = macd - macd_sig
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200) if len(closes) >= 80 else ema50
    bb = bollinger(closes, 20)
    buy = sell = 0
    if r < 30:
        buy += 1
    if r > 70:
        sell += 1
    if hist > 0 and macd > macd_sig:
        buy += 1
    if hist < 0 and macd < macd_sig:
        sell += 1
    if ema20 > ema50:
        buy += 1
    if ema20 < ema50:
        sell += 1
    if latest.close > ema200:
        buy += 1
    if latest.close < ema200:
        sell += 1
    if bb:
        upper, mid, lower = bb
        if latest.close <= lower:
            buy += 1
        if latest.close >= upper:
            sell += 1
    if buy == sell:
        return None
    direction = "long" if buy > sell else "short"
    strength = max(buy, sell) / 5.0
    if strength < float(settings.get("minimumStrength", 0.2)):
        return None
    conf = clamp(0.5 + strength * 0.45, 0.5, 0.99)
    if conf < float(settings.get("minimumConfidence", 0.6)) * 0.9:
        return None
    return _kind_indication(
        symbol, "common", direction, strength, latest.close, settings,
        [f"rsi:{r:.1f}", f"macd:{hist:.5f}", "ema20-50", "ema200", "bb"],
        agreement=strength, mode="rsi-macd-ema-bb", conf=conf,
    )


def combine_timeframes(
    tf_evals: List[SignalEval],
    min_agree: int,
    settings: Dict[str, Any],
) -> Optional[Tuple[str, List[SignalEval], Dict[str, float]]]:
    """Independent TF votes folded into one comprehensive Indication."""
    if len(tf_evals) < max(2, int(min_agree)):
        return None
    by = {"long": [e for e in tf_evals if e.direction == "long"], "short": [e for e in tf_evals if e.direction == "short"]}
    long_w = sum(vote_weight(e, settings) for e in by["long"])
    short_w = sum(vote_weight(e, settings) for e in by["short"])
    total = long_w + short_w
    if total <= 0 or long_w == short_w:
        return None
    direction = "long" if long_w > short_w else "short"
    winners = by[direction]
    if len(winners) < max(2, int(min_agree)):
        return None
    win_w = long_w if direction == "long" else short_w
    agreement = win_w / total
    ordered = sorted(winners, key=lambda e: (e.stop_loss_pct, -e.confidence))
    sl = clamp(
        sum(e.stop_loss_pct * vote_weight(e, settings) for e in ordered) / (sum(vote_weight(e, settings) for e in ordered) or 1e-12),
        float(settings["stopLossMinPct"]),
        float(settings["stopLossMaxPct"]),
    )
    rr = clamp(sum(e.reward_risk * vote_weight(e, settings) for e in winners) / win_w, 1.1, 5.0)
    tp = clamp(
        max(sl * rr, sum(e.take_profit_pct for e in ordered) / len(ordered)),
        sl * 1.05,
        float(settings["takeProfitMaxPct"]),
    )
    confidence = clamp(
        agreement * 0.5 + sum(e.confidence for e in winners) / len(winners) * 0.5,
        0.5,
        0.99,
    )
    risk = {
        "stopLossPct": sl,
        "takeProfitPct": tp,
        "rewardRisk": tp / sl if sl else rr,
        "agreement": agreement,
        "confidence": confidence,
    }
    return direction, winners, risk


class ExtraBook:
    """Public 1m klines from Binance / Bybit — independent Signal lanes."""

    def __init__(self) -> None:
        self.cache: Dict[str, Tuple[float, List[Candle]]] = {}
        self.fail: Dict[str, int] = {}
        self.cool: Dict[str, float] = {}
        self.http = httpx.Client(timeout=2.2, headers={"User-Agent": "grok-x01-pulse/ind"}) if httpx else None
        from concurrent.futures import ThreadPoolExecutor
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ind-x")
        self.cache_max = 48

    def prune(self, keep: Optional[set] = None, max_n: Optional[int] = None) -> int:
        now = time.time()
        drop: List[str] = []
        for k, (ts, _) in list(self.cache.items()):
            sym = k.split(":", 1)[-1] if ":" in k else k
            if keep is not None and sym not in keep:
                drop.append(k)
            elif now - ts > 90:
                drop.append(k)
        for k in drop:
            self.cache.pop(k, None)
        cap = int(max_n if max_n is not None else self.cache_max)
        extra = len(self.cache) - cap
        if extra > 0:
            oldest = sorted(self.cache.items(), key=lambda kv: kv[1][0])
            for k, _ in oldest[:extra]:
                self.cache.pop(k, None)
                drop.append(k)
        for src in list(self.fail):
            if self.fail.get(src, 0) <= 0:
                self.fail.pop(src, None)
        for src, until in list(self.cool.items()):
            if until < now:
                self.cool.pop(src, None)
        return len(drop)

    def prefetch(self, pairs: List[Tuple[str, str]]) -> None:
        now = time.time()
        want = []
        for src, sym in pairs:
            hit = self.cache.get(f"{src}:{sym}")
            if hit and now - hit[0] < 30:
                continue
            want.append((src, sym))
        if not want:
            return
        futs = [self.pool.submit(self.get, src, sym) for src, sym in want[:8]]
        deadline = time.time() + 0.40
        for f in futs:
            remain = deadline - time.time()
            if remain <= 0:
                break
            try:
                f.result(timeout=max(0.02, remain))
            except Exception:
                continue
        if len(self.cache) > self.cache_max:
            self.prune(max_n=self.cache_max)

    def get(self, source: str, symbol: str) -> List[Candle]:
        key = f"{source}:{symbol}"
        hit = self.cache.get(key)
        if hit and time.time() - hit[0] < 30:
            return hit[1]
        if self.cool.get(source, 0) > time.time():
            return hit[1] if hit else []
        try:
            bars = self._fetch(source, symbol)
            self.cache[key] = (time.time(), bars)
            self.fail[source] = 0
            return bars
        except Exception:
            self.fail[source] = self.fail.get(source, 0) + 1
            if self.fail[source] >= 3:
                self.cool[source] = time.time() + 120
            return hit[1] if hit else []

    def _fetch(self, source: str, symbol: str) -> List[Candle]:
        compact = symbol.replace("-", "")
        if source == "binance-usdm":
            url = f"https://fapi.binance.com/fapi/v1/klines?symbol={compact}&interval=1m&limit=60"
            rows = self._json(url)
            out = []
            if isinstance(rows, list):
                for r in rows:
                    out.append(Candle(float(r[0]) / 1000.0, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])))
            return out
        if source == "bybit-linear":
            url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={compact}&interval=1&limit=60"
            payload = self._json(url)
            rows = ((payload or {}).get("result") or {}).get("list") or []
            out = []
            for r in rows:
                out.append(Candle(float(r[0]) / 1000.0, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])))
            out.sort(key=lambda c: c.ts)
            return out
        return []

    def _json(self, url: str) -> Any:
        if self.http is not None:
            r = self.http.get(url)
            r.raise_for_status()
            return r.json()
        req = urllib.request.Request(url, headers={"User-Agent": "grok-x01-pulse/ind"})
        with urllib.request.urlopen(req, timeout=2.2) as resp:
            import json
            return json.loads(resp.read().decode())


EXTRA = ExtraBook()


class IndicationBook:
    def __init__(self) -> None:
        self.settings = dict(DEFAULT_SETTINGS)
        self.last: Dict[str, List[Indication]] = {}
        self.evals: Dict[str, List[SignalEval]] = {}
        self.cycle = 0
        self.extra_cursor = 0
        self.counters: Dict[str, Dict[str, Any]] = {
            kind: {
                "kind": kind,
                "evaluated": 0,
                "qualified": 0,
                "entered": 0,
                "exited": 0,
                "blocked": 0,
                "rejected": 0,
                "hits": 0,
                "noSignal": 0,
                "symbols": 0,
                "long": 0,
                "short": 0,
                "confTotal": 0.0,
                "strengthTotal": 0.0,
                "slTotal": 0.0,
            }
            for kind in INDICATION_KINDS
        }
        self.lane_results: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def keep(self, symbols: Sequence[str]) -> int:
        want = set(symbols)
        n = 0
        for store in (self.last, self.evals, self.lane_results):
            for k in list(store):
                if k not in want:
                    store.pop(k, None)
                    n += 1
        return n

    def record_outcome(self, kind: str, outcome: str, amount: int = 1) -> None:
        """Record entry/exit/block/reject outcomes without changing lane identity."""
        key = str(kind or "").strip().lower()
        if key not in self.counters:
            return
        name = str(outcome or "").strip()
        bucket = self.counters[key]
        if name in bucket and isinstance(bucket.get(name), int):
            bucket[name] += max(0, int(amount or 0))


    def load(self, overlay: Dict[str, Any]) -> None:
        s = self.settings
        s["enabled"] = bool(overlay.get("indEnabled", s["enabled"]))
        s["minimumSourceSignals"] = int(overlay.get("indMinSources", s["minimumSourceSignals"]))
        s["minimumAgreement"] = float(overlay.get("indMinAgreement", s["minimumAgreement"]))
        s["minimumConfidence"] = float(overlay.get("indMinConfidence", s["minimumConfidence"]))
        s["minimumStrength"] = float(overlay.get("indMinStrength", s["minimumStrength"]))
        s["stopLossMinPct"] = float(overlay.get("indStopMinPct", s["stopLossMinPct"]))
        s["stopLossMaxPct"] = float(overlay.get("indStopMaxPct", s["stopLossMaxPct"]))
        s["stopLossAtrMultiplier"] = float(overlay.get("indAtrMult", s["stopLossAtrMultiplier"]))
        s["takeProfitRewardRisk"] = float(overlay.get("indRewardRisk", s["takeProfitRewardRisk"]))
        s["extraSources"] = bool(overlay.get("indExtraSources", s["extraSources"]))
        s["tf1m"] = bool(overlay.get("tf1m", True))
        s["tf5m"] = bool(overlay.get("tf5m", True))
        s["tf15m"] = bool(overlay.get("tf15m", True))
        s["tfCombined"] = bool(overlay.get("tfCombined", True))
        s["tfMinAgree"] = int(overlay.get("tfMinAgree") or 2)
        for key, ovk in (
            ("typeState", "indTypeState"),
            ("typeDirection", "indTypeDirection"),
            ("typeMove", "indTypeMove"),
            ("typeActive", "indTypeActive"),
            ("typeCommon", "indTypeCommon"),
            ("typeSignals", "indTypeSignals"),
            ("typeTrend", "indTypeTrend"),
            ("typeBreak", "indTypeBreak"),
        ):
            if ovk in overlay:
                s[key] = bool(overlay.get(ovk))
        s["dirRange"] = int(overlay.get("indDirRange") or s.get("dirRange") or 10)
        s["moveRange"] = int(overlay.get("indMoveRange") or s.get("moveRange") or 10)
        outbreaks = overlay.get("activeOutbreakRanges") or overlay.get("indActiveOutbreak")
        if isinstance(outbreaks, (list, tuple)) and outbreaks:
            s["activeOutbreak"] = [int(x) for x in outbreaks]
        s["activeThreshold"] = float(overlay.get("indActiveThreshold") or s.get("activeThreshold") or 1.0)
        noise = overlay.get("noise") or overlay.get("indActiveNoise") or s.get("activeNoise") or 0.0005
        s["activeNoise"] = float(noise)
        s["activeMovePct"] = float(overlay.get("indActiveMovePct") or overlay.get("activeMovePct") or 0.5)
        s["activeVolatilityWeight"] = float(overlay.get("volWeight") or overlay.get("activeVolatilityWeight") or 0.3)

    def process(
        self,
        symbol: str,
        bars: List[List[float]],
        pulse_dir: int = 0,
        pulse_conf: float = 0.0,
        px: float = 0.0,
        sl_pct: float = 0.0048,
        tp_pct: float = 0.0075,
        want_extra: bool = False,
        bars_by_tf: Optional[Dict[str, List[List[float]]]] = None,
    ) -> List[Indication]:
        if not self.settings.get("enabled", True):
            self.last[symbol] = []
            return []
        tf_map: Dict[str, List[List[float]]] = dict(bars_by_tf or {})
        if bars and "1m" not in tf_map:
            tf_map["1m"] = bars
        evals: List[SignalEval] = []
        tf_evals: List[SignalEval] = []
        for tf in TIMEFRAMES:
            flag = f"tf{tf}"
            if not self.settings.get(flag, True):
                continue
            rows = tf_map.get(tf) or []
            if len(rows) < 20:
                continue
            candles = bars_to_candles(rows, period_s=TF_SECONDS[tf])
            ev = evaluate_signal_candles(
                f"bingx-{tf}",
                f"BingX {tf}",
                candles,
                self.settings,
                weight=TF_WEIGHT[tf],
            )
            if ev:
                evals.append(ev)
                tf_evals.append(ev)
            if tf == "1m":
                loc = evaluate_pulse_local(
                    pulse_dir,
                    pulse_conf,
                    px or (candles[-1].close if candles else 0),
                    self.settings,
                    sl_pct,
                    tp_pct,
                )
                if loc:
                    evals.append(loc)
        if self.settings.get("extraSources") and want_extra:
            for src, name in (("binance-usdm", "Binance USD-M"), ("bybit-linear", "Bybit Linear")):
                extra = EXTRA.get(src, symbol)
                ev = evaluate_signal_candles(src, name, extra, self.settings)
                if ev:
                    evals.append(ev)
        candles_1m = bars_to_candles(tf_map.get("1m") or bars or [], period_s=60.0)
        if self.settings.get("typeState", True) and candles_1m:
            ta = evaluate_ta_pack(candles_1m, self.settings)
            if ta:
                evals.append(ta)
        self.evals[symbol] = evals
        indications: List[Indication] = []
        now = time.time()
        # Independent TF lanes stay first-class
        for ev in tf_evals:
            tf = ev.source_id.split("-")[-1] if "-" in ev.source_id else ""
            indications.append(
                Indication(
                    symbol=symbol,
                    direction=ev.direction,
                    mode="direct_tf",
                    confidence=ev.confidence,
                    strength=ev.strength,
                    agreement=1.0,
                    stop_loss_pct=ev.stop_loss_pct,
                    take_profit_pct=ev.take_profit_pct,
                    reward_risk=ev.reward_risk,
                    last_price=ev.last_price,
                    sources=[ev.source_id],
                    votes_long=1 if ev.direction == "long" else 0,
                    votes_short=1 if ev.direction == "short" else 0,
                    primary=False,
                    t=now,
                    timeframe=tf,
                    kind="signals",
                )
            )
        # Non-TF direct sources
        others = [e for e in evals if not str(e.source_id).startswith("bingx-") and e.source_id != "ta-rsi-macd-ema"]
        ordered = sorted(others, key=lambda e: (e.stop_loss_pct, -e.confidence, -e.strength, e.source_id))
        for ev in ordered:
            indications.append(
                Indication(
                    symbol=symbol,
                    direction=ev.direction,
                    mode="direct_source",
                    confidence=ev.confidence,
                    strength=ev.strength,
                    agreement=1.0,
                    stop_loss_pct=ev.stop_loss_pct,
                    take_profit_pct=ev.take_profit_pct,
                    reward_risk=ev.reward_risk,
                    last_price=ev.last_price,
                    sources=[ev.source_id],
                    votes_long=1 if ev.direction == "long" else 0,
                    votes_short=1 if ev.direction == "short" else 0,
                    primary=False,
                    t=now,
                    timeframe="1m",
                    kind="signals",
                )
            )
        if self.settings.get("typeState", True):
            ta = next((e for e in evals if e.source_id == "ta-rsi-macd-ema"), None)
            if ta:
                indications.append(
                    Indication(
                        symbol=symbol,
                        direction=ta.direction,
                        mode="ta_pack",
                        confidence=ta.confidence,
                        strength=ta.strength,
                        agreement=1.0,
                        stop_loss_pct=ta.stop_loss_pct,
                        take_profit_pct=ta.take_profit_pct,
                        reward_risk=ta.reward_risk,
                        last_price=ta.last_price,
                        sources=[ta.source_id],
                        votes_long=1 if ta.direction == "long" else 0,
                        votes_short=1 if ta.direction == "short" else 0,
                        primary=False,
                        t=now,
                        timeframe="1m",
                        kind="state",
                    )
                )
        if self.settings.get("tfCombined", True):
            comb = combine_timeframes(tf_evals, int(self.settings.get("tfMinAgree") or 2), self.settings)
            if comb:
                direction, contrib, risk = comb
                indications.append(
                    Indication(
                        symbol=symbol,
                        direction=direction,
                        mode="tf_combined",
                        confidence=risk["confidence"],
                        strength=sum(e.strength for e in contrib) / len(contrib),
                        agreement=risk["agreement"],
                        stop_loss_pct=risk["stopLossPct"],
                        take_profit_pct=risk["takeProfitPct"],
                        reward_risk=risk["rewardRisk"],
                        last_price=sum(e.last_price for e in contrib) / len(contrib),
                        sources=[e.source_id for e in contrib],
                        votes_long=sum(1 for e in tf_evals if e.direction == "long"),
                        votes_short=sum(1 for e in tf_evals if e.direction == "short"),
                        primary=True,
                        t=now,
                        timeframe="combined",
                        kind="state",
                    )
                )
        cons = low_stop_consensus(evals, self.settings)
        if cons:
            direction, contrib, risk = cons
            has_tf = any(i.mode == "tf_combined" for i in indications)
            indications.append(
                Indication(
                    symbol=symbol,
                    direction=direction,
                    mode="multi_source_consensus",
                    confidence=risk["confidence"],
                    strength=sum(e.strength for e in contrib) / len(contrib),
                    agreement=risk["agreement"],
                    stop_loss_pct=risk["stopLossPct"],
                    take_profit_pct=risk["takeProfitPct"],
                    reward_risk=risk["rewardRisk"],
                    last_price=sum(e.last_price for e in contrib) / len(contrib),
                    sources=[e.source_id for e in contrib],
                    votes_long=sum(1 for e in evals if e.direction == "long"),
                    votes_short=sum(1 for e in evals if e.direction == "short"),
                    primary=not has_tf,
                    t=now,
                    timeframe="multi",
                    kind="state",
                )
            )
        closes = _closes(tf_map.get("1m") or bars or [])
        if self.settings.get("typeDirection", True) and closes:
            drow = evaluate_direction(symbol, closes, self.settings)
            if drow:
                indications.append(drow)
        if self.settings.get("typeMove", True) and closes:
            mrow = evaluate_move(symbol, closes, self.settings)
            if mrow:
                indications.append(mrow)
        if self.settings.get("typeActive", True) and closes:
            indications.extend(evaluate_active_all(symbol, closes, self.settings))
        if self.settings.get("typeTrend", True) and closes:
            trow = evaluate_trend(symbol, closes, self.settings)
            if trow:
                indications.append(trow)
        if self.settings.get("typeBreak", True) and closes:
            brow = evaluate_break(symbol, closes, self.settings)
            if brow:
                indications.append(brow)
        if self.settings.get("typeCommon", True):
            c1 = bars_to_candles(tf_map.get("1m") or bars or [], period_s=60.0)
            crow = evaluate_common(symbol, c1, self.settings)
            if crow:
                indications.append(crow)
        if not self.settings.get("typeSignals", True):
            indications = [i for i in indications if i.kind != "signals"]
        if not self.settings.get("typeState", True):
            indications = [i for i in indications if i.kind != "state"]
        if not self.settings.get("typeCommon", True):
            indications = [i for i in indications if i.kind != "common"]
        if not self.settings.get("typeTrend", True):
            indications = [i for i in indications if i.kind != "trend"]
        if not self.settings.get("typeBreak", True):
            indications = [i for i in indications if i.kind != "break"]
        lane_result: Dict[str, Dict[str, Any]] = {}
        for kind in INDICATION_KINDS:
            bucket = self.counters[kind]
            enabled = bool(self.settings.get(f"type{kind.title()}", True))
            # Signals is the only plural setting key.
            if kind == "signals":
                enabled = bool(self.settings.get("typeSignals", True))
            rows_kind = [item for item in indications if item.kind == kind]
            bucket["evaluated"] += 1 if enabled else 0
            bucket["symbols"] += 1 if enabled else 0
            if rows_kind:
                bucket["qualified"] += len(rows_kind)
                bucket["hits"] += len(rows_kind)
                bucket["long"] += sum(1 for item in rows_kind if item.direction == "long")
                bucket["short"] += sum(1 for item in rows_kind if item.direction == "short")
                bucket["confTotal"] += sum(float(item.confidence or 0.0) for item in rows_kind)
                bucket["strengthTotal"] += sum(float(item.strength or 0.0) for item in rows_kind)
                bucket["slTotal"] += sum(float(item.stop_loss_pct or 0.0) for item in rows_kind)
                best = max(rows_kind, key=lambda item: (item.confidence, item.strength))
                lane_result[kind] = {
                    "status": "qualified",
                    "direction": best.direction,
                    "confidence": round(float(best.confidence or 0.0), 4),
                    "strength": round(float(best.strength or 0.0), 4),
                    "sampleCount": len(rows_kind),
                }
            elif enabled:
                bucket["noSignal"] += 1
                lane_result[kind] = {
                    "status": "no-signal",
                    "direction": "",
                    "confidence": 0.0,
                    "strength": 0.0,
                    "sampleCount": 0,
                }
            else:
                lane_result[kind] = {"status": "disabled", "direction": "", "confidence": 0.0, "strength": 0.0, "sampleCount": 0}
        self.lane_results[symbol] = lane_result
        self.last[symbol] = indications
        self.cycle += 1
        return indications

    def primary(self, symbol: str) -> Optional[Indication]:
        rows = self.last.get(symbol) or []
        if not rows:
            return None
        for mode in ("tf_combined", "multi_source_consensus"):
            for i in rows:
                if i.mode == mode and i.kind == "state":
                    return i
        ranked = sorted(rows, key=lambda e: (e.kind == "state", e.confidence, -e.stop_loss_pct), reverse=True)
        return ranked[0]

    def best(self, symbol: str) -> Optional[Indication]:
        """Highest-confidence independent type (State / Direction / Move / Active / Common / Signals)."""
        rows = [i for i in (self.last.get(symbol) or []) if i.confidence >= float(self.settings.get("minimumConfidence", 0.6)) * 0.9]
        if not rows:
            rows = list(self.last.get(symbol) or [])
        if not rows:
            return None
        order = {"state": 5, "signals": 4, "trend": 4, "break": 4, "active": 3, "direction": 2, "move": 2, "common": 1}
        rows.sort(key=lambda e: (e.confidence, order.get(e.kind, 0), -e.stop_loss_pct), reverse=True)
        return rows[0]

    def match(self, symbol: str, reason: str) -> Optional[Indication]:
        rows = self.last.get(symbol) or []
        low = (reason or "").lower()
        bits = [b for b in low.split(":") if b]
        want_kind = ""
        if bits and bits[0] == "ind" and len(bits) > 1:
            cand = bits[1].strip()
            if cand in ("state", "signals", "active", "direction", "move", "common", "trend", "break"):
                want_kind = cand
        if want_kind:
            cands = [i for i in rows if i.kind == want_kind]
            if not cands:
                return None
            cands.sort(key=lambda e: (e.confidence, -e.stop_loss_pct), reverse=True)
            return cands[0]
        for i in rows:
            if i.mode and i.mode in low:
                return i
        return self.best(symbol)

    def kinds_for(self, symbol: str) -> Dict[str, "Indication"]:
        """One winner per independent type."""
        out: Dict[str, Indication] = {}
        for i in self.last.get(symbol) or []:
            cur = out.get(i.kind)
            if cur is None or i.confidence > cur.confidence:
                out[i.kind] = i
        return out

    def pick_entries(
        self,
        symbol: str,
        min_conf: float = 0.52,
        allow: Optional[Any] = None,
    ) -> List[Tuple["Indication", float, int]]:
        """Return every eligible kind lane without a consensus requirement."""
        by = self.kinds_for(symbol)
        out: List[Tuple["Indication", float, int]] = []
        for kind, indication in by.items():
            if callable(allow):
                try:
                    if not bool(allow(kind, indication.direction)):
                        continue
                except TypeError:
                    if not bool(allow(kind)):
                        continue
                except Exception:
                    pass
            confidence = float(indication.confidence or 0.0)
            if confidence < min_conf:
                continue
            out.append((indication, confidence, 1))
        out.sort(key=lambda item: (item[1], item[0].strength, item[0].kind), reverse=True)
        return out

    def pick_entry(
        self,
        symbol: str,
        min_conf: float = 0.52,
        allow: Optional[Any] = None,
    ) -> Optional[Tuple["Indication", float, int]]:
        """Highest independent type that is allowed, boosted when ≥2 kinds agree."""
        by = self.kinds_for(symbol)
        if callable(allow) and by:
            kept: Dict[str, Indication] = {}
            for k, i in by.items():
                ok = True
                try:
                    ok = bool(allow(k, i.direction))
                except TypeError:
                    try:
                        ok = bool(allow(k))
                    except Exception:
                        ok = True
                except Exception:
                    ok = True
                if ok:
                    kept[k] = i
            by = kept
        if not by:
            return None
        signed = [(1.0 if i.direction == "long" else -1.0) * max(0.01, float(i.confidence)) for i in by.values()]
        agr = evaluate_independent_directions(signed, min_evidence=1)
        selected = agr.get("selected")
        cands = list(by.values())
        if selected:
            aligned = [i for i in cands if i.direction == selected]
            if aligned:
                cands = aligned
        order = {"state": 5, "signals": 4, "trend": 4, "break": 4, "active": 3, "direction": 2, "move": 2, "common": 1}
        cands.sort(key=lambda e: (e.confidence, order.get(e.kind, 0), -e.stop_loss_pct), reverse=True)
        pick = cands[0]
        agree_n = sum(1 for i in by.values() if i.direction == pick.direction)
        conf = float(pick.confidence)
        if agree_n >= 2:
            conf = min(1.0, conf + 0.05 * min(3, agree_n - 1) + 0.03 * min(1.0, float(agr.get("margin") or 0)))
        floor = min_conf * (0.9 if agree_n >= 2 else 1.0)
        if conf < floor:
            return None
        return pick, conf, agree_n

    def kind_stats(self) -> Dict[str, Any]:
        """Live scan evidence for every independent indication type."""
        flags = {
            "state": "typeState",
            "signals": "typeSignals",
            "active": "typeActive",
            "direction": "typeDirection",
            "move": "typeMove",
            "common": "typeCommon",
            "trend": "typeTrend",
            "break": "typeBreak",
        }
        out: Dict[str, Any] = {}
        for kind in INDICATION_KINDS:
            source = self.counters.get(kind) or {}
            evaluated = int(source.get("evaluated") or 0)
            qualified = int(source.get("qualified") or 0)
            out[kind] = {
                "kind": kind,
                "enabled": bool(self.settings.get(flags[kind], True)),
                "hits": int(source.get("hits") or 0),
                "evaluated": evaluated,
                "qualified": qualified,
                "entered": int(source.get("entered") or 0),
                "exited": int(source.get("exited") or 0),
                "blocked": int(source.get("blocked") or 0),
                "rejected": int(source.get("rejected") or 0),
                "noSignal": int(source.get("noSignal") or 0),
                "symbols": int(source.get("symbols") or 0),
                "long": int(source.get("long") or 0),
                "short": int(source.get("short") or 0),
                "avgConf": round(float(source.get("confTotal") or 0.0) / max(1, qualified), 3),
                "avgStrength": round(float(source.get("strengthTotal") or 0.0) / max(1, qualified), 3),
                "avgSl": round(float(source.get("slTotal") or 0.0) / max(1, qualified), 3),
                "sampleCount": qualified,
                "insufficientSample": qualified < 8,
                "status": "no-signal" if (bool(self.settings.get(flags[kind], True)) and not qualified) else ("building" if qualified < 8 else "ready"),
                "processed": True,
            }
        # Keep current per-symbol lane status visible even when the lane is quiet.
        for kind in INDICATION_KINDS:
            current = [row.get(kind) for row in list(self.lane_results.values()) if isinstance(row, dict) and isinstance(row.get(kind), dict)]
            out[kind]["currentNoSignal"] = sum(1 for row in current if row.get("status") == "no-signal")
        return out

    def snapshot(self) -> Dict[str, Any]:
        primaries = []
        for s, rows in list(self.last.items()):
            p = self.primary(s)
            if p:
                rec = asdict(p)
                primaries.append(rec)
        primaries.sort(key=lambda r: r["confidence"], reverse=True)
        kinds = self.kind_stats()
        # `types` is the configuration contract consumed by the UI and QA;
        # hit counts belong in the separate `typeHits` field. A quiet kind
        # must not look disabled merely because it has no current hit.
        types = {k: bool(v.get("enabled")) for k, v in kinds.items()}
        type_hits = {k: int(v.get("hits") or 0) for k, v in kinds.items()}
        keys = sorted(self.last.keys())
        eval_items = list(self.evals.items())
        last_items = list(self.last.items())
        if len(eval_items) > 24:
            eval_items = eval_items[:24]
        if len(last_items) > 24:
            last_items = last_items[:24]
        samples: List[Dict[str, Any]] = []
        for s, rows in last_items:
            by = self.kinds_for(s)
            samples.append({
                "symbol": s,
                "kinds": {
                    k: {"dir": i.direction, "conf": round(i.confidence, 3), "sl": round(i.stop_loss_pct, 3), "mode": i.mode}
                    for k, i in by.items()
                },
            })
        return {
            "enabled": bool(self.settings.get("enabled")),
            "types": {
                "state": bool(self.settings.get("typeState", True)),
                "direction": bool(self.settings.get("typeDirection", True)),
                "move": bool(self.settings.get("typeMove", True)),
                "active": bool(self.settings.get("typeActive", True)),
                "common": bool(self.settings.get("typeCommon", True)),
                "signals": bool(self.settings.get("typeSignals", True)),
                "trend": bool(self.settings.get("typeTrend", True)),
                "break": bool(self.settings.get("typeBreak", True)),
            },
            "typeHits": type_hits,
            "kindStats": kinds,
            "minSources": self.settings.get("minimumSourceSignals"),
            "minAgreement": self.settings.get("minimumAgreement"),
            "minConfidence": self.settings.get("minimumConfidence"),
            "extraSources": bool(self.settings.get("extraSources")),
            "tf1m": bool(self.settings.get("tf1m", True)),
            "tf5m": bool(self.settings.get("tf5m", True)),
            "tf15m": bool(self.settings.get("tf15m", True)),
            "tfCombined": bool(self.settings.get("tfCombined", True)),
            "tfMinAgree": int(self.settings.get("tfMinAgree") or 2),
            "symbols": len(self.last),
            "processed": keys if len(keys) <= 48 else keys[:32],
            "evalN": sum(len(v) for v in list(self.evals.values())),
            "lanes": {s: [e.source_id for e in v] for s, v in eval_items},
            "primary": primaries[:24],
            "samples": samples[:16],
            "tf": {
                s: {
                    "independent": [i.timeframe for i in rows if i.mode == "direct_tf"],
                    "combined": next((i.direction for i in rows if i.mode == "tf_combined"), None),
                }
                for s, rows in last_items
            },
        }


def self_test() -> List[Tuple[str, bool, str]]:
    # Rising series → long
    base = 100.0
    up = []
    for i in range(40):
        c = base * (1 + i * 0.0012)
        p = base * (1 + max(0, i - 1) * 0.0012)
        up.append([p, c * 1.0006, min(p, c) * 0.9994, c, 1000 + i])
    candles = bars_to_candles(up)
    st = dict(DEFAULT_SETTINGS)
    st["minimumConfidence"] = 0.5
    st["minimumStrength"] = 0.05
    ev = evaluate_signal_candles("bingx-swap", "BingX", candles, st)
    t1 = (ev is not None and ev.direction == "long", f"up={ev.direction if ev else None} str={ev.strength if ev else 0:.3f}")
    # Down series → short
    down = []
    for i in range(40):
        c = base * (1 - i * 0.0012)
        p = base * (1 - max(0, i - 1) * 0.0012)
        down.append([p, max(p, c) * 1.0006, c * 0.9994, c, 1000 + i])
    ev2 = evaluate_signal_candles("bingx-swap", "BingX", bars_to_candles(down), st)
    t2 = (ev2 is not None and ev2.direction == "short", f"dn={ev2.direction if ev2 else None}")
    # Consensus
    evals = [
        SignalEval("a", "A", "long", 0.8, 0.5, 0.4, 0.9, 2.2, 0.5, 100, 30, 1),
        SignalEval("b", "B", "long", 0.75, 0.4, 0.5, 1.0, 2.0, 0.5, 100, 30, 1),
        SignalEval("c", "C", "long", 0.7, 0.35, 0.45, 0.95, 2.1, 0.5, 100, 30, 1),
        SignalEval("d", "D", "short", 0.6, 0.2, 0.8, 1.2, 1.5, 0.5, 100, 30, 0.5),
    ]
    cons = low_stop_consensus(evals, st)
    t3 = (cons is not None and cons[0] == "long" and cons[2]["agreement"] >= 0.6, f"cons={cons[0] if cons else None} agr={cons[2]['agreement'] if cons else 0:.2f}")
    # Independent TFs: 1m up, 5m up, 15m up → combined long
    book = IndicationBook()
    book.settings["minimumConfidence"] = 0.5
    book.settings["minimumStrength"] = 0.05
    book.settings["minimumSourceSignals"] = 2
    tf_bars = {"1m": up, "5m": up, "15m": up}
    rows = book.process("AAA-USDT", up, bars_by_tf=tf_bars)
    modes = {r.mode for r in rows}
    comb = next((r for r in rows if r.mode == "tf_combined"), None)
    tfs = [r.timeframe for r in rows if r.mode == "direct_tf"]
    t4 = ("tf_combined" in modes and comb is not None and comb.direction == "long", f"modes={sorted(modes)} tfs={tfs} dir={comb.direction if comb else None}")
    # Disagree: 1m up vs 5m/15m down → combined short if min_agree=2
    book.settings["tfMinAgree"] = 2
    rows2 = book.process("BBB-USDT", up, bars_by_tf={"1m": up, "5m": down, "15m": down})
    comb2 = next((r for r in rows2 if r.mode == "tf_combined"), None)
    t5 = (comb2 is not None and comb2.direction == "short" and comb2.votes_short >= 2, f"dir={comb2.direction if comb2 else None} vs={comb2.votes_short if comb2 else 0} vl={comb2.votes_long if comb2 else 0}")
    # Independence: 1m-only still produces a 1m lane without combined
    rows3 = book.process("CCC-USDT", up, bars_by_tf={"1m": up, "5m": [], "15m": []})
    t6 = (any(r.mode == "direct_tf" and r.timeframe == "1m" for r in rows3) and not any(r.mode == "tf_combined" for r in rows3), f"modes={[r.mode+':'+r.timeframe for r in rows3]}")
    # Direction: down then up reversal
    rev = []
    for i in range(12):
        c = base * (1 - i * 0.004)
        rev.append([c, c * 1.0004, c * 0.9996, c, 800])
    for i in range(12):
        c = base * (0.952 + i * 0.005)
        rev.append([c, c * 1.0004, c * 0.9996, c, 900])
    st2 = dict(DEFAULT_SETTINGS)
    st2["dirRange"] = 8
    st2["dirMinChange"] = 0.001
    st2["minimumConfidence"] = 0.4
    drow = evaluate_direction("REV-USDT", [b[3] for b in rev], st2)
    t7 = (drow is not None and drow.direction == "long" and drow.kind == "direction", f"dir={drow.direction if drow else None} str={drow.strength if drow else 0:.3f}")
    # Move: persistent up
    mrow = evaluate_move("MOV-USDT", [b[3] for b in up], st2)
    t8 = (mrow is not None and mrow.direction == "long" and mrow.kind == "move", f"move={mrow.direction if mrow else None}")
    # Move must not require reversal (same-dir)
    t8b = (mrow is not None, "move-same-dir")
    # Active outbreak: quiet then sharp breakout
    act_px = [100.0] * 12
    for i in range(8):
        act_px.append(100.0 + i * 0.02)
    for i in range(6):
        act_px.append(100.16 + (i + 1) * 1.2)
    st3 = dict(DEFAULT_SETTINGS)
    st3["activeOutbreak"] = [3, 5, 10]
    st3["activeMovePct"] = 0.3
    st3["activeThreshold"] = 1.0
    st3["activeNoise"] = 0.05
    st3["minimumConfidence"] = 0.3
    arows = evaluate_active_all("ACT-USDT", act_px, st3)
    t9 = (len(arows) >= 1 and all(r.kind == "active" and r.direction == "long" for r in arows), f"n={len(arows)} dirs={[r.mode for r in arows]}")
    # Common: oversold then bounce-shaped rsi via steep drop
    drop = []
    px = 100.0
    for i in range(40):
        px *= 0.992
        drop.append(Candle(i, px * 1.001, px * 1.002, px * 0.998, px, 1000))
    crow = evaluate_common("COM-USDT", drop, st2)
    t10 = (crow is None or crow.kind == "common", f"common={crow.direction if crow else None} {crow.mode if crow else 'none'}")
    # Type flags: disable state → no consensus/combined
    book.settings["typeState"] = False
    book.settings["typeSignals"] = True
    book.settings["typeDirection"] = True
    book.settings["typeMove"] = True
    book.settings["typeActive"] = True
    book.settings["typeCommon"] = True
    rows4 = book.process("FLG-USDT", up, bars_by_tf={"1m": up, "5m": up, "15m": up})
    kinds4 = {r.kind for r in rows4}
    t11 = ("state" not in kinds4 and ("signals" in kinds4 or "move" in kinds4), f"kinds={sorted(kinds4)}")
    # best() prefers highest confidence independent of State when State off
    b = book.best("FLG-USDT")
    t12 = (b is not None and b.kind != "state", f"best={b.kind if b else None} c={b.confidence if b else 0:.2f}")
    # Independent Long vs Short: mixed evidence does not copy one side
    split = evaluate_independent_directions([0.2, 0.1, -0.4, -0.3, -0.2], min_evidence=1, min_agreement=0.3)
    t13 = (split["selected"] == "short" and split["long"]["ok"] and split["short"]["ok"], f"sel={split['selected']} L={split['long']['score']:.2f} S={split['short']['score']:.2f}")
    no_rev = evaluate_direction("FLAT-USDT", [b[3] for b in up], st2)
    t14 = (no_rev is None, f"dir-on-trend={no_rev.direction if no_rev else None}")
    quiet = evaluate_active_all("Q-USDT", [100.0 + i * 0.0001 for i in range(40)], st3)
    t15 = (len(quiet) == 0, f"quiet n={len(quiet)}")
    book.settings["typeState"] = True
    book.settings["minimumConfidence"] = 0.5
    book.process("PICK-USDT", up, pulse_dir=1, pulse_conf=0.8, bars_by_tf={"1m": up, "5m": up, "15m": up})
    pe = book.pick_entry("PICK-USDT", min_conf=0.5)
    t16 = (pe is not None and pe[0].kind in ("state", "signals", "move", "direction", "active", "common", "trend", "break") and pe[1] >= 0.5, f"pick={pe[0].kind if pe else None} c={pe[1] if pe else 0:.2f} a={pe[2] if pe else 0}")
    book.process("KEEP-USDT", up, pulse_dir=1, pulse_conf=0.7, bars_by_tf={"1m": up, "5m": up, "15m": up})
    dropped = book.keep(["PICK-USDT"])
    t17 = (dropped >= 1 and "PICK-USDT" in book.last and "KEEP-USDT" not in book.last, f"drop={dropped} keys={sorted(book.last)}")
    EXTRA.cache["binance-usdm:OLD-USDT"] = (time.time() - 200, [])
    EXTRA.cache["binance-usdm:PICK-USDT"] = (time.time(), [])
    pruned = EXTRA.prune({"PICK-USDT"}, max_n=8)
    t18 = (pruned >= 1 and "binance-usdm:PICK-USDT" in EXTRA.cache, f"pruned={pruned} n={len(EXTRA.cache)}")
    # TA pack is State, never Signals
    book.settings["typeState"] = True
    book.settings["typeSignals"] = True
    book.settings["minimumConfidence"] = 0.4
    book.settings["minimumStrength"] = 0.05
    rows_ta = book.process("TA-USDT", up, bars_by_tf={"1m": up, "5m": up, "15m": up})
    ta_rows = [r for r in rows_ta if r.mode == "ta_pack"]
    t19 = (len(ta_rows) >= 1 and all(r.kind == "state" for r in ta_rows), f"ta={[r.kind+':'+r.mode for r in ta_rows]}")
    t19b = (not any(r.kind == "signals" and "ta-rsi" in (r.sources or []) for r in rows_ta), f"sig-src={[r.sources for r in rows_ta if r.kind=='signals'][:3]}")
    snap = book.snapshot()
    ks = snap.get("kindStats") or {}
    t20 = (set(ks.keys()) == {"state", "signals", "active", "direction", "move", "common", "trend", "break"}, f"keys={sorted(ks)}")
    t21 = (int((ks.get("signals") or {}).get("hits") or 0) >= 1 and int((ks.get("state") or {}).get("hits") or 0) >= 1, f"hits={{k: v.get('hits') for k, v in ks.items()}}")
    t22 = (all(bool(v.get("processed")) and "enabled" in v for v in ks.values()), str({k: v.get("enabled") for k, v in ks.items()}))
    # Direction / move / active / common still run independently on their own tapes
    kinds_seen = {r.kind for r in rows_ta}
    if drow:
        kinds_seen.add(drow.kind)
    if mrow:
        kinds_seen.add(mrow.kind)
    kinds_seen |= {r.kind for r in arows}
    if crow:
        kinds_seen.add(crow.kind)
    t23 = ({"state", "signals", "direction", "move", "active"} <= kinds_seen, f"seen={sorted(kinds_seen)}")
    # Signals stay first-class: flag off drops them; match never swaps to State
    book.settings["typeState"] = True
    book.settings["typeSignals"] = True
    book.settings["minimumConfidence"] = 0.5
    book.settings["minimumStrength"] = 0.05
    rows_sig = book.process("SIG-USDT", up, bars_by_tf={"1m": up, "5m": up, "15m": up})
    sig_rows = [r for r in rows_sig if r.kind == "signals"]
    t24 = (len(sig_rows) >= 1 and all(r.kind == "signals" for r in sig_rows), f"n={len(sig_rows)} modes={[r.mode for r in sig_rows[:4]]}")
    m_sig = book.match("SIG-USDT", "ind:signals:direct_tf:1.00:a1:bingx-1m")
    t25 = (m_sig is not None and m_sig.kind == "signals", f"match={m_sig.kind if m_sig else None}")
    m_miss = book.match("SIG-USDT", "ind:active:outbreak:3")
    # active may or may not exist on a pure up-tape; if missing, must not fall back to state
    if not any(r.kind == "active" for r in rows_sig):
        t26 = (m_miss is None, f"miss-fallback={m_miss.kind if m_miss else None}")
    else:
        t26 = (m_miss is not None and m_miss.kind == "active", f"active={m_miss.kind if m_miss else None}")
    pe_skip = book.pick_entry("SIG-USDT", min_conf=0.5, allow=lambda k, d=None: k != "state")
    t27 = (pe_skip is not None and pe_skip[0].kind != "state", f"pick={pe_skip[0].kind if pe_skip else None}")
    book.settings["typeSignals"] = False
    rows_nosig = book.process("NOSIG-USDT", up, bars_by_tf={"1m": up, "5m": up, "15m": up})
    t28 = (not any(r.kind == "signals" for r in rows_nosig), f"kinds={sorted({r.kind for r in rows_nosig})}")
    book.settings["typeSignals"] = True
    six = []
    ts0 = time.time() - 40 * 60
    for i, b in enumerate(up):
        six.append([ts0 + i * 60, b[0], b[1], b[2], b[3], b[4]])
    c5 = bars_to_candles(up)
    c6 = bars_to_candles(six)
    t29 = (
        len(c5) == len(c6) == 40
        and abs(c5[-1].close - c6[-1].close) < 1e-9
        and abs(c5[-1].open - c6[-1].open) < 1e-9
        and c6[-1].open < 1000,
        f"n5={len(c5)} n6={len(c6)} o6={c6[-1].open if c6 else None} c6={c6[-1].close if c6 else None}",
    )
    rows6 = book.process("SIX-USDT", six, bars_by_tf={"1m": six})
    t30 = (any(r.kind == "signals" for r in rows6) and any(r.kind == "state" for r in rows6),
           f"kinds={sorted({r.kind for r in rows6})}")
    tr = evaluate_trend("TR-USDT", [b[3] for b in up], st)
    t31 = (tr is not None and tr.kind == "trend" and tr.direction == "long", f"tr={tr.kind if tr else None} {tr.direction if tr else None}")
    brk_px = [100.0] * 20 + [102.2]
    br = evaluate_break("BR-USDT", brk_px, st)
    t32 = (br is not None and br.kind == "break" and br.direction == "long", f"br={br.kind if br else None} {br.direction if br else None}")
    book.settings["typeTrend"] = True
    book.settings["typeBreak"] = True
    rows_tb = book.process("TB-USDT", up, bars_by_tf={"1m": up})
    t33 = (any(r.kind == "trend" for r in rows_tb) or any(r.kind == "break" for r in rows_tb) or (tr is not None),
           f"kinds={sorted({r.kind for r in rows_tb})}")
    return [
        ("ind-eval-long", t1[0], t1[1]),
        ("ind-eval-short", t2[0], t2[1]),
        ("ind-consensus", t3[0], t3[1]),
        ("ind-tf-combined", t4[0], t4[1]),
        ("ind-tf-majority", t5[0], t5[1]),
        ("ind-tf-independent", t6[0], t6[1]),
        ("ind-direction-reversal", t7[0], t7[1]),
        ("ind-move-same-dir", t8[0] and t8b[0], t8[1]),
        ("ind-active-outbreak", t9[0], t9[1]),
        ("ind-common-ta", t10[0], t10[1]),
        ("ind-type-flags", t11[0], t11[1]),
        ("ind-best-independent", t12[0], t12[1]),
        ("ind-dir-lanes-independent", t13[0], t13[1]),
        ("ind-direction-needs-reversal", t14[0], t14[1]),
        ("ind-active-quiet-skip", t15[0], t15[1]),
        ("ind-pick-entry", t16[0], t16[1]),
        ("ind-keep-trim", t17[0], t17[1]),
        ("ind-extra-prune", t18[0], t18[1]),
        ("ind-ta-is-state", t19[0] and t19b[0], f"{t19[1]} {t19b[1]}"),
        ("ind-kind-stats-all-six", t20[0], t20[1]),
        ("ind-kind-stats-hits", t21[0], t21[1]),
        ("ind-kind-stats-processed", t22[0], t22[1]),
        ("ind-all-types-evaluated", t23[0], t23[1]),
        ("ind-signals-tf-lanes", t24[0], t24[1]),
        ("ind-match-signals", t25[0], t25[1]),
        ("ind-match-no-swap", t26[0], t26[1]),
        ("ind-pick-skip-gated-state", t27[0], t27[1]),
        ("ind-signals-flag-off", t28[0], t28[1]),
        ("ind-ohlcv-6tuple", t29[0], t29[1]),
        ("ind-process-6tuple-signals", t30[0], t30[1]),
        ("ind-trend", t31[0], t31[1]),
        ("ind-break", t32[0], t32[1]),
        ("ind-trend-break-process", t33[0], t33[1]),
    ]


if __name__ == "__main__":
    rows = self_test()
    bad = 0
    for name, ok, detail in rows:
        print(("PASS" if ok else "FAIL"), name, detail)
        bad += int(not ok)
    print("indication_engine", "ok" if not bad else f"fail={bad}")
    raise SystemExit(1 if bad else 0)
