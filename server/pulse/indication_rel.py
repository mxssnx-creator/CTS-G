"""Relations: any indicator name combined with conditions on the market around its signal, as one composite name.

    <indicator>@<relation>[@<relation>...]        e.g.  rsirev_n8_t1@T15      a reversion signal taken only with the 15m trend
                                                        bbrev_n14_t5@N15@C    a band fade only without a 15m trend, in a contraction
                                                        macdh_n8_t1@I@t5      the inverted signal, only against the 5m trend

Every relation reads the same closed 1m rows the indicator reads (newest last), causally; nothing depends on a clock. The relations
are applied left to right on the signal's direction d (+1 long, -1 short); an upper-case letter asks the condition WITH the
direction, the lower-case letter AGAINST it:

  T<tf>[n<k>]  the Trend family (indication_extra.trend_signal, k bars of the tf-minute series, default 20) points the same way;
  t<tf>[n<k>]  it points the other way (a counter-trend signal);   N<tf>[n<k>]  no trend on that series (a range regime)
  B<tf>[n<k>]  the close is beyond the prior k-bar extreme of the tf-minute series in the direction (a break state, default 20);
  b<tf>[n<k>]  the close is inside the prior k-bar range (no break)
  M<n>         the earlier market move (the n 1m bars before the last part of 5 bars) went the same way by at least one
               random-walk unit (ATR x sqrt(n)); m<n> it went the other way (default n 30)
  A[<k>]       the last part (k 1m bars, default 5) is active: mean true range at least 1.5 x the 30 bars before; a[<k>] quiet (0.7 x)
  V[<k>]       the last part's volume at least 1.5 x the 30 bars before; v[<k>] dried up (0.7 x)
  D            a pullback against the direction from the 30-bar extreme of 0.5 to 2.5 ATR (buying a dip in an up move);
  d            at the extreme (under 0.3 ATR);   X  a deep drawdown from it (over 2.5 ATR)
  C            volatility contraction: ATR of the last 14 bars at most 0.75 x the ATR of the 60 before; c  an expansion (1.25 x)
  I            tactic: invert the direction (fade the signal); the relations after it judge the inverted direction
  K            tactic: confirmation, the indicator gave the same direction on the bar before too;   F  fresh, it did not
  G            tactic: the newest 1m close moved in the direction (a go candle)

`resolve(name)` compiles a composite once (cached); an unknown base or relation gives None, so a misspelt name is never a silent
always-off vote but an unknown name. `window_of(name)` is the most 1m rows the composite reads.
"""
from __future__ import annotations

import math
import re
import threading
from typing import Callable, Dict, List, Optional, Sequence, Tuple

Row = Sequence[float]
Signal = Optional[Tuple[int, float]]

_TOKEN = re.compile(r"^([TtNBb])(\d+)(?:n(\d+))?$|^([Mm])(\d*)$|^([AaVv])(\d*)$|^([DdXCcIKFG])$")
_cache: Dict[str, Optional[Callable[[Sequence[Row]], Signal]]] = {}
_cache_lock = threading.Lock()
TREND_N = 20
BREAK_N = 20
MOVE_N = 30
LAST_PART = 5


def _features(rows: Sequence[Row]) -> Dict:
    """The relation features of one history (dict feature -> value), in indication_lib's bounded per-thread memo keyed by the rows'
    count, first and newest row objects and the newest values: every composite of one evaluation computes each feature once, and a
    list changed in place is never served its earlier features."""
    from indication_lib import _memo
    if not len(rows):
        return {}
    last = rows[-1]
    k = ("rel", len(rows), id(rows[0]), id(last), tuple(last[:5]))
    m = _memo()
    got = m.get(k)
    if got is None:
        got = (rows[0], last, {})
        m[k] = got
    return got[2]


def _atr1(rows: Sequence[Row], n: int, end: int) -> float:
    """Mean true range of the n 1m bars ending before index `end` (end exclusive, counted from the start of rows)."""
    lo = max(1, end - n)
    if end - lo < 1:
        return 0.0
    s = 0.0
    for i in range(lo, end):
        h, l_, pc = float(rows[i][1]), float(rows[i][2]), float(rows[i - 1][3])
        s += max(h - l_, abs(h - pc), abs(l_ - pc))
    return s / (end - lo)


