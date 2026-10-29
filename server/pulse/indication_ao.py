"""Indicators ported from the CTS-A-O project (src/core/indications: research3/4/5, range-families), re-implemented here as pure
causal functions on the tf-minute series built from the newest 1m rows, at ranges 5/8/14/21 on 1m/5m/15m, alone (`ao_<family>_n<range>_t<tf>`),
across the three timeframes (`ao_<family>_c<range>`) and as group votes (`g_ao_<min|med>`). Same row format, window and rules as
indication_lib: (direction, confidence) or None, nothing looks ahead.

Ported families: laguerre RSI (Ehlers), ultimate oscillator (Williams), Klinger volume oscillator, Relative Vigor Index cross,
FRAMA pullback, Adaptive Price Zone reclaim (ADX below 25), Supertrend pullback, confluence trend (EMA, MACD histogram, ADX/DI),
confluence break (Donchian, 1.5x volume, outer 30 % of the bar), higher-timeframe pullback to EMA, volatility-regime tactic
(ATR rank: break when high, fade a 2-sigma stretch when low) and compression break (Bollinger width in its lowest fifth, then a close
outside the band). The ranks and windows scale with the series length so the 300-row window can carry them on every timeframe."""
from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from indication_lib import (RANGES, TFS, Row, Signal, _agg, _atr, _combine, _conf, _cl, _ema, _hi, _lo, _tr, _vo,
                            f_adx)

# ---- helpers -------------------------------------------------------------------------------------------------------

def _rank(x: Sequence[float], w: int) -> Optional[float]:
    """Percentile rank (0-1) of the newest value among the last w, ties counted half; None until w finite values."""
    if len(x) < w:
        return None
    win = x[-w:]
    cur = win[-1]
    below = sum(1.0 if v < cur else 0.5 if v == cur else 0.0 for v in win)
    return below / w


def _supertrend(b: Sequence[Row], period: int, mult: float) -> Tuple[List[int], List[float]]:
    """Direction (+1/-1) and line per bar, standard ATR band supertrend."""
    n = len(b)
    h, lo, c = _hi(b), _lo(b), _cl(b)
    tr = _tr(b)
    atr = [0.0] * n
    for i in range(n):
        w = tr[max(0, i - period + 1): i + 1]
        atr[i] = sum(w) / len(w)
    ub = [0.0] * n
    lb = [0.0] * n
    d = [1] * n
    line = [0.0] * n
    for i in range(n):
        mid = (h[i] + lo[i]) / 2.0
        u, l_ = mid + mult * atr[i], mid - mult * atr[i]
        if i == 0:
            ub[i], lb[i] = u, l_
            line[i] = l_
            continue
        ub[i] = u if (u < ub[i - 1] or c[i - 1] > ub[i - 1]) else ub[i - 1]
        lb[i] = l_ if (l_ > lb[i - 1] or c[i - 1] < lb[i - 1]) else lb[i - 1]
        if d[i - 1] == 1:
            d[i] = -1 if c[i] < lb[i] else 1
        else:
            d[i] = 1 if c[i] > ub[i] else -1
        line[i] = lb[i] if d[i] == 1 else ub[i]
    return d, line


# ---- oscillators ----------------------------------------------------------------------------------------------------

def a_laguerre(b, n):
    """Ehlers' Laguerre RSI (gamma from the range): leaves oversold (< 0.2) upward / overbought (> 0.8) downward."""
    if len(b) < max(12, n + 6):
        return None
    g = max(0.3, min(0.9, 1.0 - 2.0 / (n + 1.0)))
    c = _cl(b)
    l0 = l1 = l2 = l3 = c[0]
    out: List[float] = []
    for i in range(1, len(c)):
        p0, p1, p2 = l0, l1, l2
        l0 = (1 - g) * c[i] + g * l0
        l1 = -g * l0 + p0 + g * l1
        l2 = -g * l1 + p1 + g * l2
        l3 = -g * l2 + p2 + g * l3
        cu = cd = 0.0
        for d in (l0 - l1, l1 - l2, l2 - l3):
            if d >= 0:
                cu += d
            else:
                cd -= d
        out.append(cu / (cu + cd) if cu + cd > 0 else 0.5)
    if out[-2] <= 0.2 < out[-1]:
        return 1, _conf(0.6, (0.2 - min(out[-4:-1])) * 0.5)
    if out[-2] >= 0.8 > out[-1]:
        return -1, _conf(0.6, (max(out[-4:-1]) - 0.8) * 0.5)
    return None


