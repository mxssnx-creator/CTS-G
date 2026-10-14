"""CTS-A-O hourly indications, 4h agreement, bots and filters.

Ported from CTS-A-O src/core/indications/{registry,cache,filters}.ts and
src/core/bots/bots.ts. An indication is a per-bar state (+1 bullish,
-1 bearish, 0 neutral) that reads bars 0..i only. "<id>@x4" keeps the state
only where the same indication agrees on the last completed 4h bar. The
"follow" bot enters on a state onset in its direction, "revert" against it;
entries happen at the next bar's open (ao_sim).

The registry holds exactly the 30 pairs the hourly lane trades or shadows:
the 16 that held PF >= 1.1 out of sample in A-O and 14 single-period
candidates that stay shadow-only until they pass the same judge.
"""
from __future__ import annotations

import math
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

import ao_math as M

H_MS = 3_600_000
D_MS = 86_400_000
VOL_RANK_BARS = 336  # two weeks of 1h bars (filters.ts VOL_RANK_BARS)

PAIRS_VALIDATED: Tuple[str, ...] = (
    "follow|rsi-mom-10-25", "follow|rsi-mom-14-15", "follow|rsi-mom-14-20", "follow|rsi-mom-14-25",
    "follow|rsi-mom-21-15", "follow|rsi-mom-21-20", "follow|rsi-mom-21-25",
    "follow|bb-walk@x4", "follow|break-vol@x4", "follow|break-vol-2@x4", "follow|break-atr-2@x4",
    "follow|act-burst-2.5@x4",
    "revert|cci-40-200@x4", "revert|cci-14-200@x4", "revert|z-50-2.5@x4", "revert|act-chop@x4",
)
PAIRS_CANDIDATES: Tuple[str, ...] = (
    "follow|kelt-20-2.5@x4", "follow|bb-walk-50@x4", "revert|cci-20-200@x4", "follow|squeeze-20@x4",
    "follow|macd-cross@x4", "follow|break-vol-1.3@x4", "follow|break-atr-1.5@x4",
    "revert|bb-bounce-20-2.5@x4", "revert|z-20-2.5@x4", "revert|bb-bounce-20-3@x4",
    "revert|rsi-14-25-75@x4", "revert|mfi-14-20@x4",
    "follow|break-atr-2+adx25", "revert|bb-bounce-20-3+volHi",
)
PAIRS_ALL: Tuple[str, ...] = PAIRS_VALIDATED + PAIRS_CANDIDATES

# Indications whose own rule fixes a low-ADX regime skip the trend-strength
# tactic (filters.ts LOW_ADX_INDS); with it they would never trade.
LOW_ADX_INDS = frozenset({"act-chop"})


def js_num(x: float) -> str:
    """JavaScript Number→string for the short decimals used in ids (2.0 → '2')."""
    if float(x).is_integer():
        return str(int(x))
    return repr(float(x))


class Bars:
    """Column bars; t is the open time in ms, tf_min the bar length in minutes."""

    __slots__ = ("sym", "tf_min", "t", "o", "h", "l", "c", "v")

    def __init__(self, sym: str, tf_min: int, t, o, h, l, c, v) -> None:
        self.sym = sym
        self.tf_min = int(tf_min)
        self.t = np.asarray(t, dtype=np.float64)
        self.o = np.asarray(o, dtype=np.float64)
        self.h = np.asarray(h, dtype=np.float64)
        self.l = np.asarray(l, dtype=np.float64)
        self.c = np.asarray(c, dtype=np.float64)
        self.v = np.asarray(v, dtype=np.float64)

    @property
    def n(self) -> int:
        return len(self.c)

    def tail(self, count: int) -> "Bars":
        s = max(0, self.n - int(count))
        return Bars(self.sym, self.tf_min, self.t[s:], self.o[s:], self.h[s:], self.l[s:], self.c[s:], self.v[s:])

    def head(self, cut_ms: float) -> "Bars":
        """Bars that had closed by cut_ms (the history as it was then)."""
        n = int(np.searchsorted(self.t + self.tf_min * 60_000, cut_ms, side="right"))
        return Bars(self.sym, self.tf_min, self.t[:n], self.o[:n], self.h[:n], self.l[:n], self.c[:n], self.v[:n])