def _feat(rows: Sequence[Row], name: str, fn: Callable[[], float]) -> float:
    f = _features(rows)
    v = f.get(name)
    if v is None:
        v = fn()
        f[name] = v
    return v


def _trend_dir(rows, tf, n) -> float:
    from indication_extra import trend_signal
    s = trend_signal(rows, n, tf)
    return float(s[0]) if s else 0.0


def _break_state(rows, tf, n) -> float:
    from indication_lib import _agg, rows_for
    b = _agg(rows[-rows_for(tf):] if len(rows) > rows_for(tf) else rows, tf)
    if len(b) < n + 2:
        return 0.0
    prior = b[-n - 1:-1]
    c = float(b[-1][3])
    if c > max(float(x[1]) for x in prior):
        return 1.0
    if c < min(float(x[2]) for x in prior):
        return -1.0
    return 0.0


def _prior_move(rows, n) -> float:
    """The move of the n bars before the last part, in random-walk units (ATR of those bars x sqrt(n)); nan when too short."""
    end = len(rows) - LAST_PART
    if end - n < 1:
        return float("nan")
    atr = _atr1(rows, n, end)
    if atr <= 0:
        return 0.0
    return (float(rows[end - 1][3]) - float(rows[end - n - 1][3])) / (atr * math.sqrt(n))


def _ratio(rows, k, what) -> float:
    """The last k bars against the 30 before: mean true range (what "tr") or mean volume (what "vol"); nan when too short."""
    n = len(rows)
    if n < k + 31:
        return float("nan")
    if what == "tr":
        last, prev = _atr1(rows, k, n), _atr1(rows, 30, n - k)
    else:
        last = sum(float(r[4]) for r in rows[n - k:]) / k
        prev = sum(float(r[4]) for r in rows[n - k - 30:n - k]) / 30
    return last / prev if prev > 0 else float("nan")


def _pullback(rows, side) -> float:
    """Distance of the close from the 30-bar extreme in the direction (the high for a long, the low for a short), in ATR."""
    if len(rows) < 32:
        return float("nan")
    atr = _atr1(rows, 14, len(rows))
    if atr <= 0:
        return float("nan")
    c = float(rows[-1][3])
    w = rows[-30:]
    return (max(float(r[1]) for r in w) - c) / atr if side > 0 else (c - min(float(r[2]) for r in w)) / atr


def _contraction(rows) -> float:
    n = len(rows)
    if n < 76:
        return float("nan")
    prev = _atr1(rows, 60, n - 14)
    return _atr1(rows, 14, n) / prev if prev > 0 else float("nan")


