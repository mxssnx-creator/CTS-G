"""Extra causal indicators for the Set indication pack.

Every indicator is a pure function of the newest bars, 1m rows [o, h, l, c, v] (newest last), and returns
(direction, confidence) with direction +1 long / -1 short, or None when it has nothing to say. Only the last
LOOKBACK rows are read, nothing looks ahead, and the result does not depend on any clock.

The set is deliberately wide: classic trend, momentum, reversion, volatility and volume tools next to raw
price-action measures (streaks, range position, candle shape, the move before a pullback). Which of them earn a
place in a vote is decided by measurement (scripts/indicator_eval.py), not here: set_engine reads the overlay key
"extraIndicators" (a list of names) and adds the listed ones as votes. The default is the empty list, so the
shipped votes stay as they are until a measurement says otherwise.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

LOOKBACK = 160
Row = Sequence[float]
Signal = Optional[Tuple[int, float]]


def _ema_list(values: Sequence[float], period: int) -> List[float]:
    k = 2.0 / (period + 1.0)
    out: List[float] = []
    e = float(values[0])
    for v in values:
        e = float(v) * k + e * (1.0 - k)
        out.append(e)
    return out


def _true_ranges(rows: Sequence[Row]) -> List[float]:
    out = [float(rows[0][1]) - float(rows[0][2])]
    for i in range(1, len(rows)):
        h, lo, pc = float(rows[i][1]), float(rows[i][2]), float(rows[i - 1][3])
        out.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    return out


def _atr(rows: Sequence[Row], period: int = 14) -> float:
    trs = _true_ranges(rows)[-period:]
    return sum(trs) / len(trs) if trs else 0.0


def _rsi_series(closes: Sequence[float], period: int) -> List[float]:
    """Wilder RSI per bar (50 until the first full period)."""
    out = [50.0] * len(closes)
    if len(closes) <= period:
        return out
    gain = loss = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gain += max(d, 0.0)
        loss += max(-d, 0.0)
    gain /= period
    loss /= period
    out[period] = 100.0 if loss == 0 else 100.0 - 100.0 / (1.0 + gain / loss)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        gain = (gain * (period - 1) + max(d, 0.0)) / period
        loss = (loss * (period - 1) + max(-d, 0.0)) / period
        out[i] = 100.0 if loss == 0 else 100.0 - 100.0 / (1.0 + gain / loss)
    return out


def _conf(base: float, extra: float, cap: float = 0.95) -> float:
    return max(0.5, min(cap, base + max(0.0, extra)))


def _tail(rows: Sequence[Row]) -> Sequence[Row]:
    return rows[-LOOKBACK:] if len(rows) > LOOKBACK else rows


# ---- trend ------------------------------------------------------------------------------------------------------


def supertrend(rows: Sequence[Row], period: int = 10, mult: float = 3.0, fresh: int = 3) -> Signal:
    """ATR band trend; signals in the first `fresh` bars after the trend flips."""
    r = _tail(rows)
    if len(r) < period + 12:
        return None
    trs = _true_ranges(r)
    atrs = [0.0] * len(r)
    a = sum(trs[:period]) / period
    for i in range(period, len(r)):
        a = (a * (period - 1) + trs[i]) / period
        atrs[i] = a
    up = dn = None
    trend = 1
    flipped_at = -10 ** 6
    for i in range(period, len(r)):
        mid = (float(r[i][1]) + float(r[i][2])) / 2.0
        bu, bd = mid + mult * atrs[i], mid - mult * atrs[i]
        c_prev = float(r[i - 1][3])
        up = bu if up is None or bu < up or c_prev > up else up
        dn = bd if dn is None or bd > dn or c_prev < dn else dn
        c = float(r[i][3])
        new = trend
        if trend == 1 and c < dn:
            new = -1
        elif trend == -1 and c > up:
            new = 1
        if new != trend:
            flipped_at = i
            trend = new
    age = len(r) - 1 - flipped_at
    if age >= fresh:
        return None
    return trend, _conf(0.62, 0.05 * (fresh - age))


def adx_trend(rows: Sequence[Row], period: int = 14, floor: float = 22.0) -> Signal:
    r = _tail(rows)
    if len(r) < period * 3:
        return None
    pdm: List[float] = []
    mdm: List[float] = []
    for i in range(1, len(r)):
        up = float(r[i][1]) - float(r[i - 1][1])
        dn = float(r[i - 1][2]) - float(r[i][2])
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
    trs = _true_ranges(r)[1:]
    sm_tr, sm_p, sm_m = sum(trs[:period]), sum(pdm[:period]), sum(mdm[:period])
    dxs: List[float] = []
    pdi = mdi = 0.0
    for i in range(period, len(trs)):
        sm_tr = sm_tr - sm_tr / period + trs[i]
        sm_p = sm_p - sm_p / period + pdm[i]
        sm_m = sm_m - sm_m / period + mdm[i]
        if sm_tr <= 0:
            continue
        pdi, mdi = 100.0 * sm_p / sm_tr, 100.0 * sm_m / sm_tr
        dxs.append(100.0 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi > 0 else 0.0)
    if len(dxs) < period:
        return None
    adx = sum(dxs[-period:]) / period
    if adx < floor or abs(pdi - mdi) < 5.0:
        return None
    return (1 if pdi > mdi else -1), _conf(0.6, (adx - floor) / 100.0)


def macd_cross(rows: Sequence[Row], fast: int = 12, slow: int = 26, sig: int = 9) -> Signal:
    r = _tail(rows)
    if len(r) < slow + sig + 4:
        return None
    closes = [float(b[3]) for b in r]
    ef, es = _ema_list(closes, fast), _ema_list(closes, slow)
    macd = [a - b for a, b in zip(ef, es)]
    signal = _ema_list(macd, sig)
    hist = [m - s for m, s in zip(macd, signal)]
    if hist[-2] <= 0 < hist[-1]:
        d = 1
    elif hist[-2] >= 0 > hist[-1]:
        d = -1
    else:
        return None
    atr = _atr(r)
    if atr <= 0 or abs(hist[-1]) < 0.03 * atr:
        return None
    return d, _conf(0.6, min(0.2, abs(hist[-1]) / atr))


def pullback_trend(rows: Sequence[Row]) -> Signal:
    """Trend (EMA20 vs EMA60, both sloping), a pullback of 0.6-1.8 ATR from the 30-bar extreme, and the close back
    through EMA9: the trend resumes."""
    r = _tail(rows)
    if len(r) < 80:
        return None
    closes = [float(b[3]) for b in r]
    e9, e20, e60 = _ema_list(closes, 9), _ema_list(closes, 20), _ema_list(closes, 60)
    atr = _atr(r)
    if atr <= 0:
        return None
    hi = max(float(b[1]) for b in r[-30:])
    lo = min(float(b[2]) for b in r[-30:])
    c = closes[-1]
    if e20[-1] > e60[-1] and e20[-1] > e20[-6] and e60[-1] > e60[-6]:
        dd = (hi - min(float(b[2]) for b in r[-8:])) / atr
        if 0.6 <= dd <= 1.8 and closes[-2] <= e9[-2] and c > e9[-1]:
            return 1, _conf(0.62, 0.05 * min(2.0, dd))
    if e20[-1] < e60[-1] and e20[-1] < e20[-6] and e60[-1] < e60[-6]:
        dd = (max(float(b[1]) for b in r[-8:]) - lo) / atr
        if 0.6 <= dd <= 1.8 and closes[-2] >= e9[-2] and c < e9[-1]:
            return -1, _conf(0.62, 0.05 * min(2.0, dd))
    return None


# ---- breakout / volatility ----------------------------------------------------------------------------------------


def donchian_break(rows: Sequence[Row], n: int = 20, vol_mult: float = 1.5) -> Signal:
    r = _tail(rows)
    if len(r) < n + 2:
        return None
    prior = r[-n - 1 : -1]
    hi = max(float(b[1]) for b in prior)
    lo = min(float(b[2]) for b in prior)
    avg_v = sum(float(b[4]) for b in prior) / n
    c, v = float(r[-1][3]), float(r[-1][4])
    if avg_v <= 0 or v < vol_mult * avg_v:
        return None
    if c > hi:
        return 1, _conf(0.62, min(0.2, (v / avg_v - vol_mult) * 0.05))
    if c < lo:
        return -1, _conf(0.62, min(0.2, (v / avg_v - vol_mult) * 0.05))
    return None


def squeeze_release(rows: Sequence[Row], n: int = 20, min_on: int = 6) -> Signal:
    """Bollinger bands inside Keltner channels for at least `min_on` bars, released this bar; direction from the
    close against the channel midline."""
    r = _tail(rows)
    if len(r) < n + min_on + 2:
        return None
    closes = [float(b[3]) for b in r]
    trs = _true_ranges(r)
    flags: List[bool] = []
    mids: List[float] = []
    for i in range(n - 1, len(r)):
        w = closes[i - n + 1 : i + 1]
        m = sum(w) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
        kc = sum(trs[i - n + 1 : i + 1]) / n * 1.5
        flags.append(2.0 * sd < kc)
        mids.append(m)
    if flags[-1] or sum(1 for f in flags[-(min_on + 1) : -1] if f) < min_on:
        return None
    d = 1 if closes[-1] > mids[-1] else -1
    return d, _conf(0.62, 0.0)


def range_expansion(rows: Sequence[Row]) -> Signal:
    """Short ATR well above the long ATR and a decisive candle body: the move continues."""
    r = _tail(rows)
    if len(r) < 40:
        return None
    a_s, a_l = _atr(r, 5), _atr(r, 30)
    if a_l <= 0 or a_s / a_l < 1.8:
        return None
    o, c, h, lo = float(r[-1][0]), float(r[-1][3]), float(r[-1][1]), float(r[-1][2])
    rng = h - lo
    if rng <= 0 or abs(c - o) / rng < 0.6:
        return None
    return (1 if c > o else -1), _conf(0.6, min(0.2, (a_s / a_l - 1.8) * 0.1))


# ---- reversion ----------------------------------------------------------------------------------------------------


def rsi2_reversion(rows: Sequence[Row], lo: float = 10.0, hi: float = 90.0, trend_ema: int = 60) -> Signal:
    r = _tail(rows)
    if len(r) < trend_ema + 5:
        return None
    closes = [float(b[3]) for b in r]
    rs = _rsi_series(closes, 2)[-1]
    e = _ema_list(closes, trend_ema)[-1]
    if rs < lo and closes[-1] > e:
        return 1, _conf(0.6, (lo - rs) / 100.0)
    if rs > hi and closes[-1] < e:
        return -1, _conf(0.6, (rs - hi) / 100.0)
    return None


def stoch_rsi_cross(rows: Sequence[Row], period: int = 14, lo: float = 20.0, hi: float = 80.0) -> Signal:
    r = _tail(rows)
    if len(r) < period * 3:
        return None
    closes = [float(b[3]) for b in r]
    rs = _rsi_series(closes, period)
    k: List[float] = []
    for i in range(period, len(rs)):
        w = rs[i - period + 1 : i + 1]
        lo_w, hi_w = min(w), max(w)
        k.append(100.0 * (rs[i] - lo_w) / (hi_w - lo_w) if hi_w > lo_w else 50.0)
    if len(k) < 5:
        return None
    d_now = sum(k[-3:]) / 3.0
    d_prev = sum(k[-4:-1]) / 3.0
    if k[-2] <= d_prev and k[-1] > d_now and k[-1] < lo + 15:
        return 1, _conf(0.6, (lo + 15 - k[-1]) / 200.0)
    if k[-2] >= d_prev and k[-1] < d_now and k[-1] > hi - 15:
        return -1, _conf(0.6, (k[-1] - (hi - 15)) / 200.0)
    return None


def range_position(rows: Sequence[Row], n: int = 60) -> Signal:
    """Close in the bottom/top 6% of the n-bar range with RSI(14) stretched: fade it."""
    r = _tail(rows)
    if len(r) < n + 2:
        return None
    w = r[-n:]
    hi = max(float(b[1]) for b in w)
    lo = min(float(b[2]) for b in w)
    if hi <= lo:
        return None
    pos = (float(r[-1][3]) - lo) / (hi - lo)
    rsi = _rsi_series([float(b[3]) for b in r], 14)[-1]
    if pos < 0.06 and rsi < 30:
        return 1, _conf(0.6, (30 - rsi) / 150.0)
    if pos > 0.94 and rsi > 70:
        return -1, _conf(0.6, (rsi - 70) / 150.0)
    return None


def vwap_revert(rows: Sequence[Row], n: int = 60, z: float = 2.2) -> Signal:
    r = _tail(rows)
    if len(r) < n + 2:
        return None
    w = r[-n:]
    pv = vv = 0.0
    for b in w:
        tp = (float(b[1]) + float(b[2]) + float(b[3])) / 3.0
        pv += tp * float(b[4])
        vv += float(b[4])
    if vv <= 0:
        return None
    vwap = pv / vv
    devs = [float(b[3]) - vwap for b in w]
    sd = math.sqrt(sum(d * d for d in devs) / n)
    if sd <= 0:
        return None
    zz = devs[-1] / sd
    if zz < -z:
        return 1, _conf(0.6, min(0.2, (-zz - z) * 0.1))
    if zz > z:
        return -1, _conf(0.6, min(0.2, (zz - z) * 0.1))
    return None


def vwap_trend(rows: Sequence[Row], n: int = 60) -> Signal:
    """Close crosses VWAP with EMA9 above/below EMA21 and a VWAP that slopes the same way."""
    r = _tail(rows)
    if len(r) < n + 8:
        return None

    def vwap_at(end: int) -> float:
        pv = vv = 0.0
        for b in r[end - n : end]:
            pv += (float(b[1]) + float(b[2]) + float(b[3])) / 3.0 * float(b[4])
            vv += float(b[4])
        return pv / vv if vv > 0 else 0.0

    v_now, v_prev, v_old = vwap_at(len(r)), vwap_at(len(r) - 1), vwap_at(len(r) - 5)
    if v_now <= 0:
        return None
    closes = [float(b[3]) for b in r]
    e9, e21 = _ema_list(closes, 9)[-1], _ema_list(closes, 21)[-1]
    if closes[-2] <= v_prev < closes[-1] and e9 > e21 and v_now > v_old:
        return 1, 0.63
    if closes[-2] >= v_prev > closes[-1] and e9 < e21 and v_now < v_old:
        return -1, 0.63
    return None


# ---- volume / raw price action ------------------------------------------------------------------------------------


def volume_momentum(rows: Sequence[Row], n: int = 10) -> Signal:
    r = _tail(rows)
    if len(r) < 40:
        return None
    atr = _atr(r)
    if atr <= 0:
        return None
    w = r[-n:]
    ret = float(w[-1][3]) - float(w[0][0])
    vol_now = sum(float(b[4]) for b in w) / n
    vol_old = sum(float(b[4]) for b in r[-40:-n]) / (40 - n)
    if vol_old <= 0 or vol_now < 1.5 * vol_old:
        return None
    z = ret / (atr * math.sqrt(n))
    if abs(z) < 1.5:
        return None
    return (1 if z > 0 else -1), _conf(0.6, min(0.2, (abs(z) - 1.5) * 0.08))


def move_continuation(rows: Sequence[Row], n: int = 15) -> Signal:
    """The move before: a displacement of at least 4 ATR over n bars, a pullback of at most 38% of it, and the last
    bar resumes: the move continues."""
    r = _tail(rows)
    if len(r) < n + 20:
        return None
    atr = _atr(r)
    if atr <= 0:
        return None
    seg = r[-n - 1 : -1]
    start, end = float(seg[0][3]), float(seg[-1][3])
    move = end - start
    if abs(move) < 4.0 * atr:
        return None
    c0, c1 = float(r[-2][3]), float(r[-1][3])
    if move > 0:
        peak = max(float(b[1]) for b in seg)
        if (peak - c0) <= 0.38 * (peak - start) and c1 > c0:
            return 1, _conf(0.62, min(0.2, (move / atr - 4.0) * 0.02))
    else:
        trough = min(float(b[2]) for b in seg)
        if (c0 - trough) <= 0.38 * (start - trough) and c1 < c0:
            return -1, _conf(0.62, min(0.2, (abs(move) / atr - 4.0) * 0.02))
    return None


def move_fade(rows: Sequence[Row], n: int = 15) -> Signal:
    """The move before, exhausted: 5+ ATR over n bars and a reversal candle that closes back over 50% of the last bar."""
    r = _tail(rows)
    if len(r) < n + 20:
        return None
    atr = _atr(r)
    if atr <= 0:
        return None
    move = float(r[-2][3]) - float(r[-n - 1][3])
    if abs(move) < 5.0 * atr:
        return None
    o1, c1, o0, c0 = float(r[-1][0]), float(r[-1][3]), float(r[-2][0]), float(r[-2][3])
    body0 = abs(c0 - o0)
    if body0 <= 0:
        return None
    if move > 0 and c1 < o1 and (c1 <= c0 - 0.5 * body0):
        return -1, _conf(0.6, min(0.2, (move / atr - 5.0) * 0.02))
    if move < 0 and c1 > o1 and (c1 >= c0 + 0.5 * body0):
        return 1, _conf(0.6, min(0.2, (abs(move) / atr - 5.0) * 0.02))
    return None


def engulfing(rows: Sequence[Row]) -> Signal:
    r = _tail(rows)
    if len(r) < 20:
        return None
    atr = _atr(r)
    if atr <= 0:
        return None
    o1, h1, l1, c1 = (float(x) for x in r[-1][:4])
    o0, c0 = float(r[-2][0]), float(r[-2][3])
    downs = all(float(b[3]) < float(b[0]) for b in r[-4:-1])
    ups = all(float(b[3]) > float(b[0]) for b in r[-4:-1])
    if downs and c1 > o1 and c1 >= o0 and o1 <= c0 and (h1 - l1) >= atr:
        return 1, 0.62
    if ups and c1 < o1 and c1 <= o0 and o1 >= c0 and (h1 - l1) >= atr:
        return -1, 0.62
    return None


def streak_exhaustion(rows: Sequence[Row], n: int = 6) -> Signal:
    """n consecutive closes in one direction on shrinking volume: fade it."""
    r = _tail(rows)
    if len(r) < n + 2:
        return None
    seg = r[-n - 1 :]
    diffs = [float(seg[i][3]) - float(seg[i - 1][3]) for i in range(1, len(seg))]
    vols = [float(b[4]) for b in seg[1:]]
    shrinking = vols[-1] < vols[0] and vols[-1] < sum(vols) / len(vols)
    if all(d > 0 for d in diffs) and shrinking:
        return -1, 0.6
    if all(d < 0 for d in diffs) and shrinking:
        return 1, 0.6
    return None


# ---- Low-Range family ---------------------------------------------------------------------------------------------
# A low range is a stretch where the last n bars span only a few ATR and the Bollinger bandwidth sits at the bottom
# of its own recent distribution. Three tactics read it: fade the box edges, take the break of the box (alone, and
# only in the direction of the slope of a slower EMA), and the narrow-range bar (NR7, inside bar) break. All are
# causal and read only the newest LOOKBACK bars.


def low_range_state(rows: Sequence[Row], n: int = 24, bw_len: int = 20, rank_len: int = 120, lag: int = 0) -> Optional[Dict[str, float]]:
    """Measures of the current range: box width in percent and in ATR, the close's position in the box, the
    percentile rank of the Bollinger bandwidth over `rank_len` bars (0 = the narrowest seen), and the slope of EMA50
    in percent over 10 bars. `lag` reads the bandwidth rank `lag` bars back: a break reads the regime BEFORE the bar
    that breaks it, because that bar widens the bandwidth itself. None when there are not enough bars."""
    r = _tail(rows)
    if len(r) < max(n, rank_len // 2) + 12:
        return None
    box = r[-n - 1 : -1]            # the box is the n bars BEFORE the current bar: a break is outside it
    hi = max(float(b[1]) for b in box)
    lo = min(float(b[2]) for b in box)
    c = float(r[-1][3])
    if lo <= 0 or hi <= lo:
        return None
    atr = _atr(r)
    closes = [float(b[3]) for b in r]
    widths: List[float] = []
    for i in range(bw_len - 1, len(closes)):
        w = closes[i - bw_len + 1 : i + 1]
        m = sum(w) / bw_len
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / bw_len)
        widths.append(4.0 * sd / m if m > 0 else 0.0)
    used = widths[: len(widths) - lag] if lag else widths
    hist = used[-rank_len:]
    rank = sum(1 for x in hist if x <= hist[-1]) / len(hist)
    e50 = _ema_list(closes, 50)
    slope = (e50[-1] - e50[-11]) / e50[-11] * 100.0 if e50[-11] > 0 else 0.0
    return {
        "hi": hi, "lo": lo, "close": c,
        "width_pct": (hi - lo) / lo * 100.0,
        "width_atr": (hi - lo) / atr if atr > 0 else 99.0,
        "pos": (c - lo) / (hi - lo),
        "bw_rank": rank,
        "slope_pct": slope,
        "atr_pct": atr / c * 100.0 if c > 0 else 0.0,
    }


def low_range_levels(rows: Sequence[Row], cost_pct: float, step: int = 2) -> Optional[Dict[str, float]]:
    """Stop and target distances (percent of price) for a low-range entry, each with the PositionCost added: the
    stop is the box width plus one PositionCost (never under the overall minimum of two), the target is `step`
    PositionCosts on top of the cost."""
    st = low_range_state(rows)
    if st is None:
        return None
    sl = max(st["width_pct"] + cost_pct, 2.0 * cost_pct)
    return {"range_pct": st["width_pct"], "sl_pct": sl, "tp_pct": step * cost_pct + cost_pct}


def lr_fade(rows: Sequence[Row], max_atr: float = 3.0, edge: float = 0.12) -> Signal:
    """Inside a low range (box no wider than max_atr ATR, bandwidth in the bottom 35%) the close touches the lower or
    upper edge of the box with RSI(14) leaning the same way: fade it."""
    s = low_range_state(rows, lag=1)
    if s is None or s["width_atr"] > max_atr or s["bw_rank"] > 0.35:
        return None
    if abs(s["slope_pct"]) > 0.25:      # a sloping market is a trend, not a range
        return None
    rsi = _rsi_series([float(b[3]) for b in _tail(rows)], 14)[-1]
    if s["pos"] <= edge and rsi < 40:
        return 1, _conf(0.6, (0.5 - s["pos"]) * 0.2)
    if s["pos"] >= 1.0 - edge and rsi > 60:
        return -1, _conf(0.6, (s["pos"] - 0.5) * 0.2)
    return None


def lr_break(rows: Sequence[Row], max_atr: float = 3.0, vol_mult: float = 1.2) -> Signal:
    """A close outside the box of a low range, on a volume above the box average: the break."""
    s = low_range_state(rows, lag=1)
    if s is None or s["width_atr"] > max_atr or s["bw_rank"] > 0.35:
        return None
    r = _tail(rows)
    avg_v = sum(float(b[4]) for b in r[-25:-1]) / 24.0
    if avg_v <= 0 or float(r[-1][4]) < vol_mult * avg_v:
        return None
    if s["close"] > s["hi"]:
        return 1, _conf(0.62, 0.0)
    if s["close"] < s["lo"]:
        return -1, _conf(0.62, 0.0)
    return None


def lr_trendbreak(rows: Sequence[Row], max_atr: float = 3.5) -> Signal:
    """The box break, taken only in the direction of the EMA50 slope (the trend the range paused)."""
    s = low_range_state(rows, lag=1)
    if s is None or s["width_atr"] > max_atr or s["bw_rank"] > 0.5:
        return None
    if s["close"] > s["hi"] and s["slope_pct"] > 0.02:
        return 1, _conf(0.62, min(0.15, s["slope_pct"] * 0.2))
    if s["close"] < s["lo"] and s["slope_pct"] < -0.02:
        return -1, _conf(0.62, min(0.15, -s["slope_pct"] * 0.2))
    return None


def lr_nr7(rows: Sequence[Row]) -> Signal:
    """NR7 (Crabel: the narrowest high-low of the last 7 bars, true range not used) one bar ago, and this bar closes
    beyond that bar's high or low."""
    r = _tail(rows)
    if len(r) < 10:
        return None
    sig = r[-2]
    rng = float(sig[1]) - float(sig[2])
    prior = [float(b[1]) - float(b[2]) for b in r[-8:-2]]
    if rng <= 0 or rng >= min(prior):      # strictly the narrowest of the last 7: a tie is not an NR7
        return None
    c = float(r[-1][3])
    if c > float(sig[1]):
        return 1, 0.62
    if c < float(sig[2]):
        return -1, 0.62
    return None