def bars_from_candles(sym: str, tf_min: int, candles: Sequence[dict]) -> Bars:
    cols = {k: [float(c[k]) for c in candles] for k in ("t", "o", "h", "l", "c", "v")}
    return Bars(sym, tf_min, cols["t"], cols["o"], cols["h"], cols["l"], cols["c"], cols["v"])


def bars_from_arrays(sym: str, a: Dict[str, np.ndarray], tf_min: int = 60) -> Bars:
    return Bars(sym, tf_min, a["t"], a["o"], a["h"], a["l"], a["c"], a["v"])


def bars_from_npz(path: str, sym: str, tf_min: int = 60) -> Bars:
    with np.load(path) as z:
        return bars_from_arrays(sym, {k: z[k] for k in z.files}, tf_min)


def htf_bars(b: Bars, factor: int) -> Tuple[Bars, np.ndarray]:
    """Completed higher bars (a bucket is published with the bar that closes it).

    map[i] = index of the last higher bar closed when bar i closed (-1 = none).
    """
    tf_ms = b.tf_min * 60_000
    span = tf_ms * factor
    T: List[float] = []
    O: List[float] = []
    Hh: List[float] = []
    L: List[float] = []
    C: List[float] = []
    V: List[float] = []
    mp = np.full(b.n, -1, dtype=np.int32)
    cur = None
    bo = bh = bl = bc = bv = bt = 0.0
    t, o, h, l, c, v = b.t.tolist(), b.o.tolist(), b.h.tolist(), b.l.tolist(), b.c.tolist(), b.v.tolist()
    for i in range(b.n):
        bucket = math.floor(t[i] / span)
        if bucket != cur:
            cur = bucket
            bt = bucket * span
            bo, bh, bl, bc, bv = o[i], h[i], l[i], c[i], v[i]
        else:
            if h[i] > bh:
                bh = h[i]
            if l[i] < bl:
                bl = l[i]
            bc = c[i]
            bv += v[i]
        if t[i] + tf_ms >= bt + span:
            T.append(bt)
            O.append(bo)
            Hh.append(bh)
            L.append(bl)
            C.append(bc)
            V.append(bv)
        mp[i] = len(T) - 1
    return Bars(b.sym, b.tf_min * factor, T, O, Hh, L, C, V), mp


class SeriesCache:
    """Per-symbol memo so the 30 pairs share one computation (A-O cache.ts)."""

    def __init__(self, b: Bars) -> None:
        self.b = b
        self._m: Dict[str, object] = {}

    def memo(self, key: str, fn: Callable[[], object]):
        hit = self._m.get(key)
        if hit is None:
            hit = fn()
            self._m[key] = hit
        return hit

    def ema(self, p: int):
        return self.memo(f"ema{p}", lambda: M.ema(self.b.c, p))

    def rsi(self, p: int):
        return self.memo(f"rsi{p}", lambda: M.rsi(self.b.c, p))

    def atr(self, p: int):
        return self.memo(f"atr{p}", lambda: M.atr(self.b.h, self.b.l, self.b.c, p))

    def macd(self, f: int = 12, s: int = 26, g: int = 9):
        return self.memo(f"macd{f}.{s}.{g}", lambda: M.macd(self.b.c, f, s, g))

    def bb(self, p: int = 20, k: float = 2):
        return self.memo(f"bb{p}.{k}", lambda: M.bollinger(self.b.c, p, k))

    def dmi(self, p: int = 14):
        return self.memo(f"dmi{p}", lambda: M.dmi(self.b.h, self.b.l, self.b.c, p))

    def don(self, p: int):
        return self.memo(f"don{p}", lambda: M.donchian_prior(self.b.h, self.b.l, p))

    def vol_sma(self, p: int):
        return self.memo(f"vsma{p}", lambda: M.sma(self.b.v, p))

    def range_sma(self, p: int):
        return self.memo(f"rsma{p}", lambda: M.range_sma(self.b.h, self.b.l, p))

    def cci(self, p: int):
        return self.memo(f"cci{p}", lambda: M.cci(self.b.h, self.b.l, self.b.c, p))

    def mfi(self, p: int):
        return self.memo(f"mfi{p}", lambda: M.mfi(self.b.h, self.b.l, self.b.c, self.b.v, p))

    def kelt(self, p: int, m: float):
        return self.memo(f"kelt{p}.{m}", lambda: M.keltner(self.b.h, self.b.l, self.b.c, p, m))

    def z(self, p: int):
        return self.memo(f"z{p}", lambda: M.zscore(self.b.c, p))

    def htf(self, factor: int) -> Tuple["SeriesCache", np.ndarray]:
        def build():
            hb, mp = htf_bars(self.b, factor)
            return SeriesCache(hb), mp

        return self.memo(f"htf{factor}", build)


