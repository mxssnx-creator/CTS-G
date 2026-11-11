"""CTS-A-O Low-Range coordinations, each an independent CTS-G signal pack.

Source: mxssnx-creator/CTS-A-O at 9f054c3, low-range-research.ts.
The prior-window geometry and 13 relation rules are ported without tuning.
CTS-G supplies its causal timeframe aggregation and its own protection/stages.
No return or validation result participates in a signal calculation.
"""
from __future__ import annotations

import math
from indication_lib import _agg, _key, _memo

WINDOWS = (1, 2, 3, 6, 9, 12, 18, 24, 36, 48)
METHODS = ("edge-turn", "failed-break", "breakout", "mid-reclaim", "compression-break")
RELATIONS = ("standalone", "trend", "breakout", "activity", "trend-breakout",
             "move-follow", "move-reverse", "trend-move-follow", "trend-move-reverse",
             "breakout-move-follow", "breakout-move-reverse",
             "trend-breakout-move-follow", "trend-breakout-move-reverse")
TIMEFRAMES = (1, 5, 15)


def features(rows, window):
    """Current completed bar against strictly earlier bars; invalid history is neutral."""
    i = len(rows) - 1
    if i < window:
        return {}
    tail = rows[max(0, i - max(2 * window, window + 2)):]
    run = 0
    for r in tail:
        if len(r) < 5 or not all(math.isfinite(float(v)) for v in r[:5]):
            run = 0
            continue
        o, h, l, c, v = r[:5]
        if l <= 0 or h < max(o, c) or l > min(o, c) or v < 0:
            run = 0
        else:
            run += 1
    # run includes the current bar; the source's s.run is its prior valid run.
    if run <= window:
        return {}
    prior = rows[i - window:i]
    high, low = max(r[1] for r in prior), min(r[2] for r in prior)
    previous, close = rows[i - 1][3], rows[i][3]
    width = high - low
    if width <= 0 or width / previous > .025:
        return {}
    sign = lambda x: 1 if x > 0 else -1 if x < 0 else 0
    edge = 1 if previous <= low + width * .25 and close > previous else -1 if previous >= high - width * .25 and close < previous else 0
    reject_long, reject_short = rows[i][2] < low and close > low, rows[i][1] > high and close < high
    failed = 0 if reject_long == reject_short else 1 if reject_long else -1
    breakout = 1 if close > high else -1 if close < low else 0
    mid = (high + low) / 2
    reclaim = 1 if previous < mid <= close else -1 if previous > mid >= close else 0
    compression = 0
    if run > 2 * window:
        older = rows[i - 2 * window:i]
        older_width = max(r[1] for r in older) - min(r[2] for r in older)
        if older_width > 0 and width / older_width <= .65:
            compression = breakout
    trend = sign(previous - rows[i - window - 2][3]) if window < 12 and run > window + 2 else sign(previous - sum(r[3] for r in prior) / window) if window >= 12 else 0
    recent = prior[-6:]
    short_break = 1 if close > max(r[1] for r in recent) else -1 if close < min(r[2] for r in recent) else 0
    volume = sum(r[4] for r in prior)
    activity = volume > 0 and rows[i][4] >= 1.2 * volume / window
    anchor_i = i - 2 if window == 1 else i - window
    move = 0
    if anchor_i >= 0 and rows[anchor_i][3] > 0:
        displacement = math.log1p((previous - rows[anchor_i][3]) / rows[anchor_i][3])
        threshold = .2 * math.log1p(width / low)
        move = 1 if displacement >= threshold else -1 if displacement <= -threshold else 0
    return {"edge-turn": edge, "failed-break": failed, "breakout": breakout, "mid-reclaim": reclaim,
            "compression-break": compression, "trend": trend, "short-break": short_break,
            "activity": activity, "move": move}


def direction(f, method, relation):
    side = f.get(method, 0)
    if not side:
        return 0
    if relation == "standalone":
        return side
    if relation == "activity":
        return side if f["activity"] else 0
    if "trend" in relation and f["trend"] != side:
        return 0
    if "breakout" in relation and f["short-break"] != side:
        return 0
    if "move-follow" in relation and f["move"] != side:
        return 0
    if "move-reverse" in relation and f["move"] != -side:
        return 0
    return side


def make_signal(window, method, relation, tf):
    def signal(rows):
        key = ("ao-lowrange", window) + _key(rows, tf)
        memo = _memo()
        cached = memo.get(key)
        if cached is None:
            f = features(_agg(rows, tf), window)
            # Retain endpoints to prevent recycled object identities from aliasing symbols.
            cached = memo[key] = (rows[0] if len(rows) else None, rows[-1] if len(rows) else None, f)
        side = direction(cached[2], method, relation)
        return (side, .65) if side else None
    return signal


LOW_RANGE = {f"ao_lr{window}_{method}_{relation}_t{tf}": make_signal(window, method, relation, tf)
             for window in WINDOWS for method in METHODS for relation in RELATIONS for tf in TIMEFRAMES}
