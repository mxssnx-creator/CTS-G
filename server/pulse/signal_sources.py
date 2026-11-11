"""Causal experimental Signal sources with explicit, independently tested definitions.

This is not IndicationBook's combined vote. Source, range and completed-bar
timeframe identities remain separate. Unsupported sources fail explicitly.
Reference: CTS-A-O 2599b652, registry.ts and math/indicators.ts.
"""
from __future__ import annotations

import math
from collections import deque

SOURCES = ("ema-cross", "macd-cross", "rsi-reversal", "ema-trend", "rsi-momentum", "donchian",
           "bb-bounce", "cci-reversal", "obv-trend")
RANGES = ("short", "medium")
TIMEFRAMES = (1, 5, 15, 30)


def warmup_bars(source, range_name):
    """Minimum completed TF bars for the first possible nonzero source state."""
    if source not in SOURCES or range_name not in RANGES:
        raise ValueError("unsupported Signal source/range")
    return {"ema-cross": (21, 55), "ema-trend": (26, 50),
            "macd-cross": (26, 35), "rsi-reversal": (8, 15),
            "rsi-momentum": (15, 22), "donchian": (11, 21), "bb-bounce": (20, 50),
            "cci-reversal": (14, 20), "obv-trend": (20, 50)}[source][range_name == "medium"]


def validate_bars(rows):
    previous = None
    for row in rows:
        if len(row) != 6 or any(not math.isfinite(x) for x in row):
            raise ValueError("Signal OHLCV must have six finite numbers")
        t, o, h, low, c, v = row
        if t % 60000 or min(o, low, c) <= 0 or low > min(o, c) or h < max(o, c) or v < 0:
            raise ValueError("invalid Signal candle")
        if previous is not None and t != previous + 60000:
            raise ValueError("Signal minutes must be contiguous, ordered and unique")
        previous = t


