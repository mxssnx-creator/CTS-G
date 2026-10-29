"""Indication library: many causal indicator families, each at the 1m, 5m and 15m series, at minimal and medial ranges,
alone and combined across timeframes, plus group votes. Every function is a pure function of the newest 1m rows
[o, h, l, c, v] (newest last, at most WINDOW rows) and returns (direction, confidence) or None; nothing looks ahead.

Families (sources: Kaufman's efficiency ratio and adaptive MA, Dreiss' choppiness index, Chande's Aroon, Botes and
Siepman's Vortex, Wilder's ADX / parabolic SAR / RSI, Williams' / Lane's oscillators, Bollinger and Keltner bands (TTM
squeeze family), Donchian channels, Crabel's narrow-range and inside-bar breakouts, Granville's OBV, Chaikin money flow,
Quong and Soudack's MFI, VWAP deviation, Heikin-Ashi, Ichimoku, pivot points, Fibonacci retracements) and
price-structure measures built on relations and ratios: higher-high / higher-low counts, close location, body ratio,
up/down volume ratio, ATR expansion ratio and last-part acceleration (the last third of the window against the rest).

Names: <family>_n<range>_t<minutes> (single), <family>_c<range> (the three timeframes combined: at least two agree and
none disagrees) and g_<group>_<min|med> (group votes across families and timeframes). Minimal ranges 5 and 8 bars, medial
14 and 21; on the 15m series the window (300 one-minute rows = 20 bars) limits the range, and a range that does not fit
returns None."""
from __future__ import annotations

import math
import threading
from typing import Callable, Dict, List, Optional, Sequence, Tuple

Row = Sequence[float]
Signal = Optional[Tuple[int, float]]
WINDOW = 300
TF_BARS = 96                    # bars of a higher timeframe the library reads (the longest family, htfpb, needs 96): 480 rows for 5m, 1440 for 15m


def rows_for(tf: int) -> int:
    """1m rows a tf-minute series is built from: the 300-row window, or TF_BARS bars of the timeframe when that is longer."""
    return max(WINDOW, TF_BARS * int(tf))


HISTORY = rows_for(15)           # the most 1m rows any library name reads (its 15m series)
RANGES = {"min": (5, 8), "med": (14, 21)}
TFS = (1, 5, 15)

_tls = threading.local()


def _conf(base: float, extra: float, cap: float = 0.93) -> float:
    return max(0.5, min(cap, base + max(0.0, extra)))


def _agg(rows: Sequence[Row], tf: int) -> List[List[float]]:
    """Causal tf-minute bars of the newest rows_for(tf) rows, aligned to the newest row. Memoised in one thread-local slot keyed by the rows
    object, its length and its newest row: a list that grew (or changed its newest row) in place is aggregated again, never served
    the bars of its earlier state; every family of one evaluation shares the slot."""
    key = (len(rows), id(rows[-1]), tuple(rows[-1][:5])) if len(rows) else (0, 0, ())
    slot = getattr(_tls, "slot", None)
    if slot is None or slot[0] is not rows or slot[2] != key:
        slot = (rows, {}, key)
        _tls.slot = slot
    got = slot[1].get(tf)
    if got is not None:
        return got
    if len(rows) > rows_for(tf):
        rows = rows[-rows_for(tf):]
    if tf == 1:
        out = [[float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])] for r in rows]
    else:
        n = len(rows)
        k = n // tf
        out = []
        for j in range(k):
            hi = n - (k - 1 - j) * tf
            blk = rows[hi - tf : hi]
            out.append([float(blk[0][0]), max(float(b[1]) for b in blk), min(float(b[2]) for b in blk), float(blk[-1][3]),
                        sum(float(b[4]) for b in blk)])
    slot[1][tf] = out
    return out


def _tr(b: Sequence[Row]) -> List[float]:
    out = [float(b[0][1]) - float(b[0][2])]
    for i in range(1, len(b)):
        h, lo, pc = float(b[i][1]), float(b[i][2]), float(b[i - 1][3])
        out.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    return out


def _atr(b: Sequence[Row], n: int) -> float:
    t = _tr(b)[-n:]
    return sum(t) / len(t) if t else 0.0


def _ema(vals: Sequence[float], n: int) -> List[float]:
    k = 2.0 / (n + 1.0)
    e = float(vals[0])
    out = []
    for v in vals:
        e = float(v) * k + e * (1 - k)
        out.append(e)
    return out


def _wma(vals: Sequence[float], n: int) -> List[float]:
    out = []
    for i in range(len(vals)):
        lo = max(0, i - n + 1)
        w = vals[lo : i + 1]
        den = len(w) * (len(w) + 1) / 2.0
        out.append(sum((j + 1) * v for j, v in enumerate(w)) / den)
    return out