# ── state helpers (registry.ts state / hold) ────────────────
def _state(n: int, f: Callable[[int], float]) -> np.ndarray:
    out = np.zeros(n, dtype=np.int8)
    for i in range(n):
        v = f(i)
        if v is None or math.isnan(v) or math.isinf(v):
            continue
        out[i] = 1 if v > 0 else (-1 if v < 0 else 0)
    return out


def _hold(ev: np.ndarray, keep: int) -> np.ndarray:
    """An event at bar i holds its sign for `keep` bars; a later event restarts."""
    out = np.zeros(len(ev), dtype=np.int8)
    cur = 0
    left = 0
    for i, e in enumerate(ev.tolist()):
        if e != 0:
            cur = e
            left = keep
        if left > 0:
            out[i] = cur
            left -= 1
    return out


def _ok(*xs: float) -> bool:
    return all(not (math.isnan(x) or math.isinf(x)) for x in xs)


def _rsi_mom(p: int, lvl: int):
    def fn(k: SeriesCache):
        r = k.rsi(p).tolist()
        return _state(k.b.n, lambda i: 1 if r[i] > 100 - lvl else (-1 if r[i] < lvl else 0))
    return fn


def _rsi_revert(p: int, lo: float, hi: float):
    def fn(k: SeriesCache):
        r = k.rsi(p).tolist()
        return _state(k.b.n, lambda i: 1 if r[i] < lo else (-1 if r[i] > hi else 0))
    return fn


def _bb_walk(p: int):
    def fn(k: SeriesCache):
        bb = k.bb(p, 2)
        up, lo, c = bb["up"].tolist(), bb["lo"].tolist(), k.b.c.tolist()
        return _state(k.b.n, lambda i: 1 if (i > 0 and c[i] > up[i] and c[i - 1] > up[i - 1])
                      else (-1 if (i > 0 and c[i] < lo[i] and c[i - 1] < lo[i - 1]) else 0))
    return fn


def _bb_bounce(p: int, kk: float):
    def fn(k: SeriesCache):
        bb = k.bb(p, kk)
        up, lo, c = bb["up"].tolist(), bb["lo"].tolist(), k.b.c.tolist()
        return _state(k.b.n, lambda i: 1 if c[i] < lo[i] else (-1 if c[i] > up[i] else 0))
    return fn


def _break_vol(mul: float):
    def fn(k: SeriesCache):
        hi, lo = (a.tolist() for a in k.don(20))
        c, v, vs = k.b.c.tolist(), k.b.v.tolist(), k.vol_sma(20).tolist()
        return _hold(_state(k.b.n, lambda i: (1 if c[i] > hi[i] else (-1 if c[i] < lo[i] else 0)) if v[i] > mul * vs[i] else 0), 6)
    return fn


def _break_atr(mul: float):
    def fn(k: SeriesCache):
        a, c = k.atr(14).tolist(), k.b.c.tolist()
        return _hold(_state(k.b.n, lambda i: c[i] - c[i - 1] if (i > 0 and _ok(a[i - 1]) and abs(c[i] - c[i - 1]) > mul * a[i - 1]) else 0), 4)
    return fn


def _act_burst(mul: float):
    def fn(k: SeriesCache):
        rs = k.range_sma(20).tolist()
        o, c, h, l = k.b.o.tolist(), k.b.c.tolist(), k.b.h.tolist(), k.b.l.tolist()
        return _hold(_state(k.b.n, lambda i: c[i] - o[i] if (i > 0 and h[i] - l[i] > mul * rs[i - 1]) else 0), 4)
    return fn


def _act_chop(k: SeriesCache):
    adx, r = k.dmi(14)["adx"].tolist(), k.rsi(7).tolist()
    return _state(k.b.n, lambda i: (1 if r[i] < 30 else (-1 if r[i] > 70 else 0)) if (_ok(adx[i], r[i]) and adx[i] < 18) else 0)


def _cci(p: int, lvl: float):
    def fn(k: SeriesCache):
        x = k.cci(p).tolist()
        return _state(k.b.n, lambda i: 1 if x[i] < -lvl else (-1 if x[i] > lvl else 0))
    return fn