def completed_bars(rows, minutes):
    """Aggregate only complete UTC-aligned blocks; never use a partial last bar."""
    if minutes not in TIMEFRAMES:
        raise ValueError("unsupported Signal timeframe")
    out, group, bucket = [], [], None
    width = minutes * 60000
    for row in rows:
        key = int(row[0] // width) * width
        if key != bucket:
            group, bucket = [], key
        group.append(row)
        if len(group) == minutes and group[0][0] == key and row[0] + 60000 == key + width:
            out.append([key, group[0][1], max(r[2] for r in group), min(r[3] for r in group), row[4], sum(r[5] for r in group)])
    return out


def ema(values, period):
    out = [math.nan] * len(values)
    start = next((i for i, x in enumerate(values) if math.isfinite(x)), len(values))
    total, previous, k = 0.0, math.nan, 2 / (period + 1)
    for i in range(start, len(values)):
        if i - start < period - 1:
            total += values[i]
            continue
        previous = (total + values[i]) / period if i - start == period - 1 else values[i] * k + previous * (1 - k)
        out[i] = previous
    return out


def rsi(values, period):
    out = [math.nan] * len(values)
    gain = loss = 0.0
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        up, down = max(0, change), max(0, -change)
        if i <= period:
            gain += up
            loss += down
            if i == period:
                gain /= period
                loss /= period
        else:
            gain = (gain * (period - 1) + up) / period
            loss = (loss * (period - 1) + down) / period
        if i >= period:
            out[i] = (50 if gain == 0 else 100) if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def held(events, keep=6):
    out, current, left = [], 0, 0
    for event in events:
        if event:
            current, left = event, keep
        out.append(current if left else 0)
        left = max(0, left - 1)
    return out


def states(closes, source, range_name, highs=None, lows=None, volumes=None):
    if source not in SOURCES or range_name not in RANGES:
        raise ValueError("unsupported Signal source/range")
    if source == "bb-bounce":
        period = 20 if range_name == "short" else 50
        out = [0] * len(closes)
        for i in range(period - 1, len(closes)):
            window = closes[i-period+1:i+1]
            # Center first: E[x²]-E[x]² loses all variance on near-flat prices.
            origin = window[0]
            offsets = [x-origin for x in window]
            mean = math.fsum(offsets)/period
            sd = math.sqrt(math.fsum((x-mean)**2 for x in offsets)/period)
            delta = closes[i]-origin-mean
            out[i] = 1 if delta < -2*sd else -1 if delta > 2*sd else 0
        return out
    if source == "cci-reversal":
        if highs is None or lows is None or len(highs) != len(closes) or len(lows) != len(closes):
            raise ValueError("CCI needs corresponding high/low data")
        period = 14 if range_name == "short" else 20
        typical = [(h+l+c)/3 for h,l,c in zip(highs,lows,closes)]
        out = [0] * len(closes)
        for i in range(period-1,len(closes)):
            window = typical[i-period+1:i+1]
            origin = window[0]
            offsets = [x-origin for x in window]
            mean = math.fsum(offsets)/period
            deviation = math.fsum(abs(x-mean) for x in offsets)/period
            delta = typical[i]-origin-mean
            # CCI ±100 is exactly ±1.5 mean deviation. Avoid another division.
            out[i] = 1 if delta < -1.5*deviation else -1 if delta > 1.5*deviation else 0
        return out
    if source == "obv-trend":
        if volumes is None or len(volumes) != len(closes) or any(not math.isfinite(v) or v < 0 for v in volumes):
            raise ValueError("OBV needs corresponding finite, nonnegative volume")
        period = 20 if range_name == "short" else 50
        obv = [0.0] * len(closes)
        for i in range(1,len(closes)):
            obv[i] = obv[i-1] + (volumes[i] if closes[i]>closes[i-1] else -volumes[i] if closes[i]<closes[i-1] else 0)
        return [1 if x>y else -1 if x<y else 0 for x,y in zip(obv,ema(obv,period))]
    if source in ("ema-cross", "ema-trend"):
        fast, slow = ((9, 21) if range_name == "short" else (21, 55)) if source == "ema-cross" else ((12, 26) if range_name == "short" else (20, 50))
        a, b = ema(closes, fast), ema(closes, slow)
        if source == "ema-cross":
            return [1 if x > y else -1 if x < y else 0 for x, y in zip(a, b)]
        return [1 if x > y and c > y else -1 if x < y and c < y else 0 for x, y, c in zip(a, b, closes)]
    if source in ("rsi-reversal", "rsi-momentum"):
        p, low, high = ((7, 15, 85) if range_name == "short" else (14, 25, 75)) if source == "rsi-reversal" else ((14, 20, 80) if range_name == "short" else (21, 20, 80))
        sign = 1 if source == "rsi-reversal" else -1
        return [sign if x < low else -sign if x > high else 0 for x in rsi(closes, p)]
    if source == "donchian":
        if highs is None or lows is None or len(highs) != len(closes) or len(lows) != len(closes):
            raise ValueError("Donchian needs corresponding high/low data")
        period, up, down, events = (10 if range_name == "short" else 20), deque(), deque(), []
        for i, close in enumerate(closes):
            while up and up[0] < i - period: up.popleft()
            while down and down[0] < i - period: down.popleft()
            events.append((1 if close > highs[up[0]] else -1 if close < lows[down[0]] else 0) if i >= period else 0)
            while up and highs[up[-1]] <= highs[i]: up.pop()
            while down and lows[down[-1]] >= lows[i]: down.pop()
            up.append(i); down.append(i)
        return held(events)
    fast, slow, length = (8, 21, 5) if range_name == "short" else (12, 26, 9)
    line = [a - b for a, b in zip(ema(closes, fast), ema(closes, slow))]
    signal = ema(line, length)
    out, current, left = [0] * len(closes), 0, 0
    for i in range(1, len(closes)):
        event = 0
        if math.isfinite(signal[i - 1]):
            event = 1 if line[i - 1] < signal[i - 1] and line[i] > signal[i] else -1 if line[i - 1] > signal[i - 1] and line[i] < signal[i] else 0
        if event:
            current, left = event, 6
        if left:
            out[i], left = current, left - 1
    return out


def onsets(rows, source, range_name, minutes):
    """Decision at a completed bar's end; a model fill can use the next 1m open."""
    bars = completed_bars(rows, minutes)
    series = states([b[4] for b in bars], source, range_name, [b[2] for b in bars], [b[3] for b in bars], [b[5] for b in bars])
    return {int(bars[i][0] + minutes * 60000): side for i, side in enumerate(series)
            if i > 0 and side and side != series[i - 1]}