def lr_inside(rows: Sequence[Row]) -> Signal:
    """Inside bar inside a low range: the break of the mother bar."""
    s = low_range_state(rows, lag=1)
    if s is None or s["width_atr"] > 4.0 or s["bw_rank"] > 0.5:
        return None
    r = _tail(rows)
    mother, inner = r[-3], r[-2]
    if not (float(inner[1]) < float(mother[1]) and float(inner[2]) > float(mother[2])):
        return None
    c = float(r[-1][3])
    if c > float(mother[1]):
        return 1, 0.62
    if c < float(mother[2]):
        return -1, 0.62
    return None


INDICATORS: Dict[str, Callable[[Sequence[Row]], Signal]] = {
    "supertrend": supertrend,
    "adx_trend": adx_trend,
    "macd_cross": macd_cross,
    "pullback_trend": pullback_trend,
    "donchian_break": donchian_break,
    "squeeze_release": squeeze_release,
    "range_expansion": range_expansion,
    "rsi2_reversion": rsi2_reversion,
    "stoch_rsi_cross": stoch_rsi_cross,
    "range_position": range_position,
    "vwap_revert": vwap_revert,
    "vwap_trend": vwap_trend,
    "volume_momentum": volume_momentum,
    "move_continuation": move_continuation,
    "move_fade": move_fade,
    "engulfing": engulfing,
    "streak_exhaustion": streak_exhaustion,
    "lr_fade": lr_fade,
    "lr_break": lr_break,
    "lr_trendbreak": lr_trendbreak,
    "lr_nr7": lr_nr7,
    "lr_inside": lr_inside,
}


def extra_votes(rows: Sequence[Row], names: Sequence[str]) -> List[Tuple[int, float, str]]:
    """(direction, confidence, name) for each listed indicator that has a signal. Unknown names are ignored; one
    indicator failing never takes the others down."""
    out: List[Tuple[int, float, str]] = []
    for name in names:
        fn = INDICATORS.get(str(name))
        if fn is None:
            continue
        try:
            sig = fn(rows)
        except Exception:
            sig = None
        if sig:
            out.append((int(sig[0]), float(sig[1]), str(name)))
    return out