def _z(p: int, t: float):
    def fn(k: SeriesCache):
        x = k.z(p).tolist()
        return _state(k.b.n, lambda i: 1 if x[i] < -t else (-1 if x[i] > t else 0))
    return fn


def _mfi(lvl: float):
    def fn(k: SeriesCache):
        x = k.mfi(14).tolist()
        return _state(k.b.n, lambda i: 1 if x[i] < lvl else (-1 if x[i] > 100 - lvl else 0))
    return fn


def _kelt(m: float):
    def fn(k: SeriesCache):
        kc = k.kelt(20, m)
        up, lo, c = kc["up"].tolist(), kc["lo"].tolist(), k.b.c.tolist()
        return _hold(_state(k.b.n, lambda i: 1 if c[i] > up[i] else (-1 if c[i] < lo[i] else 0)), 4)
    return fn


def _squeeze(p: int):
    def fn(k: SeriesCache):
        bb, kc, c = k.bb(p, 2), k.kelt(p, 1.5), k.b.c.tolist()
        bu, bl, ku, kl, km = (bb["up"].tolist(), bb["lo"].tolist(), kc["up"].tolist(), kc["lo"].tolist(), kc["mid"].tolist())

        def f(i):
            if i < 1 or not _ok(bu[i], ku[i], bu[i - 1], ku[i - 1]):
                return 0
            was = bu[i - 1] < ku[i - 1] and bl[i - 1] > kl[i - 1]
            now = bu[i] < ku[i] and bl[i] > kl[i]
            return c[i] - km[i] if (was and not now) else 0
        return _hold(_state(k.b.n, f), 6)
    return fn


def _macd_cross(k: SeriesCache):
    m = k.macd()
    line, sig = m["line"].tolist(), m["signal"].tolist()

    def f(i):
        if not (i > 0 and _ok(sig[i - 1])):
            return 0
        if line[i - 1] < sig[i - 1] and line[i] > sig[i]:
            return 1
        if line[i - 1] > sig[i - 1] and line[i] < sig[i]:
            return -1
        return 0
    return _hold(_state(k.b.n, f), 6)


SPECS: Dict[str, Callable[[SeriesCache], np.ndarray]] = {
    **{f"rsi-mom-{p}-{lvl}": _rsi_mom(p, lvl) for p, lvl in ((10, 25), (14, 15), (14, 20), (14, 25), (21, 15), (21, 20), (21, 25))},
    "rsi-14-25-75": _rsi_revert(14, 25, 75),
    "bb-walk": _bb_walk(20),
    "bb-walk-50": _bb_walk(50),
    "bb-bounce-20-2.5": _bb_bounce(20, 2.5),
    "bb-bounce-20-3": _bb_bounce(20, 3),
    "break-vol": _break_vol(1.6),
    **{f"break-vol-{js_num(m)}": _break_vol(m) for m in (1.3, 2.0)},
    **{f"break-atr-{js_num(m)}": _break_atr(m) for m in (1.5, 2.0)},
    "act-burst-2.5": _act_burst(2.5),
    "act-chop": _act_chop,
    **{f"cci-{p}-{lvl}": _cci(p, lvl) for p in (14, 20, 40) for lvl in (200,)},
    **{f"z-{p}-{js_num(t)}": _z(p, t) for p in (20, 50) for t in (2.5,)},
    "mfi-14-20": _mfi(20),
    "kelt-20-2.5": _kelt(2.5),
    "squeeze-20": _squeeze(20),
    "macd-cross": _macd_cross,
}


def indication_state(ind: str, k: SeriesCache) -> np.ndarray:
    if ind.endswith("@x4"):
        return mtf_state(ind[:-3], k, (4,))
    fn = SPECS.get(ind)
    if fn is None:
        raise KeyError(f"unknown indication {ind}")
    return k.memo(f"ind:{ind}", lambda: fn(k))


def mtf_state(ind: str, k: SeriesCache, factors: Sequence[int]) -> np.ndarray:
    """The state kept only where the same indication agrees on each higher timeframe."""
    if not factors:
        return indication_state(ind, k)

    def build():
        out = indication_state(ind, k).copy()
        for f in factors:
            hk, mp = k.htf(f)
            hs = indication_state(ind, hk)
            for i in np.flatnonzero(out):
                j = mp[i]
                if j < 0 or hs[j] != out[i]:
                    out[i] = 0
        return out

    return k.memo(f"mtf:{'x'.join(str(f) for f in factors)}:{ind}", build)