def a_ultimate(b, n):
    """Williams' ultimate oscillator over n, 2n, 4n: turns up from below 30 / down from above 70."""
    a_, b_, d_ = n, 2 * n, 4 * n
    if len(b) < d_ + 3:
        return None
    h, lo, c = _hi(b), _lo(b), _cl(b)
    bp = [0.0] * len(b)
    tr = [0.0] * len(b)
    for i in range(1, len(b)):
        low = min(lo[i], c[i - 1])
        bp[i] = c[i] - low
        tr[i] = max(h[i], c[i - 1]) - low

    def uo(i):
        def avg(p):
            y = sum(tr[i - p + 1: i + 1])
            return sum(bp[i - p + 1: i + 1]) / y if y > 0 else None
        x, y, z = avg(a_), avg(b_), avg(d_)
        return None if None in (x, y, z) else 100.0 * (4 * x + 2 * y + z) / 7.0
    a, p = uo(len(b) - 1), uo(len(b) - 2)
    if a is None or p is None:
        return None
    if p < 30 and a > p:
        return 1, _conf(0.6, (30 - p) / 100.0)
    if p > 70 and a < p:
        return -1, _conf(0.6, (p - 70) / 100.0)
    return None


def a_klinger(b, n):
    """Klinger volume oscillator (fast n, slow 2n, signal n) crossing its signal line."""
    f, s, sg = n, 2 * n, n
    if len(b) < s + sg + 4:
        return None
    h, lo, c, v = _hi(b), _lo(b), _cl(b), _vo(b)
    vf = [0.0] * len(b)
    trend, cm, dm_prev = 0, 0.0, 0.0
    for i in range(1, len(b)):
        tp, tpp = h[i] + lo[i] + c[i], h[i - 1] + lo[i - 1] + c[i - 1]
        t = 1 if tp > tpp else -1 if tp < tpp else trend
        dm = h[i] - lo[i]
        cm = cm + dm if t == trend else dm_prev + dm
        trend, dm_prev = t, dm
        vf[i] = v[i] * abs(2 * (dm / cm) - 1) * t * 100 if cm > 0 else 0.0
    kvo = [x - y for x, y in zip(_ema(vf, f), _ema(vf, s))]
    sig = _ema(kvo, sg)
    if kvo[-2] <= sig[-2] and kvo[-1] > sig[-1] and kvo[-1] > 0:
        return 1, _conf(0.6, 0.1)
    if kvo[-2] >= sig[-2] and kvo[-1] < sig[-1] and kvo[-1] < 0:
        return -1, _conf(0.6, 0.1)
    return None


def a_rvi(b, n):
    """MetaQuotes Relative Vigor Index with its [1,2,2,1] signal: a new cross with the oscillator on the same side of zero."""
    if len(b) < n + 9:
        return None
    o = [float(x[0]) for x in b]
    h, lo, c = _hi(b), _lo(b), _cl(b)
    num, den = [0.0] * len(b), [0.0] * len(b)
    for i in range(3, len(b)):
        num[i] = (c[i] - o[i]) + 2 * (c[i - 1] - o[i - 1]) + 2 * (c[i - 2] - o[i - 2]) + (c[i - 3] - o[i - 3])
        den[i] = (h[i] - lo[i]) + 2 * (h[i - 1] - lo[i - 1]) + 2 * (h[i - 2] - lo[i - 2]) + (h[i - 3] - lo[i - 3])
    rvi: List[Optional[float]] = [None] * len(b)
    for i in range(n + 2, len(b)):
        d = sum(den[i - n + 1: i + 1])
        rvi[i] = sum(num[i - n + 1: i + 1]) / d if d > 0 else None
    i = len(b) - 1
    seq = [rvi[i - k] for k in range(5)]
    if any(x is None for x in seq):
        return None
    sig_now = (seq[0] + 2 * seq[1] + 2 * seq[2] + seq[3]) / 6.0
    sig_prev = (seq[1] + 2 * seq[2] + 2 * seq[3] + seq[4]) / 6.0
    if seq[1] <= sig_prev and seq[0] > sig_now and seq[0] > 0:
        return 1, _conf(0.6, min(0.2, abs(seq[0] - sig_now) * 2))
    if seq[1] >= sig_prev and seq[0] < sig_now and seq[0] < 0:
        return -1, _conf(0.6, min(0.2, abs(seq[0] - sig_now) * 2))
    return None


