"""Completed OHLCV reversal profiles. Keep in parity with shared/cts-reversal/reversal.ts."""

import math

PERIODS = (6, 10, 16)
TACTICS = ("reclaim", "structure", "impulse", "pullback", "rsi", "psar", "confluence")
SOURCES = tuple("trend-reversal-" + tactic for tactic in TACTICS)


def _valid(row):
    o, high, low, close, volume = row
    return (math.isfinite(o) and math.isfinite(high) and math.isfinite(close) and
            math.isfinite(volume) and volume >= 0 and 0 < low <= min(o, close) and high >= max(o, close))


def _psar_turn(rows, start, end):
    left, right = rows[start], rows[start + 1]
    side = 1 if right[3] >= left[3] else -1
    extreme = max(left[1], right[1]) if side > 0 else min(left[2], right[2])
    stop = min(left[2], right[2]) if side > 0 else max(left[1], right[1])
    acceleration = .02
    flip = 0
    for j in range(start + 2, end + 1):
        _, high, low, _, _ = rows[j]
        stop += acceleration * (extreme - stop)
        stop = min(stop, rows[j - 1][2], rows[j - 2][2]) if side > 0 else max(stop, rows[j - 1][1], rows[j - 2][1])
        next_extreme = high if side > 0 else low
        flip = 0
        if (low < stop if side > 0 else high > stop):
            side = -side
            stop, extreme, acceleration, flip = extreme, high if side > 0 else low, .02, side
        elif side * (next_extreme - extreme) > 0:
            extreme = next_extreme
            acceleration = min(.2, acceleration + .02)
    return flip


def reversal_at(rows, i, period, tactic):
    """Return direction using the bounded prefix, on the caller's contiguous completed candle series."""
    if not isinstance(period, int) or not 4 <= period <= 60:
        return 0
    related = tactic in ("pullback", "confluence")
    recent = i - period - 1
    start = recent - period if related else recent
    if start < 0 or i >= len(rows) or not _valid(rows[start]):
        return 0
    atr = volume = up = down = 0.0
    upper = swing_high = -math.inf
    lower = swing_low = math.inf
    swing = max(2, math.floor(period / 5 + .5))
    for j in range(start + 1, i + 1):
        row = rows[j]
        if not _valid(row):
            return 0
        if j <= recent or j == i:
            continue
        _, high, low, close, vol = row
        prior = rows[j - 1][3]
        delta = close - prior
        up += max(0, delta)
        down += max(0, -delta)
        atr += max(high - low, abs(high - prior), abs(low - prior))
        volume += vol
        upper = max(upper, high)
        lower = min(lower, low)
        if j >= i - swing:
            swing_high = max(swing_high, high)
            swing_low = min(swing_low, low)
    atr /= period
    if not (atr > 0 and up + down > 0):
        return 0
    o, high, low, close, vol = rows[i]
    previous = rows[i - 1][3]
    old_move = previous - rows[recent][3]
    side = 1 if old_move < 0 else -1
    body, change, candle_range = side * (close - o), side * (close - previous), high - low
    if (not (body > 0 and change > 0 and candle_range > 0) or change > 3.25 * atr or
            abs(old_move) < 1.1 * atr or abs(old_move) < .22 * (up + down)):
        return 0
    location = close - low if side > 0 else high - close
    if location < .58 * candle_range:
        return 0
    swing_break = close > swing_high if side > 0 else close < swing_low
    trend_aligned = related and side * (rows[recent][3] - rows[start][3]) >= 1.35 * atr
    if tactic == "reclaim":
        return side if (low < lower and close >= lower + .10 * atr if side > 0
                        else high > upper and close <= upper - .10 * atr) else 0
    if tactic == "structure":
        return side if swing_break and body >= .22 * atr else 0
    if tactic == "impulse":
        return side if change >= .85 * atr and body >= .45 * atr and volume > 0 and vol >= 1.20 * volume / period else 0
    if tactic == "pullback":
        return side if trend_aligned and swing_break and body >= .18 * atr else 0
    if tactic in ("rsi", "confluence"):
        prior_rsi = 100 * up / (up + down)
        drop = rows[recent + 1][3] - rows[recent][3]
        gain = up - max(0, drop) + max(0, close - previous)
        loss = down - max(0, -drop) + max(0, previous - close)
        rsi = 100 * gain / (gain + loss) if gain + loss > 0 else 50
        recovered = prior_rsi <= 40 < rsi if side > 0 else prior_rsi >= 60 > rsi
        return side if recovered and (tactic == "rsi" or (trend_aligned and swing_break)) else 0
    if tactic == "psar":
        return side if _psar_turn(rows, start, i) == side else 0
    return 0


def signal_family(tactic):
    def evaluate(rows, period):
        i = len(rows) - 1
        side = reversal_at(rows, i, period, tactic)
        return (side, .60) if side and side != reversal_at(rows, i - 1, period, tactic) else None
    return evaluate


def signal_period(range_name):
    return 6 if range_name == "short" else 16