# ── filters and tactics (filters.ts) ───────────────────────
def _keep(sig: np.ndarray, ok: Callable[[int, int], bool]) -> np.ndarray:
    out = np.zeros(len(sig), dtype=np.int8)
    for i in np.flatnonzero(sig):
        if ok(int(i), int(sig[i])):
            out[i] = sig[i]
    return out


def _natr_rank(k: SeriesCache) -> np.ndarray:
    def build():
        a, c = k.atr(14), k.b.c
        x = (a / c).tolist()
        p = VOL_RANK_BARS
        out = [math.nan] * len(x)
        for i in range(p, len(x)):
            v = x[i]
            if math.isnan(v) or math.isinf(v):
                continue
            below = 0
            cnt = 0
            for j in range(i - p, i):
                w = x[j]
                if math.isnan(w) or math.isinf(w):
                    continue
                cnt += 1
                if w < v:
                    below += 1
            if cnt > p / 2:
                out[i] = below / cnt
        return np.array(out, dtype=np.float64)

    return k.memo(f"rank:natr:{VOL_RANK_BARS}", build)


def apply_filter(name: str, sig: np.ndarray, k: SeriesCache) -> np.ndarray:
    if name == "none":
        return sig
    if name in ("adx20", "adx25"):
        lvl = 20 if name == "adx20" else 25
        adx = k.dmi(14)["adx"]
        return _keep(sig, lambda i, d: adx[i] >= lvl)
    if name in ("volHi", "volLo"):
        rank = _natr_rank(k)
        if name == "volHi":
            return _keep(sig, lambda i, d: rank[i] >= 0.5)
        return _keep(sig, lambda i, d: rank[i] < 0.5)
    if name == "euUs":
        t = k.b.t
        return _keep(sig, lambda i, d: 7 <= math.floor((t[i] % D_MS) / H_MS) < 21)
    raise KeyError(f"unknown filter {name}")


def tactic_key(tactics: Optional[Dict[str, bool]]) -> List[str]:
    if not tactics:
        return []
    order = (("session", "euUs"), ("volRegime", "volHi"), ("trendStrength", "adx20"))
    return [f for key, f in order if tactics.get(key)]


def tactic_warmup_bars(tactics: Optional[Dict[str, bool]]) -> int:
    t = tactics or {}
    return VOL_RANK_BARS + 20 if t.get("volRegime") else (40 if t.get("trendStrength") else 0)


def parse_pair(pair: str) -> Tuple[str, str, Tuple[str, ...]]:
    """'follow|break-atr-2+adx25' → ('follow', 'break-atr-2', ('adx25',))."""
    bot, rest = pair.split("|", 1)
    parts = rest.split("+")
    if bot not in ("follow", "revert"):
        raise ValueError(f"unsupported bot {bot}")
    return bot, parts[0], tuple(parts[1:])


def combo_signal(bot: str, ind: str, k: SeriesCache) -> np.ndarray:
    """Onsets of the state: follow enters with it, revert against it."""
    def build():
        st = indication_state(ind, k)
        d = 1 if bot == "follow" else -1
        out = np.zeros(len(st), dtype=np.int8)
        on = np.flatnonzero((st[1:] != 0) & (st[1:] != st[:-1])) + 1
        out[on] = st[on] * d
        return out

    return k.memo(f"combo:{bot}:{ind}", build)


def pair_signal(pair: str, k: SeriesCache, tactics: Optional[Dict[str, bool]] = None) -> np.ndarray:
    """Entry signal of a pair: combo, the pair's own filters, then engine tactics."""
    bot, ind, filters = parse_pair(pair)
    keys = tactic_key(tactics)
    base = ind[:-3] if ind.endswith("@x4") else ind
    if base in LOW_ADX_INDS:
        keys = [x for x in keys if x != "adx20"]

    def build():
        sig = combo_signal(bot, ind, k)
        for f in tuple(filters) + tuple(keys):
            sig = apply_filter(f, sig, k)
        return sig

    return k.memo(f"pair:{pair}:{'+'.join(keys)}", build)


def pair_signals(pairs: Iterable[str], k: SeriesCache, tactics: Optional[Dict[str, bool]] = None) -> Dict[str, np.ndarray]:
    return {p: pair_signal(p, k, tactics) for p in pairs}