def _relation(tok: str) -> Optional[Tuple[Callable, int]]:
    """(predicate(rows, d, base, d0) -> (keep, new_d), 1m rows it reads) for one token, or None when the token is unknown."""
    from indication_lib import rows_for
    m = _TOKEN.match(tok)
    if not m:
        return None
    if m.group(1):
        kind, tf, n = m.group(1), int(m.group(2)), int(m.group(3) or (TREND_N if m.group(1) in "TtN" else BREAK_N))
        if tf not in (1, 5, 15, 30) or not 3 <= n <= 60:
            return None
        rows_need = rows_for(tf) if kind in "TtN" else min(rows_for(tf), (n + 3) * tf)
        if kind in "TtN":
            def p(rows, d, base, d0, kind=kind, tf=tf, n=n):
                td = _feat(rows, f"T{tf}n{n}", lambda: _trend_dir(rows, tf, n))
                return (td == d if kind == "T" else td == -d if kind == "t" else td == 0), d
        else:
            def p(rows, d, base, d0, kind=kind, tf=tf, n=n):
                bs = _feat(rows, f"B{tf}n{n}", lambda: _break_state(rows, tf, n))
                return (bs == d if kind == "B" else bs == 0), d
        return p, rows_need
    if m.group(4):
        kind, n = m.group(4), int(m.group(5) or MOVE_N)
        if not 3 <= n <= 240:
            return None

        def p(rows, d, base, d0, kind=kind, n=n):
            mv = _feat(rows, f"M{n}", lambda: _prior_move(rows, n))
            if mv != mv:            # nan: too short to judge
                return False, d
            return (mv * d >= 1.0 if kind == "M" else mv * d <= -1.0), d
        return p, n + LAST_PART + 2
    if m.group(6):
        kind, k = m.group(6), int(m.group(7) or LAST_PART)
        if not 1 <= k <= 60:
            return None
        what = "tr" if kind in "Aa" else "vol"

        def p(rows, d, base, d0, kind=kind, k=k, what=what):
            r = _feat(rows, f"{what}{k}", lambda: _ratio(rows, k, what))
            if r != r:
                return False, d
            return (r >= 1.5 if kind in "AV" else r <= 0.7), d
        return p, k + 32
    kind = m.group(8)
    if kind in "DdX":
        def p(rows, d, base, d0, kind=kind):
            x = _feat(rows, f"pb{d}", lambda: _pullback(rows, d))
            if x != x:
                return False, d
            return (0.5 <= x <= 2.5 if kind == "D" else x < 0.3 if kind == "d" else x > 2.5), d
        return p, 32
    if kind in "Cc":
        def p(rows, d, base, d0, kind=kind):
            r = _feat(rows, "ctr", lambda: _contraction(rows))
            if r != r:
                return False, d
            return (r <= 0.75 if kind == "C" else r >= 1.25), d
        return p, 76
    if kind == "I":
        return (lambda rows, d, base, d0: (True, -d)), 0
    if kind in "KF":
        def p(rows, d, base, d0, kind=kind):
            if len(rows) < 2:
                return False, d
            prev = base(rows[:-1])
            same = bool(prev) and int(prev[0]) == d0
            return (same if kind == "K" else not same), d
        return p, 1
    if kind == "G":
        return (lambda rows, d, base, d0: ((float(rows[-1][3]) - float(rows[-2][3])) * d > 0 if len(rows) >= 2 else False, d)), 2
    return None


def split(name: str) -> Tuple[str, List[str]]:
    parts = str(name).split("@")
    return parts[0], parts[1:]


def _compile(name: str) -> Optional[Tuple[Callable[[Sequence[Row]], Signal], int]]:
    from indication_extra import INDICATORS, window_of as base_window
    base_name, toks = split(name)
    base = INDICATORS.get(base_name)
    if base is None or not toks:
        return None
    rels = []
    need = base_window(base_name)
    for t in toks:
        r = _relation(t)
        if r is None:
            return None
        rels.append(r[0])
        need = max(need, r[1] if t not in ("K", "F") else base_window(base_name) + 1)

    def run(rows: Sequence[Row]) -> Signal:
        sig = base(rows)
        if not sig:
            return None
        d0 = d = int(sig[0])
        conf = float(sig[1])
        for p in rels:
            keep, d = p(rows, d, base, d0)        # d0: the indicator's own direction (K / F compare the bar before with it)
            if not keep:
                return None
        return d, conf
    return run, need


def resolve(name: str) -> Optional[Callable[[Sequence[Row]], Signal]]:
    """The callable of a composite name (cached), or None for an unknown base or relation."""
    got = _cache.get(name, False)
    if got is not False:
        return got[0] if got else None
    c = _compile(name)
    with _cache_lock:
        _cache[name] = c
    return c[0] if c else None


def window_of(name: str) -> int:
    """1m rows a composite reads: its indicator's window, or more when a relation reads a longer series."""
    if resolve(name) is None:
        return 0
    return _cache[name][1]


RELATION_TOKENS = ("T5", "T15", "T30", "t5", "t15", "t30", "N15", "B5", "B15", "b15", "M30", "m30", "M60", "m60",
                   "A", "a", "V", "v", "D", "d", "X", "C", "c", "I", "K", "F", "G")
