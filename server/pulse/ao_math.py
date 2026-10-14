"""Indicator math ported from CTS-A-O (src/core/math/indicators.ts).

Every function follows the TypeScript loop and operation order so results
are bit-identical to A-O: the hourly lane's indications are trusted only
because A-O measured them, so the port must not drift. Inputs and outputs
are float64 numpy arrays; warm-up values are NaN, as in A-O.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Dict, Tuple

import numpy as np

NAN = float("nan")


def _nan(n: int) -> np.ndarray:
    return np.full(n, NAN, dtype=np.float64)


def _isfinite(x: float) -> bool:
    return not (math.isnan(x) or math.isinf(x))


def sma(x: np.ndarray, p: int) -> np.ndarray:
    xs = x.tolist()
    out = [NAN] * len(xs)
    s = 0.0
    for i, v in enumerate(xs):
        s += v
        if i >= p:
            s -= xs[i - p]
        if i >= p - 1:
            out[i] = s / p
    return np.array(out, dtype=np.float64)


def ema(x: np.ndarray, p: int) -> np.ndarray:
    """EMA seeded with the SMA of the first p values from the first finite one."""
    xs = x.tolist()
    n = len(xs)
    out = [NAN] * n
    k = 2 / (p + 1)
    start = 0
    while start < n and not _isfinite(xs[start]):
        start += 1
    prev = NAN
    s = 0.0
    for i in range(start, n):
        j = i - start
        if j < p - 1:
            s += xs[i]
            continue
        if j == p - 1:
            prev = (s + xs[i]) / p
        else:
            prev = xs[i] * k + prev * (1 - k)
        out[i] = prev
    return np.array(out, dtype=np.float64)


def rma(x: np.ndarray, p: int, start: int = 0) -> np.ndarray:
    """Wilder smoothing."""
    xs = x.tolist()
    out = [NAN] * len(xs)
    s = 0.0
    cnt = 0
    prev = NAN
    for i in range(start, len(xs)):
        if cnt < p:
            s += xs[i]
            cnt += 1
            if cnt == p:
                prev = s / p
                out[i] = prev
            continue
        prev = (prev * (p - 1) + xs[i]) / p
        out[i] = prev
    return np.array(out, dtype=np.float64)


def rsi(c: np.ndarray, p: int) -> np.ndarray:
    cs = c.tolist()
    n = len(cs)
    up = [0.0] * n
    dn = [0.0] * n
    for i in range(1, n):
        d = cs[i] - cs[i - 1]
        up[i] = d if d > 0 else 0.0
        dn[i] = -d if d < 0 else 0.0
    au = rma(np.array(up), p, 1).tolist()
    ad = rma(np.array(dn), p, 1).tolist()
    out = [NAN] * n
    for i in range(n):
        if math.isnan(au[i]):
            continue
        out[i] = (50.0 if au[i] == 0 else 100.0) if ad[i] == 0 else 100 - 100 / (1 + au[i] / ad[i])
    return np.array(out, dtype=np.float64)


def true_range(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    hs, ls, cs = h.tolist(), l.tolist(), c.tolist()
    out = [0.0] * len(cs)
    for i in range(len(cs)):
        hl = hs[i] - ls[i]
        if i == 0:
            out[i] = hl
            continue
        out[i] = max(hl, abs(hs[i] - cs[i - 1]), abs(ls[i] - cs[i - 1]))
    return np.array(out, dtype=np.float64)


def atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, p: int) -> np.ndarray:
    return rma(true_range(h, l, c), p)


def atr_ema(h: np.ndarray, l: np.ndarray, c: np.ndarray, p: int) -> np.ndarray:
    return ema(true_range(h, l, c), p)


def macd(c: np.ndarray, fast: int = 12, slow: int = 26, sig: int = 9) -> Dict[str, np.ndarray]:
    ef, es = ema(c, fast), ema(c, slow)
    line = ef - es
    n = len(c)
    signal = _nan(n)
    finite = np.flatnonzero(~np.isnan(line))
    if len(finite):
        first = int(finite[0])
        signal[first:] = ema(line[first:], sig)
    return {"line": line, "signal": signal, "hist": line - signal}


def stdev(x: np.ndarray, p: int) -> np.ndarray:
    """Population standard deviation from running sums (A-O's method)."""
    xs = x.tolist()
    out = [NAN] * len(xs)
    s = 0.0
    s2 = 0.0
    for i, v in enumerate(xs):
        s += v
        s2 += v * v
        if i >= p:
            w = xs[i - p]
            s -= w
            s2 -= w * w
        if i >= p - 1:
            m = s / p
            out[i] = math.sqrt(max(0.0, s2 / p - m * m))
    return np.array(out, dtype=np.float64)


def bollinger(c: np.ndarray, p: int = 20, k: float = 2) -> Dict[str, np.ndarray]:
    mid = sma(c, p)
    sd = stdev(c, p)
    with np.errstate(invalid="ignore", divide="ignore"):
        up = mid + k * sd
        lo = mid - k * sd
        width = (up - lo) / mid
    return {"mid": mid, "up": up, "lo": lo, "width": width}


def dmi(h: np.ndarray, l: np.ndarray, c: np.ndarray, p: int = 14) -> Dict[str, np.ndarray]:
    hs, ls = h.tolist(), l.tolist()
    n = len(hs)
    pdm = [0.0] * n
    mdm = [0.0] * n
    for i in range(1, n):
        u = hs[i] - hs[i - 1]
        d = ls[i - 1] - ls[i]
        pdm[i] = u if (u > d and u > 0) else 0.0
        mdm[i] = d if (d > u and d > 0) else 0.0
    st = rma(true_range(h, l, c), p, 1).tolist()
    sp = rma(np.array(pdm), p, 1).tolist()
    sm = rma(np.array(mdm), p, 1).tolist()
    pdi = [NAN] * n
    mdi = [NAN] * n
    dx = [NAN] * n
    first = -1
    for i in range(n):
        if math.isnan(st[i]) or st[i] == 0:
            continue
        pdi[i] = (100 * sp[i]) / st[i]
        mdi[i] = (100 * sm[i]) / st[i]
        s = pdi[i] + mdi[i]
        dx[i] = 0.0 if s == 0 else (100 * abs(pdi[i] - mdi[i])) / s
        if first < 0:
            first = i
    adx = _nan(n) if first < 0 else rma(np.array(dx), p, first)
    return {"adx": adx, "pdi": np.array(pdi), "mdi": np.array(mdi)}


def donchian_prior(h: np.ndarray, l: np.ndarray, p: int) -> Tuple[np.ndarray, np.ndarray]:
    """High / low of the p bars before bar i (the current bar excluded)."""
    hs, ls = h.tolist(), l.tolist()
    n = len(hs)
    hi = [NAN] * n
    lo = [NAN] * n
    dh: deque = deque()
    dl: deque = deque()
    for i in range(n):
        if i >= p:
            hi[i] = hs[dh[0]]
            lo[i] = ls[dl[0]]
        while dh and hs[dh[-1]] <= hs[i]:
            dh.pop()
        dh.append(i)
        if dh[0] <= i - p:
            dh.popleft()
        while dl and ls[dl[-1]] >= ls[i]:
            dl.pop()
        dl.append(i)
        if dl[0] <= i - p:
            dl.popleft()
    return np.array(hi), np.array(lo)


def cci(h: np.ndarray, l: np.ndarray, c: np.ndarray, p: int = 20) -> np.ndarray:
    hs, ls, cs = h.tolist(), l.tolist(), c.tolist()
    n = len(cs)
    tp = [(hs[i] + ls[i] + cs[i]) / 3 for i in range(n)]
    m = sma(np.array(tp, dtype=np.float64), p).tolist()
    out = [NAN] * n
    for i in range(p - 1, n):
        md = 0.0
        mi = m[i]
        for j in range(i - p + 1, i + 1):
            md += abs(tp[j] - mi)
        md /= p
        out[i] = (tp[i] - mi) / (0.015 * md) if md > 0 else 0.0
    return np.array(out, dtype=np.float64)


def mfi(h: np.ndarray, l: np.ndarray, c: np.ndarray, v: np.ndarray, p: int = 14) -> np.ndarray:
    hs, ls, cs, vs = h.tolist(), l.tolist(), c.tolist(), v.tolist()
    n = len(cs)
    tp = [(hs[i] + ls[i] + cs[i]) / 3 for i in range(n)]
    out = [NAN] * n
    for i in range(p, n):
        pos = 0.0
        neg = 0.0
        for j in range(i - p + 1, i + 1):
            f = tp[j] * vs[j]
            if tp[j] > tp[j - 1]:
                pos += f
            elif tp[j] < tp[j - 1]:
                neg += f
        out[i] = 100.0 if neg == 0 else 100 - 100 / (1 + pos / neg)
    return np.array(out, dtype=np.float64)


def keltner(h: np.ndarray, l: np.ndarray, c: np.ndarray, p: int = 20, m: float = 2) -> Dict[str, np.ndarray]:
    mid = ema(c, p)
    a = atr(h, l, c, p)
    return {"mid": mid, "up": mid + m * a, "lo": mid - m * a}


def zscore(x: np.ndarray, p: int = 20) -> np.ndarray:
    m = sma(x, p)
    s = stdev(x, p)
    out = _nan(len(x))
    with np.errstate(invalid="ignore"):
        ok = s > 0
    out[ok] = (x[ok] - m[ok]) / s[ok]
    return out


def roc(c: np.ndarray, p: int) -> np.ndarray:
    out = _nan(len(c))
    if len(c) > p:
        out[p:] = c[p:] / c[:-p] - 1
    return out


def range_sma(h: np.ndarray, l: np.ndarray, p: int) -> np.ndarray:
    return sma(h - l, p)
