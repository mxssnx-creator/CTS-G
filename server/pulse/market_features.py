"""Causal price drawdown and last-part measurements; all returns are fractions.

Ulcer index follows rolling peak drawdown RMS. The short-side analogue uses
rolling troughs. Linear time monotonic queues keep multi-window work bounded.
"""
from collections import deque
import math


def price_window(closes, n: int, last_part: int = 3):
    if n < 2 or len(closes) < 2 * n - 1:
        return None
    values = [float(x) for x in closes[-(2 * n - 1):]]
    if any(not math.isfinite(x) or x <= 0 for x in values):
        return None
    highs, lows = deque(), deque()
    dd, up = [], []
    for i, value in enumerate(values):
        while highs and highs[0] <= i - n:
            highs.popleft()
        while lows and lows[0] <= i - n:
            lows.popleft()
        while highs and values[highs[-1]] <= value:
            highs.pop()
        while lows and values[lows[-1]] >= value:
            lows.pop()
        highs.append(i)
        lows.append(i)
        if i >= n - 1:
            dd.append(1 - value / values[highs[0]])
            up.append(value / values[lows[0]] - 1)
    k = max(1, min(int(last_part), n - 1))
    recent = values[-n:]
    variation = sum(abs(b - a) for a, b in zip(recent, recent[1:]))
    return {"n": n, "peak": values[highs[0]], "trough": values[lows[0]], "drawdown": dd[-1], "drawup": up[-1],
            "ulcer": math.sqrt(sum(x * x for x in dd) / n),
            "shortUlcer": math.sqrt(sum(x * x for x in up) / n),
            "underwaterBars": len(values) - 1 - highs[0],
            "overwaterBars": len(values) - 1 - lows[0],
            "lastReturn": values[-1] / values[-1 - k] - 1,
            "priorReturn": values[-1 - k] / values[-n] - 1,
            "efficiency": abs(recent[-1] - recent[0]) / variation if variation else 0.0,
            "drawdownChange": dd[-1] - dd[-2], "drawupChange": up[-1] - up[-2]}


def multi_price_windows(closes, windows=(5, 10, 20, 40), last_part=3):
    return {n: value for n in sorted(set(windows))
            if (value := price_window(closes, n, last_part)) is not None}
