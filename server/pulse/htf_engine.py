"""1h (HTF) lane: the indications, filter and exits CTS-A-O measured as robust.

Ported from CTS-A-O (``src/core/math/indicators.ts``, ``indications/registry.ts``,
``indications/cache.ts``, ``indications/filters.ts``, ``bots/bots.ts``,
``sim/backtest.ts``) with the same conventions:

* every indication is a per-bar state (+1 / -1 / 0) from bars 0..i only;
* ``follow`` enters on state onsets with the state's sign, ``revert`` against it;
* ``@x4`` keeps the state only where the same indication agrees on the 4x
  timeframe built from COMPLETED higher bars (``mtfState`` / ``htfBars``);
* ``volHi``: ATR14/close ranks >= 0.5 among the previous 336 bars;
* a signal decided at the close of bar i enters at the open of bar i+1; inside
  a bar the stop is checked before the target; a gap bar fills at its open;
  the trail arms on completed-bar peaks; the time exit closes at the close;
  r = side * (exit - entry) / entry - cost.

Pure numpy / python, no engine imports: the validation script and the live
trader run exactly this code.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

HOUR_MS = 3_600_000
VOL_RANK_BARS = 336
NAN = float("nan")


# ------------------------------------------------------------------ bars
@dataclass
class Bars:
    """OHLCV columns; ``t`` is the bar open time in ms; ``tf_min`` the bar length."""
    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    tf_min: int = 60
    sym: str = ""

    @property
    def n(self) -> int:
        return len(self.c)

    @classmethod
    def from_rows(cls, rows: Sequence, tf_min: int = 60, sym: str = "") -> "Bars":
        """rows: [[t_ms, [o, h, l, c, v]], ...] or [[t, o, h, l, c, v], ...]."""
        flat = []
        for r in rows:
            if len(r) == 2 and isinstance(r[1], (list, tuple)):
                flat.append([float(r[0]), *[float(x) for x in r[1][:5]]])
            else:
                flat.append([float(x) for x in r[:6]])
        a = np.array(flat, dtype=float).reshape(-1, 6)
        a = a[np.argsort(a[:, 0], kind="stable")]
        return cls(a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5], tf_min, sym)

    def slice(self, lo: int, hi: int) -> "Bars":
        return Bars(self.t[lo:hi], self.o[lo:hi], self.h[lo:hi], self.l[lo:hi], self.c[lo:hi], self.v[lo:hi], self.tf_min, self.sym)


def htf_bars(b: Bars, factor: int) -> Tuple[Bars, np.ndarray]:
    """``factor`` x timeframe bars from COMPLETED buckets only; ``map[i]`` is the
    index of the last higher bar that had closed when bar i closed (-1 = none)."""
    tf_ms = b.tf_min * 60_000
    span = tf_ms * factor
    T, O, H, L, C, V = [], [], [], [], [], []
    mp = np.full(b.n, -1, dtype=np.int64)
    cur = None
    bo = bh = bl = bc = bv = bt = 0.0
    for i in range(b.n):
        bucket = math.floor(b.t[i] / span)
        if bucket != cur:
            cur = bucket
            bt = bucket * span
            bo, bh, bl, bc, bv = b.o[i], b.h[i], b.l[i], b.c[i], b.v[i]
        else:
            bh = max(bh, b.h[i])
            bl = min(bl, b.l[i])
            bc = b.c[i]
            bv += b.v[i]
        if b.t[i] + tf_ms >= bt + span:
            T.append(bt); O.append(bo); H.append(bh); L.append(bl); C.append(bc); V.append(bv)
        mp[i] = len(T) - 1
    hb = Bars(np.array(T, float), np.array(O, float), np.array(H, float), np.array(L, float),
              np.array(C, float), np.array(V, float), b.tf_min * factor, b.sym)
    return hb, mp


# --------------------------------------------------------------- indicators
def sma(x: np.ndarray, p: int) -> np.ndarray:
    out = np.full(len(x), NAN)
    if len(x) >= p:
        cs = np.cumsum(np.r_[0.0, x])
        out[p - 1:] = (cs[p:] - cs[:-p]) / p
    return out


def ema(x: np.ndarray, p: int) -> np.ndarray:
    """SMA seed starting at the first finite value, then k = 2/(p+1)."""
    out = np.full(len(x), NAN)
    k = 2.0 / (p + 1)
    start = 0
    while start < len(x) and not math.isfinite(x[start]):
        start += 1
    s, prev = 0.0, NAN
    for i in range(start, len(x)):
        j = i - start
        if j < p - 1:
            s += x[i]
            continue
        prev = (s + x[i]) / p if j == p - 1 else x[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rma(x: np.ndarray, p: int, start: int = 0) -> np.ndarray:
    """Wilder smoothing."""
    out = np.full(len(x), NAN)
    s, cnt, prev = 0.0, 0, NAN
    for i in range(start, len(x)):
        if cnt < p:
            s += x[i]
            cnt += 1
            if cnt == p:
                prev = s / p
                out[i] = prev
            continue
        prev = (prev * (p - 1) + x[i]) / p
        out[i] = prev
    return out


def rsi(c: np.ndarray, p: int) -> np.ndarray:
    d = np.diff(c, prepend=c[0]) if len(c) else c
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    if len(c):
        up[0] = dn[0] = 0.0
    au, ad = rma(up, p, 1), rma(dn, p, 1)
    out = np.full(len(c), NAN)
    ok = ~np.isnan(au)
    with np.errstate(divide="ignore", invalid="ignore"):
        val = np.where(ad == 0, np.where(au == 0, 50.0, 100.0), 100.0 - 100.0 / (1.0 + au / np.where(ad == 0, 1.0, ad)))
    out[ok] = val[ok]
    return out


def true_range(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    tr = h - l
    if len(c) > 1:
        pc = c[:-1]
        tr[1:] = np.maximum(tr[1:], np.maximum(np.abs(h[1:] - pc), np.abs(l[1:] - pc)))
    return tr


def atr(h, l, c, p: int) -> np.ndarray:
    return rma(true_range(h, l, c), p)


def stdev(x: np.ndarray, p: int) -> np.ndarray:
    """Population sigma over p values."""
    out = np.full(len(x), NAN)
    if len(x) >= p:
        cs = np.cumsum(np.r_[0.0, x])
        cs2 = np.cumsum(np.r_[0.0, x * x])
        m = (cs[p:] - cs[:-p]) / p
        var = (cs2[p:] - cs2[:-p]) / p - m * m
        out[p - 1:] = np.sqrt(np.maximum(0.0, var))
    return out


def bollinger(c: np.ndarray, p: int = 20, k: float = 2.0):
    mid = sma(c, p)
    sd = stdev(c, p)
    return mid, mid + k * sd, mid - k * sd


def dmi(h, l, c, p: int = 14):
    n = len(c)
    pdm = np.zeros(n)
    mdm = np.zeros(n)
    if n > 1:
        u = h[1:] - h[:-1]
        d = l[:-1] - l[1:]
        pdm[1:] = np.where((u > d) & (u > 0), u, 0.0)
        mdm[1:] = np.where((d > u) & (d > 0), d, 0.0)
    tr = true_range(h, l, c)
    st, sp, sm = rma(tr, p, 1), rma(pdm, p, 1), rma(mdm, p, 1)
    pdi = np.full(n, NAN)
    mdi = np.full(n, NAN)
    dx = np.full(n, NAN)
    first = -1
    for i in range(n):
        if math.isnan(st[i]) or st[i] == 0:
            continue
        pdi[i] = 100 * sp[i] / st[i]
        mdi[i] = 100 * sm[i] / st[i]
        s = pdi[i] + mdi[i]
        dx[i] = 0.0 if s == 0 else 100 * abs(pdi[i] - mdi[i]) / s
        if first < 0:
            first = i
    adx = np.full(n, NAN) if first < 0 else rma(dx, p, first)
    return adx, pdi, mdi


def donchian_prior(h, l, p: int):
    """Channel of the p bars BEFORE bar i (the current bar excluded)."""
    n = len(h)
    hi = np.full(n, NAN)
    lo = np.full(n, NAN)
    if n > p:
        w_h = np.lib.stride_tricks.sliding_window_view(h, p)
        w_l = np.lib.stride_tricks.sliding_window_view(l, p)
        hi[p:] = w_h.max(axis=1)[: n - p]
        lo[p:] = w_l.min(axis=1)[: n - p]
    return hi, lo


def cci(h, l, c, p: int = 20) -> np.ndarray:
    tp = (h + l + c) / 3.0
    m = sma(tp, p)
    out = np.full(len(c), NAN)
    if len(c) >= p:
        w = np.lib.stride_tricks.sliding_window_view(tp, p)
        md = np.abs(w - m[p - 1:, None]).mean(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[p - 1:] = np.where(md > 0, (tp[p - 1:] - m[p - 1:]) / (0.015 * md), 0.0)
    return out


def zscore(x: np.ndarray, p: int = 20) -> np.ndarray:
    m, s = sma(x, p), stdev(x, p)
    out = np.full(len(x), NAN)
    ok = s > 0
    out[ok] = (x[ok] - m[ok]) / s[ok]
    return out


# ------------------------------------------------------------------ states
def _state(v: np.ndarray) -> np.ndarray:
    out = np.zeros(len(v), dtype=np.int8)
    fin = np.isfinite(v)
    out[fin & (v > 0)] = 1
    out[fin & (v < 0)] = -1
    return out


def hold(ev: np.ndarray, keep: int) -> np.ndarray:
    """Extend event bars into a state that persists ``keep`` bars (latest wins)."""
    out = np.zeros(len(ev), dtype=np.int8)
    cur, left = 0, 0
    for i, e in enumerate(ev):
        if e != 0:
            cur, left = int(e), keep
        if left > 0:
            out[i] = cur
            left -= 1
    return out


def _rsi_mom(p: int, lvl: int):
    def fn(b: Bars) -> np.ndarray:
        r = rsi(b.c, p)
        with np.errstate(invalid="ignore"):
            return _state(np.where(r > 100 - lvl, 1.0, np.where(r < lvl, -1.0, 0.0)) * np.isfinite(r))
    return fn


def _bb_walk(b: Bars) -> np.ndarray:
    _, up, lo = bollinger(b.c, 20, 2)
    c = b.c
    out = np.zeros(b.n, dtype=np.int8)
    with np.errstate(invalid="ignore"):
        prev_up = np.r_[False, (c[:-1] > up[:-1])]
        prev_lo = np.r_[False, (c[:-1] < lo[:-1])]
        out[(c > up) & prev_up] = 1
        out[~((c > up) & prev_up) & (c < lo) & prev_lo] = -1
    out[0] = 0
    return out


def _break_vol(mul: float):
    def fn(b: Bars) -> np.ndarray:
        hi, lo = donchian_prior(b.h, b.l, 20)
        vs = sma(b.v, 20)
        with np.errstate(invalid="ignore"):
            ev = np.where(b.v > mul * vs, np.where(b.c > hi, 1, np.where(b.c < lo, -1, 0)), 0).astype(np.int8)
        return hold(ev, 6)
    return fn


def _break_atr(mul: float):
    def fn(b: Bars) -> np.ndarray:
        a = atr(b.h, b.l, b.c, 14)
        d = np.diff(b.c, prepend=b.c[0] if b.n else 0.0)
        prev_a = np.r_[NAN, a[:-1]]
        with np.errstate(invalid="ignore"):
            ev = _state(np.where(np.isfinite(prev_a) & (np.abs(d) > mul * prev_a), d, 0.0))
        if b.n:
            ev[0] = 0
        return hold(ev, 4)
    return fn


def _act_burst(mul: float):
    def fn(b: Bars) -> np.ndarray:
        rs = sma(b.h - b.l, 20)
        prev_rs = np.r_[NAN, rs[:-1]]
        with np.errstate(invalid="ignore"):
            ev = _state(np.where((b.h - b.l) > mul * prev_rs, b.c - b.o, 0.0))
        if b.n:
            ev[0] = 0
        return hold(ev, 4)
    return fn


def _act_chop(b: Bars) -> np.ndarray:
    adx, _, _ = dmi(b.h, b.l, b.c, 14)
    r = rsi(b.c, 7)
    with np.errstate(invalid="ignore"):
        v = np.where(np.isfinite(adx) & np.isfinite(r) & (adx < 18), np.where(r < 30, 1.0, np.where(r > 70, -1.0, 0.0)), 0.0)
    return _state(v)


def _cci(p: int, lvl: float):
    def fn(b: Bars) -> np.ndarray:
        x = cci(b.h, b.l, b.c, p)
        with np.errstate(invalid="ignore"):
            return _state(np.where(x < -lvl, 1.0, np.where(x > lvl, -1.0, 0.0)))
    return fn


def _z(p: int, t: float):
    def fn(b: Bars) -> np.ndarray:
        x = zscore(b.c, p)
        with np.errstate(invalid="ignore"):
            return _state(np.where(x < -t, 1.0, np.where(x > t, -1.0, 0.0)))
    return fn


@dataclass(frozen=True)
class KindSpec:
    id: str
    family: str          # "rsi-mom" | "robust"
    bot: str             # "follow" | "revert"
    fn: Callable[[Bars], np.ndarray]
    x4: bool = False     # must agree on the 4x timeframe


def _specs() -> Dict[str, KindSpec]:
    out: Dict[str, KindSpec] = {}
    for p, lvl in ((10, 25), (14, 15), (14, 20), (14, 25), (21, 15), (21, 20), (21, 25)):
        out[f"rsi-mom-{p}-{lvl}"] = KindSpec(f"rsi-mom-{p}-{lvl}", "rsi-mom", "follow", _rsi_mom(p, lvl))
    base = {
        "bb-walk": ("follow", _bb_walk),
        "break-vol": ("follow", _break_vol(1.6)),
        "break-vol-2": ("follow", _break_vol(2.0)),
        "break-atr-2": ("follow", _break_atr(2.0)),
        "act-burst-2.5": ("follow", _act_burst(2.5)),
        "act-chop": ("revert", _act_chop),
        "cci-14-200": ("revert", _cci(14, 200)),
        "cci-40-200": ("revert", _cci(40, 200)),
        "z-50-2.5": ("revert", _z(50, 2.5)),
    }
    for bid, (bot, fn) in base.items():
        out[f"{bid}@x4"] = KindSpec(f"{bid}@x4", "robust", bot, fn, x4=True)
    return out


KINDS: Dict[str, KindSpec] = _specs()
HTF_KINDS: Tuple[str, ...] = tuple(KINDS)
FAMILIES: Tuple[str, ...] = ("rsi-mom", "robust")


def kind_state(spec: KindSpec, b: Bars, htf: Optional[Tuple[Bars, np.ndarray]] = None) -> np.ndarray:
    st = spec.fn(b)
    if not spec.x4:
        return st
    hb, mp = htf if htf is not None else htf_bars(b, 4)
    hs = spec.fn(hb) if hb.n else np.zeros(0, dtype=np.int8)
    out = st.copy()
    for i in np.flatnonzero(out):
        j = mp[i]
        if j < 0 or hs[j] != out[i]:
            out[i] = 0
    return out


def onset_signal(st: np.ndarray, bot: str) -> np.ndarray:
    """Entry on the onset bar of a state; follow takes its sign, revert the opposite."""
    out = np.zeros(len(st), dtype=np.int8)
    if len(st) > 1:
        on = (st[1:] != 0) & (st[1:] != st[:-1])
        out[1:][on] = st[1:][on] * (1 if bot == "follow" else -1)
    return out


def vol_regime_ok(b: Bars, bars: int = VOL_RANK_BARS) -> np.ndarray:
    """ATR14/close percentile rank among the previous ``bars`` values >= 0.5."""
    a = atr(b.h, b.l, b.c, 14)
    with np.errstate(divide="ignore", invalid="ignore"):
        x = a / b.c
    out = np.zeros(b.n, dtype=bool)
    if b.n <= bars:
        return out
    w = np.lib.stride_tricks.sliding_window_view(x, bars)[: b.n - bars]  # window [i-bars, i)
    cur = x[bars:]
    fin = np.isfinite(w)
    cnt = fin.sum(axis=1)
    with np.errstate(invalid="ignore"):
        below = (np.where(fin, w, np.inf) < cur[:, None]).sum(axis=1)
        rank = np.where((cnt > bars / 2) & np.isfinite(cur), below / np.maximum(cnt, 1), np.nan)
        out[bars:] = rank >= 0.5
    return out


def entry_signals(b: Bars, kinds: Iterable[str], vol_regime: bool = True) -> Dict[str, np.ndarray]:
    """Per kind: +1 / -1 entry decided at the close of bar i (enters at i+1 open)."""
    htf = htf_bars(b, 4)
    vr = vol_regime_ok(b) if vol_regime else None
    out = {}
    for k in kinds:
        spec = KINDS[k]
        sig = onset_signal(kind_state(spec, b, htf), spec.bot)
        if vr is not None:
            sig = np.where(vr, sig, 0).astype(np.int8)
        out[k] = sig
    return out


# ------------------------------------------------------------------- exits
@dataclass(frozen=True)
class ExitCfg:
    tp: float          # fraction
    sl: float          # fraction
    hold: int          # bars
    trail: float = 0.0  # arm and give distance as a fraction (0 = no trail)

    @property
    def key(self) -> str:
        t = f"|tr{self.trail * 100:g}" if self.trail > 0 else ""
        return f"tp{self.tp * 100:g}|sl{self.sl * 100:g}|h{self.hold}{t}"

    @property
    def mode(self) -> str:
        return "trailing" if self.trail > 0 else "normal"


TPS_DEFAULT = (2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0)
SL_RATIOS_DEFAULT = (0.5, 1.0, 1.5, 2.0, 3.0)
TRAIL_TPS_DEFAULT = (3.0, 4.0, 5.0, 6.0, 8.0)
TRAIL_SL_RATIOS_DEFAULT = (1.0, 2.0)
TRAIL_OF_TP_DEFAULT = 0.5
HOLDS_H_DEFAULT = (24, 48)


def exit_grid(tps=TPS_DEFAULT, sl_ratios=SL_RATIOS_DEFAULT, holds_h=HOLDS_H_DEFAULT,
              trail_tps=TRAIL_TPS_DEFAULT, trail_sl_ratios=TRAIL_SL_RATIOS_DEFAULT,
              trail_of_tp=TRAIL_OF_TP_DEFAULT, tf_min: int = 60) -> List[ExitCfg]:
    out = []
    for hh in holds_h:
        hb = max(1, int(round(hh * 60 / tf_min)))
        for tp in tps:
            for r in sl_ratios:
                out.append(ExitCfg(round(tp / 100, 6), round(tp * r / 100, 6), hb))
        if trail_of_tp > 0:
            for tp in trail_tps:
                for r in trail_sl_ratios:
                    out.append(ExitCfg(round(tp / 100, 6), round(tp * r / 100, 6), hb, round(tp * trail_of_tp / 100, 6)))
    return out


@dataclass
class Trade:
    kind: str
    exit_key: str
    sym: str
    side: int
    signal_i: int
    entry_i: int
    exit_i: int
    entry_t: float
    exit_t: float
    entry: float
    exit: float
    r: float          # net of cost, fraction
    reason: str


def exit_one(b: Bars, e: int, side: int, cfg: ExitCfg) -> Optional[Tuple[int, float, str]]:
    """Reference exit (CTS-A-O ``simulate``) for an entry at bar e's open.
    Returns (exit bar index, exit price, reason) or None when still open at the end."""
    o, h, l, c = b.o, b.h, b.l, b.c
    entry = o[e]
    stop = entry * (1 - cfg.sl) if side > 0 else entry * (1 + cfg.sl)
    target = entry * (1 + cfg.tp) if side > 0 else entry * (1 - cfg.tp)
    peak = entry
    trail_on = False
    for i in range(e, b.n):
        gap = i > e
        if side > 0:
            if l[i] <= stop:
                return i, (min(o[i], stop) if gap else stop), ("trail" if trail_on else "sl")
            if h[i] >= target:
                return i, (max(o[i], target) if gap else target), "tp"
        else:
            if h[i] >= stop:
                return i, (max(o[i], stop) if gap else stop), ("trail" if trail_on else "sl")
            if l[i] <= target:
                return i, (min(o[i], target) if gap else target), "tp"
        if i - e + 1 >= cfg.hold:
            return i, c[i], "time"
        if cfg.trail > 0:
            if side > 0:
                peak = max(peak, h[i])
                if (peak - entry) / entry >= cfg.trail:
                    trail_on = True
                    stop = max(stop, peak * (1 - cfg.trail))
            else:
                peak = min(peak, l[i])
                if (entry - peak) / entry >= cfg.trail:
                    trail_on = True
                    stop = min(stop, peak * (1 + cfg.trail))
    return None


def _first_true(mask: np.ndarray) -> np.ndarray:
    """Index of the first True along axis 0 (len when none)."""
    any_ = mask.any(axis=0)
    idx = mask.argmax(axis=0)
    return np.where(any_, idx, mask.shape[0])


def exits_for_entry(b: Bars, e: int, side: int, grid: Sequence[ExitCfg]) -> List[Optional[Tuple[int, float, str]]]:
    """``exit_one`` for every config of ``grid`` at once (non-trailing configs
    vectorized over the window; trailing configs use the reference loop)."""
    plain = [k for k, g in enumerate(grid) if g.trail <= 0]
    res: List[Optional[Tuple[int, float, str]]] = [None] * len(grid)
    if plain:
        hmax = max(grid[k].hold for k in plain)
        hi_i = min(b.n, e + hmax)
        if hi_i <= e:
            return [exit_one(b, e, side, g) if g.trail > 0 else None for g in grid]
        o, h, l, c = b.o[e:hi_i], b.h[e:hi_i], b.l[e:hi_i], b.c[e:hi_i]
        entry = b.o[e]
        sls = np.array([grid[k].sl for k in plain])
        tps = np.array([grid[k].tp for k in plain])
        holds = np.array([grid[k].hold for k in plain])
        if side > 0:
            stop, target = entry * (1 - sls), entry * (1 + tps)
            s_hit = _first_true(l[:, None] <= stop[None, :])
            t_hit = _first_true(h[:, None] >= target[None, :])
        else:
            stop, target = entry * (1 + sls), entry * (1 - tps)
            s_hit = _first_true(h[:, None] >= stop[None, :])
            t_hit = _first_true(l[:, None] <= target[None, :])
        span = hi_i - e
        for j, k in enumerate(plain):
            hd = int(holds[j])
            ks, kt = int(s_hit[j]), int(t_hit[j])
            if ks < hd and ks <= kt and ks < span:
                px = stop[j] if ks == 0 else (min(o[ks], stop[j]) if side > 0 else max(o[ks], stop[j]))
                res[k] = (e + ks, float(px), "sl")
            elif kt < hd and kt < span:
                px = target[j] if kt == 0 else (max(o[kt], target[j]) if side > 0 else min(o[kt], target[j]))
                res[k] = (e + kt, float(px), "tp")
            elif hd <= span:
                res[k] = (e + hd - 1, float(c[hd - 1]), "time")
            else:
                res[k] = None  # still open at the end of the data
    for k, g in enumerate(grid):
        if g.trail > 0:
            res[k] = exit_one(b, e, side, g)
    return res


def simulate_kind(b: Bars, kind: str, sig: np.ndarray, grid: Sequence[ExitCfg], cost: float,
                  sides: Sequence[int] = (1, -1)) -> Dict[str, List[Trade]]:
    """Trades per exit config. Long and short are independent slots (one
    position each); a new entry needs the previous exit bar (CTS-A-O)."""
    out: Dict[str, List[Trade]] = {g.key: [] for g in grid}
    for side in sides:
        idx = np.flatnonzero(sig == side)
        idx = idx[idx + 1 < b.n]
        if not len(idx):
            continue
        cache = {int(i): exits_for_entry(b, int(i) + 1, side, grid) for i in idx}
        for k, g in enumerate(grid):
            free_from = -1
            for i in idx:
                i = int(i)
                if i < free_from:
                    continue
                ex = cache[i][k]
                if ex is None:
                    break  # open at the end: nothing after it can enter
                xi, px, why = ex
                e = i + 1
                entry = float(b.o[e])
                r = side * (px - entry) / entry - cost
                out[g.key].append(Trade(kind, g.key, b.sym, side, i, e, xi, float(b.t[e]),
                                        float(b.t[xi] + b.tf_min * 60_000), entry, float(px), float(r), why))
                free_from = xi
    for v in out.values():
        v.sort(key=lambda tr: tr.entry_t)
    return out


# ------------------------------------------------------------------ metrics
def pf_classic(rs: Sequence[float]) -> float:
    gp = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r < 0)
    if gl <= 0:
        return 4.0 if gp > 0 else 0.0  # CTS-A-O PF_NO_LOSS
    return gp / gl


def pf_cost(rs: Sequence[float], cost: float) -> float:
    """CTS-G cost-coordinate PF: 1 + 0.1 * mean(net) / cost."""
    if not rs or cost <= 0:
        return 1.0
    return 1.0 + 0.1 * (sum(rs) / len(rs)) / cost


# ------------------------------------------------------------- presets
# Exit and volRegime per kind, selected on half A of the research year in
# reports/htf-validation-20261007 (28 BingX symbols, 0.18% cost). Validated on
# half B and the unseen prior year: robust family PF 1.36 / 1.36, rsi-mom
# family PF 1.17 / 1.17. Frozen here so the live lane trades what was tested.
HTF_PRESETS: Dict[str, Tuple[str, bool]] = {
    "rsi-mom-10-25": ("tp10|sl15|h48", False),
    "rsi-mom-14-15": ("tp6|sl6|h24|tr3", True),
    "rsi-mom-14-20": ("tp10|sl5|h24", True),
    "rsi-mom-14-25": ("tp10|sl5|h48", True),
    "rsi-mom-21-15": ("tp6|sl12|h24|tr3", True),
    "rsi-mom-21-20": ("tp6|sl6|h48|tr3", True),
    "rsi-mom-21-25": ("tp8|sl4|h24", True),
    "bb-walk@x4": ("tp6|sl12|h24|tr3", True),
    "break-vol@x4": ("tp10|sl5|h48", True),
    "break-vol-2@x4": ("tp10|sl5|h48", True),
    "break-atr-2@x4": ("tp6|sl6|h48|tr3", True),
    "act-burst-2.5@x4": ("tp10|sl5|h48", True),
    "act-chop@x4": ("tp3|sl6|h48|tr1.5", True),
    "cci-14-200@x4": ("tp5|sl5|h24|tr2.5", True),
    "cci-40-200@x4": ("tp10|sl5|h48", True),
    "z-50-2.5@x4": ("tp10|sl5|h48", True),
}


def parse_exit_key(key: str, tf_min: int = 60) -> ExitCfg:
    """'tp10|sl5|h48|tr3' (percent, hours) -> ExitCfg."""
    parts = dict((p[:2] if p[:2] in ("tp", "sl", "tr") else p[:1], p[2:] if p[:2] in ("tp", "sl", "tr") else p[1:])
                 for p in str(key).split("|") if p)
    hold_h = float(parts.get("h", 24))
    return ExitCfg(round(float(parts["tp"]) / 100, 6), round(float(parts["sl"]) / 100, 6),
                   max(1, int(round(hold_h * 60 / tf_min))), round(float(parts.get("tr", 0)) / 100, 6))


def preset(kind: str) -> Tuple[ExitCfg, bool]:
    key, vr = HTF_PRESETS[kind]
    return parse_exit_key(key), vr


HTF_SL_MAX = max(parse_exit_key(k).sl for k, _ in HTF_PRESETS.values())
HTF_TP_MAX = max(parse_exit_key(k).tp for k, _ in HTF_PRESETS.values())


# ---------------------------------------------------------------- the book
@dataclass
class HtfSettings:
    enabled: bool = False
    kinds: Tuple[str, ...] = HTF_KINDS
    min_pf: float = 1.05          # classic PF over the evidence window
    window: int = 50              # evidence window (live-first)
    min_n: int = 20               # fewer closes: the validated preset decides
    last_n: int = 0               # CTS-A-O last-N gate (0 = off; hurt on our data)
    side_hours: float = 24.0      # per family x side acceptance (CTS-A-O engineSideAccept)
    side_min_trades: int = 30
    side_min_pf: float = 1.05
    max_open: int = 12
    cost: float = 0.0018
    history_bars: int = 1000

    @classmethod
    def from_overlay(cls, ov: Dict) -> "HtfSettings":
        def f(key, default):
            try:
                return float(ov.get(key, default))
            except (TypeError, ValueError):
                return float(default)
        kinds = ov.get("htfKinds")
        if isinstance(kinds, dict):
            kinds = [k for k, on in kinds.items() if on]
        kinds = tuple(k for k in (kinds if isinstance(kinds, (list, tuple)) else HTF_KINDS) if k in KINDS)
        cost_pct = f("positionCostPct", 0.18)
        return cls(
            enabled=ov.get("htfEnabled", False) is True,
            kinds=kinds,
            min_pf=max(0.5, f("htfMinPf", 1.05)),
            window=max(5, int(f("htfWindow", 50))),
            min_n=max(1, int(f("htfMinN", 20))),
            last_n=max(0, int(f("htfLastN", 0))),
            side_hours=max(1.0, f("htfSideHours", 24)),
            side_min_trades=max(1, int(f("htfSideMinTrades", 30))),
            side_min_pf=max(0.5, f("htfSideMinPf", 1.05)),
            max_open=max(0, int(f("htfMaxOpen", 12))),
            cost=max(0.0, cost_pct) / 100.0,
            history_bars=max(400, min(1440, int(f("htfHistoryBars", 1000)))),
        )


def _row_t(r) -> float:
    return float(r.get("t") if hasattr(r, "get") else getattr(r, "t", 0.0)) or 0.0


class HtfBook:
    """1h bars, replay evidence and live evidence of the HTF lane (one desk).

    Evidence rows: {"t": close time s, "entry_t": s, "r": net fraction,
    "kind", "side", "symbol", "source": "replay" | "live"}.
    """

    def __init__(self, settings: Optional[HtfSettings] = None):
        self.s = settings or HtfSettings()
        self.bars: Dict[str, Bars] = {}
        self.hist: Dict[str, Dict[str, List[Dict]]] = {}   # symbol -> kind -> rows
        self.live: Dict[str, List[Dict]] = {}              # kind -> rows
        self.last_signal_t: Dict[str, float] = {}          # symbol -> last processed bar open (ms)
        self._seen_live: set = set()
        # Readers (stats, gates) run on other threads than the bar refresh:
        # bars/hist are replaced whole (copy-on-write) and every reader
        # iterates over a copy; live appends are serialized.
        self._lock = threading.Lock()
        self._sig_cache: Dict[str, Tuple[float, Dict[str, np.ndarray]]] = {}
        self._snap_cache: Optional[Tuple[Tuple, Dict]] = None
        self._ver = 0                                      # bumps on bars / replay changes

    # bars ------------------------------------------------------------
    def set_bars(self, sym: str, rows: Sequence, now_ms: Optional[float] = None) -> int:
        """Merge closed 1h rows ([t_ms, o, h, l, c, v] or [t_ms, [o,h,l,c,v]])."""
        now_ms = float(now_ms if now_ms is not None else 1e18)
        old = self.bars.get(sym)
        book: Dict[float, List[float]] = {}
        if old is not None:
            for i in range(old.n):
                book[float(old.t[i])] = [old.o[i], old.h[i], old.l[i], old.c[i], old.v[i]]
        for r in rows:
            if len(r) == 2 and isinstance(r[1], (list, tuple)):
                t, vals = float(r[0]), [float(x) for x in r[1][:5]]
            else:
                t, vals = float(r[0]), [float(x) for x in r[1:6]]
            if t + HOUR_MS <= now_ms and len(vals) == 5 and min(vals[:4]) > 0:
                book[t] = vals
        if not book:
            return 0
        ts = sorted(book)[-self.s.history_bars:]
        arr = np.array([[t, *book[t]] for t in ts], dtype=float)
        self.bars[sym] = Bars(arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4], arr[:, 5], 60, sym)
        self._ver += 1
        return len(ts)

    def signals(self, sym: str) -> Dict[str, np.ndarray]:
        """Entry signals of every enabled kind on this symbol's bars, computed
        once per new bar and shared by replay and new_signals."""
        b = self.bars.get(sym)
        if b is None or b.n == 0:
            return {}
        last_t = float(b.t[-1])
        hit = self._sig_cache.get(sym)
        if hit is not None and hit[0] == last_t and all(k in hit[1] for k in self.s.kinds):
            return hit[1]
        out = {k: entry_signals(b, [k], vol_regime=preset(k)[1])[k] for k in self.s.kinds}
        self._sig_cache[sym] = (last_t, out)
        return out

    # replay evidence -------------------------------------------------
    def replay(self, sym: str) -> int:
        """Rebuild this symbol's replay evidence from its 1h history."""
        b = self.bars.get(sym)
        if b is None or b.n < 60:
            self.hist[sym] = {}
            return 0
        out: Dict[str, List[Dict]] = {}
        n = 0
        sigs = self.signals(sym)
        for kind in self.s.kinds:
            cfg, _ = preset(kind)
            sig = sigs[kind]
            trades = simulate_kind(b, kind, sig, [cfg], self.s.cost)[cfg.key]
            rows = [{"t": t.exit_t / 1000.0, "entry_t": t.entry_t / 1000.0, "r": t.r, "kind": kind,
                     "side": "LONG" if t.side > 0 else "SHORT", "symbol": sym, "source": "replay"} for t in trades]
            out[kind] = rows
            n += len(rows)
        self.hist[sym] = out
        self._ver += 1
        return n

    DEDUP_TOL_S = 2 * 3600.0   # a live lot and its replay twin enter within one or two bars

    def hist_rows(self, kind: str) -> List[Dict]:
        """Replay rows of one kind. A replay trade that the desk actually took
        live (same symbol, side and entry within two bars) is dropped: the
        live result counts for it. Signals the desk did not take (gated, cap
        reached) keep their replay row, so the evidence stays current."""
        taken: Dict[Tuple[str, str], List[float]] = {}
        for r in list(self.live.get(kind, ())):
            if r.get("entry_t") is not None:
                taken.setdefault((r.get("symbol", ""), r.get("side", "")), []).append(float(r["entry_t"]))

        def dup(sym: str, r: Dict) -> bool:
            ets = taken.get((sym, r.get("side", "")))
            if not ets:
                return False
            et = float(r.get("entry_t", r["t"]))
            return any(abs(et - x) <= self.DEDUP_TOL_S for x in ets)
        rows = [r for sym, by in list(self.hist.items()) for r in by.get(kind, ()) if not dup(sym, r)]
        rows.sort(key=_row_t)
        return rows

    # live evidence ---------------------------------------------------
    def on_live_close(self, rec) -> bool:
        """Credit one confirmed, complete exchange round trip of an HTF position."""
        row = rec if isinstance(rec, dict) else vars(rec)
        if row.get("exchange_confirmed") is not True or row.get("partial"):
            return False
        kind = str(row.get("ind_kind") or "")
        if kind not in KINDS:
            return False
        ident = (row.get("close_fill_id") or row.get("client_id"), kind)
        with self._lock:
            if ident in self._seen_live:
                return False
            self._seen_live.add(ident)
        entry, qty = float(row.get("entry") or 0), float(row.get("qty") or 0)
        notional = entry * qty
        if notional > 0:
            r = float(row.get("pnl") or 0.0) / notional  # net of fees as booked
        else:
            r = float(row.get("pnl_pct") or 0.0) - self.s.cost
        t = float(row.get("t") or 0.0)
        hold = float(row.get("hold_s") or row.get("holdS") or 0.0)
        new = {"t": t, "entry_t": t - max(0.0, hold), "r": r, "kind": kind, "side": str(row.get("side") or "").upper(),
               "symbol": str(row.get("symbol") or ""), "source": "live"}
        with self._lock:
            rows = list(self.live.get(kind, ())) + [new]
            rows.sort(key=_row_t)
            self.live[kind] = rows[-500:]
        return True

    def seed_live(self, rows: Iterable) -> int:
        """Startup: credit retained confirmed HTF round trips (idempotent)."""
        return sum(1 for r in rows if self.on_live_close(r))

    # gates -----------------------------------------------------------
    def evidence(self, kind: str) -> Tuple[List[Dict], str]:
        from position_cost import live_first_window
        return live_first_window(self.live.get(kind, []), self.hist_rows(kind), self.s.window)

    def side_accept(self, family: str, side: str, now_s: float) -> Tuple[bool, Dict]:
        """CTS-A-O acceptOnWindow on the family x side closes before now."""
        rows = [r for k in self.s.kinds if KINDS[k].family == family
                for r in (self.hist_rows(k) + list(self.live.get(k, ()))) if r["side"] == side and r["t"] <= now_s]

        def stats(hours):
            rs = [r["r"] for r in rows if now_s - r["t"] <= hours * 3600]
            return len(rs), pf_classic(rs)
        n, pf = stats(self.s.side_hours)
        if n >= self.s.side_min_trades:
            return pf >= self.s.side_min_pf, {"n": n, "pf": round(pf, 3), "hours": self.s.side_hours}
        n2, pf2 = stats(self.s.side_hours * 2)
        ok = n2 < self.s.side_min_trades or pf2 >= self.s.side_min_pf
        return ok, {"n": n2, "pf": round(pf2, 3), "hours": self.s.side_hours * 2}

    def gate(self, kind: str, side: str, now_s: float) -> Tuple[bool, str, Dict]:
        if not self.s.enabled:
            return False, "htf off", {}
        if kind not in self.s.kinds:
            return False, "kind off", {}
        tape, src = self.evidence(kind)
        rs = [r["r"] for r in tape]
        info = {"n": len(rs), "pf": round(pf_classic(rs), 3) if rs else None, "source": src,
                "liveN": len(self.live.get(kind, []))}
        if len(rs) >= self.s.min_n and pf_classic(rs) < self.s.min_pf:
            return False, f"PF {pf_classic(rs):.2f} < {self.s.min_pf:.2f} ({src} {len(rs)})", info
        if self.s.last_n > 0:
            tail = rs[-self.s.last_n:]
            if len(tail) >= min(5, self.s.last_n) and pf_classic(tail) < 1.0:
                return False, f"last {self.s.last_n} PF {pf_classic(tail):.2f} < 1", info
        ok, acc = self.side_accept(KINDS[kind].family, side, now_s)
        info["sideAccept"] = acc
        if not ok:
            return False, f"{KINDS[kind].family} {side} {acc['hours']:g}h PF {acc['pf']} < {self.s.side_min_pf}", info
        return True, "ok" if len(rs) >= self.s.min_n else "validated preset (evidence building)", info

    # signals ---------------------------------------------------------
    def new_signals(self, sym: str) -> List[Dict]:
        """Entries decided at the close of the newest bar not processed yet."""
        b = self.bars.get(sym)
        if b is None or b.n < 60:
            return []
        last_t = float(b.t[-1])
        if self.last_signal_t.get(sym, -1.0) >= last_t:
            return []
        self.last_signal_t[sym] = last_t
        out = []
        sigs = self.signals(sym)
        for kind in self.s.kinds:
            cfg, _ = preset(kind)
            s = int(sigs[kind][-1])
            if s:
                out.append({"symbol": sym, "kind": kind, "side": "LONG" if s > 0 else "SHORT",
                            "direction": s, "exit": cfg, "barT": last_t, "close": float(b.c[-1])})
        return out

    def snapshot(self, now_s: Optional[float] = None) -> Dict:
        import time as _t
        now_s = now_s if now_s is not None else _t.time()
        # Gates only change with a new bar, a new live close or new replay.
        token = (id(self.s), int(now_s // 3600), tuple(sorted((k, len(v)) for k, v in list(self.live.items()))),
                 self._ver)
        cached = self._snap_cache
        if cached is not None and cached[0] == token:
            return cached[1]
        kinds = {}
        for k in self.s.kinds:
            tape, src = self.evidence(k)
            rs = [r["r"] for r in tape]
            live_rs = [r["r"] for r in list(self.live.get(k, ()))]
            ok_l, why_l, _ = self.gate(k, "LONG", now_s)
            ok_s, why_s, _ = self.gate(k, "SHORT", now_s)
            kinds[k] = {"family": KINDS[k].family, "exit": HTF_PRESETS[k][0], "volRegime": HTF_PRESETS[k][1],
                        "n": len(rs), "pf": round(pf_classic(rs), 3) if rs else None, "source": src,
                        "liveN": len(live_rs), "livePf": round(pf_classic(live_rs), 3) if live_rs else None,
                        "long": {"ok": ok_l, "why": why_l}, "short": {"ok": ok_s, "why": why_s}}
        snap = {"enabled": self.s.enabled, "symbols": len(self.bars),
                "bars": {s: int(b.n) for s, b in list(self.bars.items())}, "kinds": kinds,
                "settings": {"minPf": self.s.min_pf, "window": self.s.window, "minN": self.s.min_n, "lastN": self.s.last_n,
                             "sideHours": self.s.side_hours, "sideMinTrades": self.s.side_min_trades,
                             "sideMinPf": self.s.side_min_pf, "maxOpen": self.s.max_open}}
        self._snap_cache = (token, snap)
        return snap