def _rsi(c: Sequence[float], n: int) -> List[float]:
    out = [50.0] * len(c)
    if len(c) <= n:
        return out
    g = l_ = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        g += max(d, 0.0)
        l_ += max(-d, 0.0)
    g /= n
    l_ /= n
    out[n] = 100.0 if l_ == 0 else 100.0 - 100.0 / (1 + g / l_)
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        g = (g * (n - 1) + max(d, 0.0)) / n
        l_ = (l_ * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if l_ == 0 else 100.0 - 100.0 / (1 + g / l_)
    return out


def _cl(b):
    return [float(x[3]) for x in b]


def _hi(b):
    return [float(x[1]) for x in b]


def _lo(b):
    return [float(x[2]) for x in b]


def _vo(b):
    return [float(x[4]) for x in b]


def _band_k(n: int) -> float:
    """Band width in standard deviations a close can reach inside its own n-bar window: a sample point is at most sqrt(n - 1)
    population deviations from the mean, so 2 sd is out of reach at n = 5 (the signal could never fire). 2 from n = 10 on."""
    return min(2.0, 0.7 * math.sqrt(max(1, n - 1)))


def _side(x: float) -> int:
    return 1 if x > 0 else -1


# ---- trend ------------------------------------------------------------------------------------------------------

def f_ker(b, n):
    if len(b) < n + 2:
        return None
    c = _cl(b)[-n - 1 :]
    path = sum(abs(c[i] - c[i - 1]) for i in range(1, len(c)))
    atr = _atr(b, n)
    if path <= 0 or atr <= 0:
        return None
    er = abs(c[-1] - c[0]) / path
    if er < 0.35 or abs(c[-1] - c[0]) < 1.0 * atr:
        return None
    return _side(c[-1] - c[0]), _conf(0.58, 0.3 * (er - 0.35) / 0.65)


def f_chop(b, n):
    if len(b) < n + 2:
        return None
    w = b[-n:]
    rng = max(_hi(w)) - min(_lo(w))
    if rng <= 0:
        return None
    chop = 100.0 * math.log10(max(1e-12, sum(_tr(b)[-n:]) / rng)) / math.log10(n)
    c = _cl(b)
    e = _ema(c, n)
    if chop > 42.0 or abs(c[-1] - c[-n]) <= 0:
        return None
    s = _side(c[-1] - c[-n])
    if s * (c[-1] - e[-1]) <= 0:
        return None
    return s, _conf(0.58, (42.0 - chop) / 100.0)


def f_aroon(b, n):
    if len(b) < n + 2:
        return None
    h, lo = _hi(b)[-n - 1 :], _lo(b)[-n - 1 :]
    up = 100.0 * (n - (len(h) - 1 - max(range(len(h)), key=lambda i: (h[i], i)))) / n
    dn = 100.0 * (n - (len(lo) - 1 - min(range(len(lo)), key=lambda i: (lo[i], -i)))) / n
    if up >= 70 and dn <= 30:
        return 1, _conf(0.58, (up - dn) / 400.0)
    if dn >= 70 and up <= 30:
        return -1, _conf(0.58, (dn - up) / 400.0)
    return None


def f_vortex(b, n):
    if len(b) < n + 2:
        return None
    vp = vm = trs = 0.0
    tr = _tr(b)
    for i in range(len(b) - n, len(b)):
        vp += abs(float(b[i][1]) - float(b[i - 1][2]))
        vm += abs(float(b[i][2]) - float(b[i - 1][1]))
        trs += tr[i]
    if trs <= 0:
        return None
    a, d = vp / trs, vm / trs
    if a > 1.0 and a > 1.1 * d:
        return 1, _conf(0.58, min(0.25, (a - d) * 0.5))
    if d > 1.0 and d > 1.1 * a:
        return -1, _conf(0.58, min(0.25, (d - a) * 0.5))
    return None


def f_adx(b, n):
    if len(b) < 2 * n + 2:
        return None
    h, lo, c = _hi(b), _lo(b), _cl(b)
    pdm, mdm, tr = [], [], _tr(b)
    for i in range(1, len(b)):
        up, dn = h[i] - h[i - 1], lo[i - 1] - lo[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
    k = 1.0 / n
    sp, sm, st = sum(pdm[:n]), sum(mdm[:n]), sum(tr[1 : n + 1])
    dx = []
    for i in range(n, len(pdm)):
        sp = sp - sp * k + pdm[i]
        sm = sm - sm * k + mdm[i]
        st = st - st * k + tr[i + 1]
        if st <= 0:
            continue
        pi, mi = 100 * sp / st, 100 * sm / st
        dx.append((100 * abs(pi - mi) / max(1e-12, pi + mi), pi, mi))
    if len(dx) < n:
        return None
    adx = sum(x[0] for x in dx[-n:]) / n
    _, pi, mi = dx[-1]
    if adx >= 22 and pi > mi * 1.15:
        return 1, _conf(0.58, (adx - 22) / 100.0)
    if adx >= 22 and mi > pi * 1.15:
        return -1, _conf(0.58, (adx - 22) / 100.0)
    return None


def f_kama(b, n):
    if len(b) < n + 4:
        return None
    c = _cl(b)
    fast, slow = 2.0 / 3.0, 2.0 / 31.0
    k = c[0]
    ks = []
    for i in range(len(c)):
        if i >= n:
            path = sum(abs(c[j] - c[j - 1]) for j in range(i - n + 1, i + 1))
            er = abs(c[i] - c[i - n]) / path if path > 0 else 0.0
            sc = (er * (fast - slow) + slow) ** 2
            k = k + sc * (c[i] - k)
        else:
            k = c[i]
        ks.append(k)
    atr = _atr(b, n)
    if atr <= 0:
        return None
    sl = ks[-1] - ks[-3]
    if c[-1] > ks[-1] and sl > 0.1 * atr:
        return 1, _conf(0.58, min(0.25, sl / atr * 0.2))
    if c[-1] < ks[-1] and sl < -0.1 * atr:
        return -1, _conf(0.58, min(0.25, -sl / atr * 0.2))
    return None


def f_hull(b, n):
    if len(b) < n + 6:
        return None
    c = _cl(b)
    h = _wma([2 * a - bb for a, bb in zip(_wma(c, max(2, n // 2)), _wma(c, n))], max(2, int(math.sqrt(n))))
    atr = _atr(b, n)
    if atr <= 0:
        return None
    s1, s0 = h[-1] - h[-2], h[-2] - h[-3]
    if s1 > 0.05 * atr and s0 <= 0.0:
        return 1, _conf(0.6, min(0.2, s1 / atr))
    if s1 < -0.05 * atr and s0 >= 0.0:
        return -1, _conf(0.6, min(0.2, -s1 / atr))
    return None


def f_ribbon(b, n):
    if len(b) < 2 * n + 2:
        return None
    c = _cl(b)
    a, m, s = _ema(c, max(2, n // 2))[-1], _ema(c, n)[-1], _ema(c, 2 * n)[-1]
    atr = _atr(b, n)
    if atr <= 0:
        return None
    sp = (a - s) / atr
    if a > m > s and sp >= 0.5:
        return 1, _conf(0.58, min(0.25, sp * 0.08))
    if a < m < s and sp <= -0.5:
        return -1, _conf(0.58, min(0.25, -sp * 0.08))
    return None


def f_macdh(b, n):
    if len(b) < 2 * n + 4:
        return None
    c = _cl(b)
    fast, slow, sg = max(2, n // 2), n, max(2, n // 2)
    macd = [x - y for x, y in zip(_ema(c, fast), _ema(c, slow))]
    h = [x - y for x, y in zip(macd, _ema(macd, sg))]
    atr = _atr(b, n)
    if atr <= 0:
        return None
    if h[-2] <= 0 < h[-1] and h[-1] > 0.04 * atr:
        return 1, _conf(0.6, min(0.2, h[-1] / atr))
    if h[-2] >= 0 > h[-1] and -h[-1] > 0.04 * atr:
        return -1, _conf(0.6, min(0.2, -h[-1] / atr))
    return None


def f_roc(b, n):
    if len(b) < n + 2:
        return None
    c = _cl(b)
    r = c[-1] / c[-n - 1] - 1.0
    r2 = c[-1] / c[-max(2, n // 2) - 1] - 1.0
    atrp = _atr(b, n) / c[-1]
    if atrp <= 0:
        return None
    if r > 0.8 * atrp and r2 * 2.0 > r > 0:
        return 1, _conf(0.58, min(0.25, r / atrp * 0.05))
    if r < -0.8 * atrp and r2 * 2.0 < r < 0:
        return -1, _conf(0.58, min(0.25, -r / atrp * 0.05))
    return None


def f_rsimom(b, n):
    if len(b) < n + 3:
        return None
    r = _rsi(_cl(b), n)
    if r[-2] <= 55 < r[-1] and r[-1] > r[-2]:
        return 1, _conf(0.6, (r[-1] - 55) / 100.0)
    if r[-2] >= 45 > r[-1] and r[-1] < r[-2]:
        return -1, _conf(0.6, (45 - r[-1]) / 100.0)
    return None


def f_hhhl(b, n):
    if len(b) < n + 2:
        return None
    h, lo = _hi(b)[-n - 1 :], _lo(b)[-n - 1 :]
    hh = sum(1 for i in range(1, len(h)) if h[i] > h[i - 1])
    hl = sum(1 for i in range(1, len(lo)) if lo[i] > lo[i - 1])
    lh = sum(1 for i in range(1, len(h)) if h[i] < h[i - 1])
    ll = sum(1 for i in range(1, len(lo)) if lo[i] < lo[i - 1])
    if hh >= 0.6 * n and hl >= 0.6 * n:
        return 1, _conf(0.58, (min(hh, hl) / n - 0.6) * 0.6)
    if lh >= 0.6 * n and ll >= 0.6 * n:
        return -1, _conf(0.58, (min(lh, ll) / n - 0.6) * 0.6)
    return None


def f_lastpart(b, n):
    """Last-part acceleration: the move of the last third of the window against the first two thirds."""
    m = max(6, n)
    if len(b) < m + 2:
        return None
    c = _cl(b)[-m - 1 :]
    k = m // 3
    late = c[-1] - c[-k - 1]
    early = (c[-k - 1] - c[0]) * (k / float(m - k))
    atr = _atr(b, n)
    if atr <= 0 or abs(late) < 1.0 * atr:
        return None
    if late * early > 0 and abs(late) >= 1.5 * abs(early):
        return _side(late), _conf(0.6, min(0.2, abs(late) / atr * 0.05))
    return None


def f_fibpb(b, n):
    """Pullback to the 38-62 % zone of the last swing and a turn back in its direction."""
    m = max(8, 2 * n)
    if len(b) < m + 2:
        return None
    w = b[-m:]
    h, lo, c = _hi(w), _lo(w), _cl(w)
    atr = _atr(b, n)
    if atr <= 0:
        return None
    ih, il = h.index(max(h)), lo.index(min(lo))
    if il < ih and h[ih] - lo[il] >= 2.0 * atr and ih < len(w) - 2:
        ret = (h[ih] - c[-1]) / (h[ih] - lo[il])
        if 0.38 <= ret <= 0.62 and c[-1] > c[-2]:
            return 1, _conf(0.6, 0.1)
    if ih < il and h[ih] - lo[il] >= 2.0 * atr and il < len(w) - 2:
        ret = (c[-1] - lo[il]) / (h[ih] - lo[il])
        if 0.38 <= ret <= 0.62 and c[-1] < c[-2]:
            return -1, _conf(0.6, 0.1)
    return None


def f_ha(b, n):
    if len(b) < n + 5:
        return None
    o, h, lo, c = [float(x[0]) for x in b], _hi(b), _lo(b), _cl(b)
    hc = [(o[i] + h[i] + lo[i] + c[i]) / 4.0 for i in range(len(b))]
    ho = [(o[0] + c[0]) / 2.0]
    for i in range(1, len(b)):
        ho.append((ho[-1] + hc[i - 1]) / 2.0)
    k = 3
    if all(hc[-i] > ho[-i] and lo[-i] >= ho[-i] - (h[-i] - lo[-i]) * 0.05 for i in range(1, k + 1)) and hc[-k - 1] <= ho[-k - 1]:
        return 1, _conf(0.6, 0.1)
    if all(hc[-i] < ho[-i] and h[-i] <= ho[-i] + (h[-i] - lo[-i]) * 0.05 for i in range(1, k + 1)) and hc[-k - 1] >= ho[-k - 1]:
        return -1, _conf(0.6, 0.1)
    return None


def f_psar(b, n):
    if len(b) < n + 6:
        return None
    h, lo = _hi(b), _lo(b)
    af0, afm = 0.02, 0.2
    bull = True
    sar, ep, af = lo[0], h[0], af0
    flips = []
    for i in range(1, len(b)):
        sar = sar + af * (ep - sar)
        if bull:
            sar = min(sar, lo[i - 1], lo[i - 2] if i > 1 else lo[i - 1])
            if lo[i] < sar:
                bull, sar, ep, af = False, ep, lo[i], af0
                flips.append((i, -1))
                continue
            if h[i] > ep:
                ep, af = h[i], min(afm, af + af0)
        else:
            sar = max(sar, h[i - 1], h[i - 2] if i > 1 else h[i - 1])
            if h[i] > sar:
                bull, sar, ep, af = True, ep, h[i], af0
                flips.append((i, 1))
                continue
            if lo[i] < ep:
                ep, af = lo[i], min(afm, af + af0)
    if flips and flips[-1][0] >= len(b) - 2:
        return flips[-1][1], _conf(0.6, 0.1)
    return None


def f_ichi(b, n):
    k = 2 * n
    if len(b) < k + 3:
        return None
    h, lo, c = _hi(b), _lo(b), _cl(b)

    def mid(a, bb, i):
        return (max(h[i - a + 1 : i + 1]) + min(lo[i - a + 1 : i + 1])) / 2.0
    t1, k1 = mid(n, 0, len(b) - 1), mid(k, 0, len(b) - 1)
    t0, k0 = mid(n, 0, len(b) - 2), mid(k, 0, len(b) - 2)
    if t0 <= k0 and t1 > k1 and c[-1] > k1:
        return 1, _conf(0.6, 0.1)
    if t0 >= k0 and t1 < k1 and c[-1] < k1:
        return -1, _conf(0.6, 0.1)
    return None


def f_cci(b, n):
    if len(b) < n + 3:
        return None
    tp = [(float(x[1]) + float(x[2]) + float(x[3])) / 3.0 for x in b]

    def cci(i):
        w = tp[i - n + 1 : i + 1]
        m = sum(w) / n
        md = sum(abs(v - m) for v in w) / n
        return (tp[i] - m) / (0.015 * md) if md > 0 else 0.0
    a, p = cci(len(b) - 1), cci(len(b) - 2)
    if p <= 100 < a:
        return 1, _conf(0.6, min(0.2, (a - 100) / 600.0))
    if p >= -100 > a:
        return -1, _conf(0.6, min(0.2, (-100 - a) / 600.0))
    return None


# ---- breakout ------------------------------------------------------------------------------------------------------

def f_donch(b, n):
    if len(b) < n + 2:
        return None
    prior = b[-n - 1 : -1]
    hi, lo = max(_hi(prior)), min(_lo(prior))
    x = b[-1]
    c, h, l_ = float(x[3]), float(x[1]), float(x[2])
    loc = (c - l_) / (h - l_) if h > l_ else 0.5
    atr = _atr(b[:-1], n)
    if atr <= 0:
        return None
    if c > hi and loc >= 0.6:
        return 1, _conf(0.6, min(0.22, (c - hi) / atr * 0.12))
    if c < lo and loc <= 0.4:
        return -1, _conf(0.6, min(0.22, (lo - c) / atr * 0.12))
    return None


def f_bbb(b, n):
    if len(b) < n + 4:
        return None
    c = _cl(b)

    def band(i):
        w = c[i - n + 1 : i + 1]
        m = sum(w) / n
        sd = math.sqrt(sum((v - m) ** 2 for v in w) / n)
        return m, sd
    m, sd = band(len(c) - 1)
    m1, sd1 = band(len(c) - 2)
    m3, sd3 = band(len(c) - 4)
    if sd <= 0 or sd <= sd3:               # the bands must be widening
        return None
    k = _band_k(n)
    if c[-1] > m + k * sd and c[-2] <= m1 + k * sd1:       # the first close outside the band
        return 1, _conf(0.6, min(0.2, (sd / sd3 - 1 if sd3 > 0 else 1.0) * 0.3))
    if c[-1] < m - k * sd and c[-2] >= m1 - k * sd1:
        return -1, _conf(0.6, min(0.2, (sd / sd3 - 1 if sd3 > 0 else 1.0) * 0.3))
    return None


def f_kelt(b, n):
    if len(b) < n + 3:
        return None
    c = _cl(b)
    e = _ema(c, n)
    atr = _atr(b[:-1], n)
    if atr <= 0:
        return None
    if c[-1] > e[-1] + 1.5 * atr and c[-2] <= e[-2] + 1.5 * atr:
        return 1, _conf(0.6, min(0.2, (c[-1] - e[-1]) / atr * 0.06))
    if c[-1] < e[-1] - 1.5 * atr and c[-2] >= e[-2] - 1.5 * atr:
        return -1, _conf(0.6, min(0.2, (e[-1] - c[-1]) / atr * 0.06))
    return None


def f_nr(b, n):
    """Narrow-range bar of the last n (before the newest) followed by a close beyond its range (Crabel NR4 / NR7)."""
    if len(b) < n + 3:
        return None
    w = b[-n - 1 : -1]
    rng = [float(x[1]) - float(x[2]) for x in w]
    if rng[-1] > min(rng) + 1e-12:
        return None
    nb = w[-1]
    c = float(b[-1][3])
    if c > float(nb[1]):
        return 1, _conf(0.6, 0.12)
    if c < float(nb[2]):
        return -1, _conf(0.6, 0.12)
    return None


def f_inside(b, n):
    if len(b) < max(4, n // 2) + 3:
        return None
    m, i1, c = b[-3], b[-2], float(b[-1][3])
    if float(i1[1]) <= float(m[1]) and float(i1[2]) >= float(m[2]):
        if c > float(m[1]):
            return 1, _conf(0.6, 0.1)
        if c < float(m[2]):
            return -1, _conf(0.6, 0.1)
    return None


def f_pivot(b, n):
    if len(b) < 2 * n + 2:
        return None
    prev = b[-2 * n - 1 : -n - 1]
    h, lo, c = max(_hi(prev)), min(_lo(prev)), float(prev[-1][3])
    p = (h + lo + c) / 3.0
    r1, s1 = 2 * p - lo, 2 * p - h
    x = float(b[-1][3])
    y = float(b[-2][3])
    if x > r1 >= y:
        return 1, _conf(0.6, 0.1)
    if x < s1 <= y:
        return -1, _conf(0.6, 0.1)
    return None


def f_atrx(b, n):
    h = max(2, n // 2)
    if len(b) < 2 * n + 2:
        return None
    s, lg = _atr(b, h), _atr(b, 2 * n)
    if lg <= 0 or s / lg < 1.3:
        return None
    c = _cl(b)
    mv = c[-1] - c[-h - 1]
    if abs(mv) < _atr(b, n):
        return None
    return _side(mv), _conf(0.58, min(0.25, (s / lg - 1.3) * 0.3))


def f_rvol(b, n):
    if len(b) < n + 2:
        return None
    v = _vo(b)
    avg = sum(v[-n - 1 : -1]) / n
    if avg <= 0 or v[-1] < 2.0 * avg:
        return None
    x = b[-1]
    o, c, h, lo = float(x[0]), float(x[3]), float(x[1]), float(x[2])
    if h <= lo or abs(c - o) < 0.5 * (h - lo):
        return None
    return _side(c - o), _conf(0.6, min(0.22, (v[-1] / avg - 2.0) * 0.04))


def f_maru(b, n):
    k = 3
    if len(b) < n + k:
        return None
    last = b[-k:]
    ok_up = ok_dn = True
    for x in last:
        o, c, h, lo = float(x[0]), float(x[3]), float(x[1]), float(x[2])
        body = abs(c - o) / (h - lo) if h > lo else 0.0
        ok_up = ok_up and c > o and body >= 0.6
        ok_dn = ok_dn and c < o and body >= 0.6
    if ok_up and float(last[-1][3]) > float(last[0][3]):
        return 1, _conf(0.6, 0.12)
    if ok_dn and float(last[-1][3]) < float(last[0][3]):
        return -1, _conf(0.6, 0.12)
    return None


# ---- reversion -----------------------------------------------------------------------------------------------------

def f_rsirev(b, n):
    if len(b) < n + 3:
        return None
    r = _rsi(_cl(b), n)
    c = _cl(b)
    if min(r[-3:-1]) <= 20 and c[-1] > c[-2] and r[-1] > r[-2]:
        return 1, _conf(0.6, (20 - min(r[-3:-1])) / 100.0)
    if max(r[-3:-1]) >= 80 and c[-1] < c[-2] and r[-1] < r[-2]:
        return -1, _conf(0.6, (max(r[-3:-1]) - 80) / 100.0)
    return None


def f_stoch(b, n):
    if len(b) < n + 5:
        return None
    h, lo, c = _hi(b), _lo(b), _cl(b)

    def k(i):
        a, bb = max(h[i - n + 1 : i + 1]), min(lo[i - n + 1 : i + 1])
        return 100.0 * (c[i] - bb) / (a - bb) if a > bb else 50.0
    ks = [k(i) for i in range(len(b) - 5, len(b))]
    d = sum(ks[-3:]) / 3.0
    dp = sum(ks[-4:-1]) / 3.0
    if ks[-2] <= dp and ks[-1] > d and ks[-1] < 30:
        return 1, _conf(0.6, (30 - ks[-1]) / 150.0)
    if ks[-2] >= dp and ks[-1] < d and ks[-1] > 70:
        return -1, _conf(0.6, (ks[-1] - 70) / 150.0)
    return None


def f_bbrev(b, n):
    if len(b) < n + 2:
        return None
    c = _cl(b)
    w = c[-n - 1 : -1]
    m = sum(w) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in w) / n)
    if sd <= 0:
        return None
    k = _band_k(n)
    if c[-2] < m - k * sd and c[-1] > c[-2]:
        return 1, _conf(0.6, min(0.2, (m - k * sd - c[-2]) / sd * 0.1))
    if c[-2] > m + k * sd and c[-1] < c[-2]:
        return -1, _conf(0.6, min(0.2, (c[-2] - m - k * sd) / sd * 0.1))
    return None


def f_mfi(b, n):
    if len(b) < n + 3:
        return None
    tp = [(float(x[1]) + float(x[2]) + float(x[3])) / 3.0 for x in b]
    v = _vo(b)

    def mfi(i):
        pos = neg = 0.0
        for j in range(i - n + 1, i + 1):
            f = tp[j] * v[j]
            if tp[j] > tp[j - 1]:
                pos += f
            elif tp[j] < tp[j - 1]:
                neg += f
        return 100.0 if neg == 0 and pos > 0 else (50.0 if neg == 0 else 100.0 - 100.0 / (1 + pos / neg))
    a, p = mfi(len(b) - 1), mfi(len(b) - 2)
    if p < 20 and a > p:
        return 1, _conf(0.6, (20 - p) / 100.0)
    if p > 80 and a < p:
        return -1, _conf(0.6, (p - 80) / 100.0)
    return None


def f_vwapz(b, n):
    if len(b) < n + 2:
        return None
    w = b[-n:]
    pv = sum(((float(x[1]) + float(x[2]) + float(x[3])) / 3.0) * float(x[4]) for x in w)
    vs = sum(float(x[4]) for x in w)
    if vs <= 0:
        return None
    vw = pv / vs
    c = _cl(w)
    sd = math.sqrt(sum((v - vw) ** 2 for v in c) / n)
    if sd <= 0:
        return None
    z = (c[-2] - vw) / sd                 # the stretched close, then the turn on the newest one (a turning close is never the extreme)
    k = _band_k(n)
    if z <= -k and c[-1] > c[-2]:
        return 1, _conf(0.6, min(0.2, (-z - k) * 0.08))
    if z >= k and c[-1] < c[-2]:
        return -1, _conf(0.6, min(0.2, (z - k) * 0.08))
    return None


def f_zrev(b, n):
    if len(b) < n + 2:
        return None
    c = _cl(b)[-n - 1 :]
    w = c[:-1]
    m = sum(w) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in w) / n)
    if sd <= 0:
        return None
    z = (c[-1] - m) / sd
    if z <= -2.5:
        return 1, _conf(0.58, min(0.2, (-z - 2.5) * 0.1))
    if z >= 2.5:
        return -1, _conf(0.58, min(0.2, (z - 2.5) * 0.1))
    return None


# ---- volume and flow --------------------------------------------------------------------------------------------

def f_obv(b, n):
    if len(b) < n + 3:
        return None
    c, v = _cl(b), _vo(b)
    obv = [0.0]
    for i in range(1, len(b)):
        obv.append(obv[-1] + (v[i] if c[i] > c[i - 1] else -v[i] if c[i] < c[i - 1] else 0.0))
    e = _ema(obv, n)
    tot = sum(v[-n:])
    if tot <= 0:
        return None
    sl = (obv[-1] - obv[-n - 1]) / tot
    if obv[-1] > e[-1] and sl > 0.25 and c[-1] > c[-n - 1]:
        return 1, _conf(0.58, min(0.25, sl * 0.3))
    if obv[-1] < e[-1] and sl < -0.25 and c[-1] < c[-n - 1]:
        return -1, _conf(0.58, min(0.25, -sl * 0.3))
    return None


def f_cmf(b, n):
    if len(b) < n + 3:
        return None

    def cmf(i):
        num = den = 0.0
        for j in range(i - n + 1, i + 1):
            h, lo, c, vv = float(b[j][1]), float(b[j][2]), float(b[j][3]), float(b[j][4])
            mf = ((c - lo) - (h - c)) / (h - lo) if h > lo else 0.0
            num += mf * vv
            den += vv
        return num / den if den > 0 else 0.0
    a, p = cmf(len(b) - 1), cmf(len(b) - 2)
    if a > 0.15 and a > p:
        return 1, _conf(0.58, min(0.25, a * 0.5))
    if a < -0.15 and a < p:
        return -1, _conf(0.58, min(0.25, -a * 0.5))
    return None


def f_udvol(b, n):
    if len(b) < n + 2:
        return None
    up = dn = 0.0
    for i in range(len(b) - n, len(b)):
        d = float(b[i][3]) - float(b[i][0])
        if d > 0:
            up += float(b[i][4])
        elif d < 0:
            dn += float(b[i][4])
    c = _cl(b)
    if up + dn <= 0:
        return None
    r = up / dn if dn > 0 else 9.0
    if r >= 1.8 and c[-1] > c[-n]:
        return 1, _conf(0.58, min(0.25, (r - 1.8) * 0.08))
    if r <= 0.55 and c[-1] < c[-n]:
        return -1, _conf(0.58, min(0.25, (1 / max(r, 1e-9) - 1.8) * 0.08))
    return None


def f_closeloc(b, n):
    if len(b) < n + 2:
        return None
    w = b[-n:]
    locs = [((float(x[3]) - float(x[2])) / (float(x[1]) - float(x[2]))) if float(x[1]) > float(x[2]) else 0.5 for x in w]
    a = sum(locs) / n
    c = _cl(b)
    if a >= 0.68 and c[-1] > c[-n]:
        return 1, _conf(0.58, (a - 0.68) * 0.8)
    if a <= 0.32 and c[-1] < c[-n]:
        return -1, _conf(0.58, (0.32 - a) * 0.8)
    return None


FAMILIES: Dict[str, Tuple[str, Callable]] = {
    # name: (group, function)
    "ker": ("trend", f_ker), "chop": ("trend", f_chop), "aroon": ("trend", f_aroon), "vortex": ("trend", f_vortex),
    "adx": ("trend", f_adx), "kama": ("trend", f_kama), "hull": ("trend", f_hull), "ribbon": ("trend", f_ribbon),
    "macdh": ("trend", f_macdh), "roc": ("trend", f_roc), "rsimom": ("trend", f_rsimom), "hhhl": ("trend", f_hhhl),
    "lastpart": ("trend", f_lastpart), "fibpb": ("trend", f_fibpb), "ha": ("trend", f_ha), "psar": ("trend", f_psar),
    "ichi": ("trend", f_ichi), "cci": ("trend", f_cci),
    "donch": ("break", f_donch), "bbb": ("break", f_bbb), "kelt": ("break", f_kelt), "nr": ("break", f_nr),
    "inside": ("break", f_inside), "pivot": ("break", f_pivot), "atrx": ("break", f_atrx), "rvol": ("break", f_rvol),
    "maru": ("break", f_maru),
    "rsirev": ("revert", f_rsirev), "stoch": ("revert", f_stoch), "bbrev": ("revert", f_bbrev), "mfi": ("revert", f_mfi),
    "vwapz": ("revert", f_vwapz), "zrev": ("revert", f_zrev),
    "obv": ("volume", f_obv), "cmf": ("volume", f_cmf), "udvol": ("volume", f_udvol), "closeloc": ("volume", f_closeloc),
}


def _combine(sigs: List[Signal], need: int = 2) -> Signal:
    fired = [s for s in sigs if s]
    up = [s for s in fired if s[0] > 0]
    dn = [s for s in fired if s[0] < 0]
    if up and dn:
        return None
    side = up or dn
    if len(side) < need:
        return None
    return side[0][0], _conf(max(x[1] for x in side), 0.03 * (len(side) - 1), 0.95)


def _single(fam: str, n: int, tf: int) -> Callable[[Sequence[Row]], Signal]:
    fn = FAMILIES[fam][1]

    def run(rows: Sequence[Row]) -> Signal:
        return fn(_agg(rows, tf), n)       # _agg reads the newest rows_for(tf) rows
    return run


def _multi(fam: str, n: int) -> Callable[[Sequence[Row]], Signal]:
    singles = [_single(fam, n, tf) for tf in TFS]
    return lambda rows: _combine([s(rows) for s in singles], 2)


def _group(group: Optional[str], rng: str, need: int = 3) -> Callable[[Sequence[Row]], Signal]:
    """Votes of every family of the group (all when None), at the range's two sizes on the three timeframes."""
    parts = [_single(fam, n, tf) for fam, (g, _) in FAMILIES.items() if group is None or g == group
             for n in RANGES[rng] for tf in TFS]

    def run(rows: Sequence[Row]) -> Signal:
        votes = [s for s in (p(rows) for p in parts) if s]
        up = [s for s in votes if s[0] > 0]
        dn = [s for s in votes if s[0] < 0]
        side = up if len(up) > len(dn) else dn
        other = len(votes) - len(side)
        if len(side) < need or other * 3 > len(side):
            return None
        return side[0][0], _conf(0.58, 0.03 * (len(side) - need) + 0.1 * (len(side) - other) / max(1, len(votes)), 0.95)
    return run


def build() -> Dict[str, Callable[[Sequence[Row]], Signal]]:
    out: Dict[str, Callable[[Sequence[Row]], Signal]] = {}
    for fam in FAMILIES:
        for rng, ns in RANGES.items():
            for n in ns:
                for tf in TFS:
                    out[f"{fam}_n{n}_t{tf}"] = _single(fam, n, tf)
                out[f"{fam}_c{n}"] = _multi(fam, n)
    for group in ("trend", "break", "revert", "volume", None):
        for rng in RANGES:
            out[f"g_{group or 'all'}_{rng}"] = _group(group, rng)
    return out


LIB = build()
GROUPS = {fam: g for fam, (g, _) in FAMILIES.items()}