# ---- adaptive averages and bands -------------------------------------------------------------------------------------

def _frama(b, period):
    half = period // 2
    h, lo, c = _hi(b), _lo(b), _cl(b)
    out: List[Optional[float]] = [None] * len(b)
    prev = None
    for i in range(len(b)):
        if i < period - 1:
            prev = c[i]
            continue
        h1 = max(h[i - period + 1: i - half + 1]); l1 = min(lo[i - period + 1: i - half + 1])
        h2 = max(h[i - half + 1: i + 1]); l2 = min(lo[i - half + 1: i + 1])
        n1, n2 = (h1 - l1) / half, (h2 - l2) / half
        n3 = (max(h1, h2) - min(l1, l2)) / period
        dim = math.log((n1 + n2) / n3) / math.log(2) if n1 > 0 and n2 > 0 and n3 > 0 else 1.0
        alpha = max(0.01, min(1.0, math.exp(-4.6 * (dim - 1))))
        prev = alpha * c[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def a_frama(b, n):
    """FRAMA pullback: the average rising (falling) over 3 bars, the close dips under it and reclaims it."""
    period = max(4, 2 * n if n < 8 else n + (n % 2))
    if len(b) < period + 6:
        return None
    f = _frama(b, period)
    c = _cl(b)
    if None in (f[-1], f[-2], f[-4]):
        return None
    if f[-1] > f[-4] and c[-2] <= f[-2] and c[-1] > f[-1]:
        return 1, _conf(0.6, 0.1)
    if f[-1] < f[-4] and c[-2] >= f[-2] and c[-1] < f[-1]:
        return -1, _conf(0.6, 0.1)
    return None


def a_apz(b, n):
    """Adaptive price zone (double-smoothed EMA centre and range, width 2) reclaim in a quiet market (ADX below 25)."""
    if len(b) < 2 * n + 30:
        return None
    c = _cl(b)
    centre = _ema(_ema(c, n), n)
    spread = _ema(_ema([x[1] - x[2] for x in b], n), n)
    adx = f_adx_value(b)
    if adx is None or adx >= 25 or spread[-1] <= 0:
        return None
    w = 2.0
    lo_p, up_p = centre[-2] - w * spread[-2], centre[-2] + w * spread[-2]
    if c[-2] <= lo_p and c[-1] > centre[-1] - w * spread[-1]:
        return 1, _conf(0.6, 0.1)
    if c[-2] >= up_p and c[-1] < centre[-1] + w * spread[-1]:
        return -1, _conf(0.6, 0.1)
    return None


def f_adx_value(b, p=14):
    """ADX(14) of the series (Wilder), None when it does not fit."""
    if len(b) < 2 * p + 2:
        return None
    h, lo = _hi(b), _lo(b)
    tr = _tr(b)
    pdm, mdm = [], []
    for i in range(1, len(b)):
        up, dn = h[i] - h[i - 1], lo[i - 1] - lo[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
    k = 1.0 / p
    sp, sm, st = sum(pdm[:p]), sum(mdm[:p]), sum(tr[1: p + 1])
    dx = []
    for i in range(p, len(pdm)):
        sp = sp - sp * k + pdm[i]
        sm = sm - sm * k + mdm[i]
        st = st - st * k + tr[i + 1]
        if st > 0:
            pi, mi = 100 * sp / st, 100 * sm / st
            dx.append(100 * abs(pi - mi) / max(1e-12, pi + mi))
    return sum(dx[-p:]) / p if len(dx) >= p else None


def a_stpb(b, n):
    """Pullback and reclaim of the 9-EMA inside an unchanged Supertrend."""
    if len(b) < max(40, n + 12):
        return None
    d, line = _supertrend(b, n, 2.0)
    c = _cl(b)
    e = _ema(c, 9)
    if d[-1] != d[-2]:
        return None
    if d[-1] == 1 and c[-1] > line[-1] and c[-2] <= e[-2] and c[-1] > e[-1]:
        return 1, _conf(0.6, 0.1)
    if d[-1] == -1 and c[-1] < line[-1] and c[-2] >= e[-2] and c[-1] < e[-1]:
        return -1, _conf(0.6, 0.1)
    return None


# ---- range families --------------------------------------------------------------------------------------------------

def a_cfltrend(b, n):
    """EMA(n) slope over 3 bars, MACD histogram and DI agree with ADX at least 20."""
    if len(b) < max(2 * 26, 3 * n):
        return None
    c = _cl(b)
    e = _ema(c, n)
    macd = [x - y for x, y in zip(_ema(c, 12), _ema(c, 26))]
    hist = [x - y for x, y in zip(macd, _ema(macd, 9))][-1]
    r = f_adx(b, 14)
    adx = f_adx_value(b)
    if adx is None or adx < 20 or r is None:
        return None
    if e[-1] > e[-4] and hist > 0 and r[0] > 0:
        return 1, _conf(0.58, (adx - 20) / 100.0)
    if e[-1] < e[-4] and hist < 0 and r[0] < 0:
        return -1, _conf(0.58, (adx - 20) / 100.0)
    return None


def a_cflbrk(b, n):
    """A close beyond the prior n-bar Donchian band on 1.5x volume, in the bar's outer 30 %."""
    if len(b) < n + 2:
        return None
    prior = b[-n - 1: -1]
    hi, lo = max(_hi(prior)), min(_lo(prior))
    avg_v = sum(_vo(prior)) / n
    x = b[-1]
    c, h, l_, v = float(x[3]), float(x[1]), float(x[2]), float(x[4])
    if avg_v <= 0 or v < 1.5 * avg_v or h <= l_:
        return None
    loc = (c - l_) / (h - l_)
    if c > hi and loc >= 0.7:
        return 1, _conf(0.6, min(0.2, (v / avg_v - 1.5) * 0.05))
    if c < lo and loc <= 0.3:
        return -1, _conf(0.6, min(0.2, (v / avg_v - 1.5) * 0.05))
    return None


def a_htfpb(b, n):
    """Pullback to the EMA(n) inside the 4x timeframe's trend (its close and EMA(8) against EMA(20))."""
    f = 4
    if len(b) < f * 24 or len(b) < n + 5:
        return None
    k = len(b) // f
    hb = []
    for j in range(k):
        hi_ = len(b) - (k - 1 - j) * f
        blk = b[hi_ - f: hi_]
        hb.append([float(blk[0][0]), max(float(x[1]) for x in blk), min(float(x[2]) for x in blk), float(blk[-1][3]), sum(float(x[4]) for x in blk)])
    hc = _cl(hb)
    h8, h20 = _ema(hc, 8)[-1], _ema(hc, 20)[-1]
    c = _cl(b)
    e = _ema(c, n)[-1]
    o, h, l_ = float(b[-1][0]), float(b[-1][1]), float(b[-1][2])
    up = hc[-1] > h20 and h8 > h20
    dn = hc[-1] < h20 and h8 < h20
    if up and l_ <= e and c[-1] > e and c[-1] > o:
        return 1, _conf(0.6, 0.1)
    if dn and h >= e and c[-1] < e and c[-1] < o:
        return -1, _conf(0.6, 0.1)
    return None


def a_regime(b, n):
    """Volatility regime: ATR/close ranked over the last bars. High: a Donchian(n) break. Low: a z(n) +-2 stretch that turns back, faded."""
    w = min(200, len(b) - 20)
    if w < 30 or len(b) < n + 20:
        return None
    c = _cl(b)
    tr = _tr(b)
    rel = []
    for i in range(14, len(b)):
        a = sum(tr[i - 13: i + 1]) / 14.0
        rel.append(a / c[i] if c[i] > 0 else float("nan"))
    r = _rank(rel, w)
    if r is None:
        return None
    if r >= 0.75:
        hi, lo = max(_hi(b[-n - 1: -1])), min(_lo(b[-n - 1: -1]))
        if c[-1] > hi:
            return 1, _conf(0.58, (r - 0.75) * 0.5)
        if c[-1] < lo:
            return -1, _conf(0.58, (r - 0.75) * 0.5)
        return None
    if r <= 0.25:
        win = c[-n - 2: -2]
        m = sum(win) / len(win)
        sd = math.sqrt(sum((x - m) ** 2 for x in win) / len(win))
        if sd <= 0:
            return None
        z = (c[-2] - m) / sd
        if z <= -2 and c[-1] > c[-2]:
            return 1, _conf(0.58, (0.25 - r) * 0.5)
        if z >= 2 and c[-1] < c[-2]:
            return -1, _conf(0.58, (0.25 - r) * 0.5)
    return None


def a_cmpx(b, n):
    """Compression break: Bollinger(20, 2) width in the lowest fifth of its last L bars (the previous bar), then a close outside the band."""
    L = max(30, min(100, len(b) - 25))
    if len(b) < 20 + L:
        return None
    c = _cl(b)

    def band(i):
        w = c[i - 19: i + 1]
        m = sum(w) / 20.0
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / 20.0)
        return m, sd
    widths = []
    for i in range(len(b) - L - 1, len(b) - 1):
        m, sd = band(i)
        widths.append(4 * sd / m if m > 0 else float("nan"))
    r = _rank(widths, L)
    if r is None or r > 0.2:
        return None
    m, sd = band(len(b) - 1)
    if c[-1] > m + 2 * sd:
        return 1, _conf(0.6, (0.2 - r) * 0.5)
    if c[-1] < m - 2 * sd:
        return -1, _conf(0.6, (0.2 - r) * 0.5)
    return None


def a_axisrev(b, n):
    """CTS-A-O's Axis entry: a mean-reversion signal around an axis (EMA centre of the series). The signal points back to the axis
    (price below it: long, above it: short) while the displacement from the axis is between 0.8 and 3 ATR. The target the A-O ladder uses
    is the axis price itself; here the Set lane (TP step, SL ratio, hold) decides the exit and Base validates the Set."""
    if len(b) < n + 6:
        return None
    c = _cl(b)
    centre = _ema(c, n)[-1]
    atr = _atr(b, max(5, min(14, n)))
    if atr <= 0:
        return None
    disp = (c[-1] - centre) / atr
    if -3.0 <= disp <= -0.8 and c[-1] >= c[-2]:
        return 1, _conf(0.58, (-disp - 0.8) * 0.08)
    if 0.8 <= disp <= 3.0 and c[-1] <= c[-2]:
        return -1, _conf(0.58, (disp - 0.8) * 0.08)
    return None


AO_FAMILIES: Dict[str, Tuple[str, Callable]] = {
    "axisrev": ("revert", a_axisrev),
    "laguerre": ("revert", a_laguerre), "ultimate": ("revert", a_ultimate), "klinger": ("volume", a_klinger),
    "rvi": ("trend", a_rvi), "frama": ("trend", a_frama), "apz": ("revert", a_apz), "stpb": ("trend", a_stpb),
    "cfltrend": ("trend", a_cfltrend), "cflbrk": ("break", a_cflbrk), "htfpb": ("trend", a_htfpb),
    "regime": ("break", a_regime), "cmpx": ("break", a_cmpx),
}


def _single(fam: str, n: int, tf: int) -> Callable[[Sequence[Row]], Signal]:
    fn = AO_FAMILIES[fam][1]

    def run(rows: Sequence[Row]) -> Signal:
        return fn(_agg(rows, tf), n)       # _agg reads rows_for(tf) rows (60 bars of a higher timeframe)
    return run


def build() -> Dict[str, Callable[[Sequence[Row]], Signal]]:
    out: Dict[str, Callable[[Sequence[Row]], Signal]] = {}
    for fam in AO_FAMILIES:
        for ns in RANGES.values():
            for n in ns:
                singles = [_single(fam, n, tf) for tf in TFS]
                for tf, s in zip(TFS, singles):
                    out[f"ao_{fam}_n{n}_t{tf}"] = s
                out[f"ao_{fam}_c{n}"] = (lambda rows, ss=singles: _combine([s(rows) for s in ss], 2))
    for rng, ns in RANGES.items():
        parts = [_single(fam, n, tf) for fam in AO_FAMILIES for n in ns for tf in TFS]

        def run(rows, parts=parts):
            votes = [s for s in (p(rows) for p in parts) if s]
            up = [s for s in votes if s[0] > 0]
            dn = [s for s in votes if s[0] < 0]
            side = up if len(up) > len(dn) else dn
            other = len(votes) - len(side)
            if len(side) < 3 or other * 3 > len(side):
                return None
            return side[0][0], _conf(0.58, 0.03 * (len(side) - 3) + 0.1 * (len(side) - other) / max(1, len(votes)), 0.95)
        out[f"g_ao_{rng}"] = run
    return out


AO = build()
