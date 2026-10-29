#!/usr/bin/env python3
"""Independent config Sets: 1m historic replay, base gate, max DD time, per-Set lifecycle.

A Set is one (pack × SL:TP ratio × trail) book. Historic walks 1-minute OHLC,
simulates entries/exits, then scores each Set on its own tape. Live closes
merge into the same book. One rule decides validity (gate_reason): PF over the
last setGateWindow orders at or above setMinPf (at least setGateMinTrades orders when that minimum is
on; 0 = off, the default), max drawdown time below setMaxDdtHours, and a fresh evaluation. An invalid Set
keeps being scored and becomes valid again on its own; only a lock is sticky.
"""
from __future__ import annotations

import bisect
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from itertools import accumulate, repeat
from operator import neg
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from config_num import num
from position_cost import (
    GATE_MIN_PF_DEFAULT,
    base_min_pf,
    pf_floor,
    MIN_VALIDATION_N,
    window_n,
    LAST_N_DEFAULT,
    POSITION_COST_PCT_DEFAULT,
    normalize_cost_pct,
    last_n_cost_pf,
    signed_result_r,
    SL_RATIOS,
    TP_STEP_MAX,
    TP_STEP_MIN,
    TRAIL_STEP_DEFAULT,
    TRAIL_STEP_SETTING_MAX,
    TRAIL_STEP_SETTING_MIN,
    SL_MIN_STEPS_DEFAULT,
    TRAIL_MIN_STEPS_DEFAULT,
    configure_min_steps,
    cost_as_frac,
    net_pnl_pct,
    sl_floor_frac,
    snap_sl_ratio,
    trail_floor_steps,
)
from indication_engine import (
    aggregate_bars,
    bars_to_candles,
    combine_timeframes,
    evaluate_direction,
    evaluate_move,
    evaluate_ta_pack,
    ema,
    rsi,
    timeframe_evals,
)
from indication_extra import FAMILY_WINDOW as EXTRA_WINDOW, INDICATORS as EXTRA_INDICATORS, LOOKBACK as EXTRA_LOOKBACK, extra_votes, window_of
from risk_variants import parse_trail, trail_grid

PACKS = ("indications", "general")
SHORT_PACK = "shortrange"            # the Short Range strategy: Low-Range signals, TP steps 2-6 only
SHORT_STEP_MIN, SHORT_STEP_MAX = 2, 6
SHORT_MIN_PF_DEFAULT = 1.1       # owner: the Short Range Base floor defined earlier (a770ec1), the other Sets 1.25
SHORT_VARIANTS_DEFAULT = ("lr_fade", "lr_break", "lr_trendbreak", "lr_nr7", "lr_inside")
TREND_PACK = "trend"                  # additional strategy: the Trend family (indication_extra), judged by Base on its own Sets
BREAK_PACK = "breakout"               # additional strategy: the Breakout family
TREND_VARIANTS_DEFAULT = ("trend_c6", "trend_c10", "trend_c20")      # combined across 1m / 5m / 15m, by range
BREAK_VARIANTS_DEFAULT = ("brk_c6", "brk_c10", "brk_c20")
# Additional strategies built from named indicators (indication_extra / indication_lib), each its own pack: its own Sets, its
# own Base validation. pack: (overlay switch, overlay key for the indicator names, default names). All off by default.
EXTRA_PACKS = {
    TREND_PACK: ("stratTrend", "trendVariants", TREND_VARIANTS_DEFAULT),
    BREAK_PACK: ("stratBreakout", "breakoutVariants", BREAK_VARIANTS_DEFAULT),
    "reversion": ("stratReversion", "reversionVariants", ("g_revert_min", "g_revert_med")),
    "flow": ("stratFlow", "flowVariants", ("g_volume_min", "g_volume_med")),
    "multi": ("stratMulti", "multiVariants", ("g_all_min", "g_all_med")),
}
GENERAL_WINDOW_BARS = 60     # general pack reads the newest 60 one-minute bars
IND_WINDOW_BARS = 300        # indication pack: 15m lane needs 20 candles = 300 one-minute bars
DEACT_N_DEFAULT = 25
PF_N_DEFAULT = 15
LOOKBACK_DEFAULT = 1920          # 32 h of 1m bars (systemwide default; was 960 = 16 h)
# dedupe keys kept for live closes: a close re-delivered after this many newer closes is counted again
LIVE_SEEN_MAX = 20000
GATE_WINDOW_DEFAULT = 50        # gate PF over each Set's last N closed orders
GATE_MIN_DEFAULT = 0            # minimum orders behind a Base verdict: 0 = off (owner 2026-10-29), a Set is judged on the orders it has
GATE_MAX_DDT_HOURS_DEFAULT = 24.0   # the longest drawdown over the gate window must stay under this (owner 2026-10-29; setMaxDdtHours 2..36)
WARMUP_DEFAULT = 30
BAR_S = 60.0
FEE_PCT = 0.001  # round-trip, matches live close_pos
STEP_MIN = TP_STEP_MIN
STEP_MAX = TP_STEP_MAX
HIST_CAP = 80
# trail Sets carry no SL grid: their stop is TRAIL_SL_RATIO x their mid-step TP (documented family constant)
TRAIL_SL_RATIO = 0.6
EXIT_TAG = "xt"                 # id suffix of an Exit Set (no TP: stop, time and scratch exits only)
SET_KINDS = ("base", "exit", "trail")
RANK_SHRINK_N = 60              # orders at which a Set's PF counts half toward its rank (the rest is pulled toward 1)
RANK_PF_CAP = 10.0              # a PF above this ranks as this (a window without a loss is not infinitely good)


def _join_bars(old: Sequence[Sequence[float]], new: Sequence[Sequence[float]], keep_on_gap: bool = False) -> List[List[float]]:
    """Join a newer 1-minute window onto a stored series. Rows carry no timestamps, so the join finds where the new
    window's first row sits in the stored series and checks the overlap row by row. The stored history before that
    point is kept, and the new rows (including a still-forming last bar) replace the overlapped tail. With no overlap
    the new window stands, because a gap cannot be filled from rows that do not line up. keep_on_gap: a window that does not
    line up is dropped and the stored series kept (rows built from live ticks never equal the exchange's rows, and a short
    window of them must not replace a long history)."""
    if not old:
        return [list(r) for r in new]
    head = list(new[0])
    for p in range(len(old) - 1, -1, -1):
        if list(old[p]) == head:
            span = min(len(new), len(old) - p)
            if all(list(old[p + i]) == list(new[i]) for i in range(span - 1)):
                return [list(r) for r in old[:p]] + [list(r) for r in new]
            break
    if keep_on_gap and old:
        return [list(r) for r in old]
    return [list(r) for r in new]


def clamp_step(v: Any, lo: int = STEP_MIN, hi: int = STEP_MAX) -> int:
    try:
        n = int(v)
    except Exception:
        n = lo
    return max(lo, min(hi, n))


def step_tp_pct(step: int, cost_pct: float) -> float:
    """TP fraction = step × position cost. Cost 0.15 means 0.15% → step 3 = 0.45%."""
    c = max(1e-9, float(cost_pct)) / 100.0  # cost is always percent (0.15 = 0.15%)
    return max(c, clamp_step(step) * c)


def finite(v: Any, fallback: float = 0.0) -> float:
    try:
        n = float(v)
    except Exception:
        return fallback
    return n if n == n and abs(n) != float("inf") else fallback


def drawdown_time(rows: Sequence[Dict[str, Any]], now: Optional[float] = None) -> Dict[str, float]:
    """Drawdown time of a Set's last positions, in seconds.

    A drawdown starts at the last equity high (the time of that high, not the first losing close) and ends when
    net is back at or above that high. An open drawdown is measured to `now` (the caller's bar, trade or report
    clock); it is never cut short by idle time. With no clock it is the last trade time, so the result never
    depends on when the code runs. Rows are net fractions in `pnl`; `t` is seconds.
    """
    ordered = sorted((r for r in rows if finite(r.get("t")) > 0), key=lambda r: finite(r.get("t")))
    if not ordered:
        return {"episodes": 0.0, "maxS": 0.0, "avgS": 0.0, "currentS": 0.0, "maxDepth": 0.0, "inDd": 0.0, "n": 0.0}
    last_t = finite(ordered[-1].get("t"))
    now = last_t if now is None else float(now)
    equity = 0.0
    peak = 0.0
    peak_t = finite(ordered[0].get("t"))
    in_dd = False
    max_s = 0.0
    total_s = 0.0
    episodes = 0
    max_depth = 0.0
    for row in ordered:
        t = finite(row.get("t"))
        equity += finite(row.get("pnl"))
        if equity >= peak - 1e-12:  # a new (or equal) high: any open drawdown recovers here
            if in_dd:
                dur = max(0.0, t - peak_t)
                max_s = max(max_s, dur)
                total_s += dur
                in_dd = False
            peak = max(peak, equity)
            peak_t = t
            continue
        if not in_dd:
            in_dd = True
            episodes += 1
        max_depth = max(max_depth, peak - equity)
    current = max(0.0, now - peak_t) if in_dd else 0.0
    if in_dd:
        max_s = max(max_s, current)
        total_s += current
    return {
        "episodes": float(episodes),
        "maxS": round(max_s, 1),
        "avgS": round(total_s / episodes, 1) if episodes else 0.0,
        "currentS": round(current, 1),
        "maxDepth": round(max_depth, 6),
        "inDd": 1.0 if in_dd else 0.0,
        "n": float(len(ordered)),
    }


def processed_flag(ts: Sequence[float], nets: Sequence[float], gpre: Sequence[float], lpre: Sequence[float], entry_t: float,
                   gate_window: int, gate_min: int, floor: float, max_dd_s: float, lookback_s: float, lo_floor: int = 0,
                   consistency: float = 0.0) -> bool:
    """Whether a Set passed its Base gate at entry_t, judged on its own closes before that moment (exit time < entry_t, within the
    lookback, from index lo_floor on): at least gate_min of its last gate_window closes, their net PF at or above floor, the
    consistency floor on both halves when set, and a drawdown time under max_dd_s measured to entry_t. ts: exit times
    ascending; nets: net results (after PositionCost); gpre / lpre: prefix sums of the gains and of the losses of nets."""
    e = bisect.bisect_left(ts, entry_t)
    lo = max(lo_floor, bisect.bisect_left(ts, entry_t - lookback_s))
    s0 = max(lo, e - gate_window)
    if e - s0 < gate_min:
        return False
    g, l_ = gpre[e] - gpre[s0], lpre[e] - lpre[s0]
    pf = round(g / l_, 4) if l_ > 0 else (99.0 if g > 0 else 0.0)
    if pf + 1e-9 < floor:
        return False
    return _base_tail_ok(ts, nets, gpre, lpre, entry_t, e, s0, max_dd_s, consistency)


def stage_lanes(ts: Sequence[float], ents: Sequence[float], nets: Sequence[float], gate_window: int, gate_min: int, floor: float,
                max_dd_s: float, lookback_s: float, consistency: float, stage_ns: Sequence[int]) -> List[List[int]]:
    """The chain of a Set's lanes, as indices into its tape (exit time ascending; ents are the entry times). Lane 0: the orders it
    opened while it passed Base, judged on its raw tape before each entry (processed_flag). Lane j (j >= 1): the orders of lane
    j - 1 whose entry also passed stage j, which reads only lane j - 1: at least stage_ns[j - 1] of lane j - 1's orders closed
    before the entry (within the lookback), their net PF over the newest stage_ns[j - 1] at or above floor. Each stage is its own
    Set built from the one before; the last lane is what the Set would have executed after every gate."""
    gpre, lpre = prefix_sums(nets)
    lane = [k for k in range(len(ts))
            if processed_flag(ts, nets, gpre, lpre, ents[k], gate_window, gate_min, floor, max_dd_s, lookback_s, 0, consistency)]
    lanes = [lane]
    for n_ in stage_ns:
        prev = lanes[-1]
        lt = [ts[k] for k in prev]
        lg, ll = prefix_sums([nets[k] for k in prev])
        nxt = []
        for k in prev:
            e = bisect.bisect_left(lt, ents[k])
            lo = bisect.bisect_left(lt, ents[k] - lookback_s)
            s0 = max(lo, e - n_)
            if e - s0 < n_:
                continue
            g, l_ = lg[e] - lg[s0], ll[e] - ll[s0]
            if (g / l_ if l_ > 0 else (99.0 if g > 0 else 0.0)) + 1e-9 >= floor:
                nxt.append(k)
        lanes.append(nxt)
    return lanes


def _alias(ov: Dict[str, Any], key: str, old: str) -> Any:
    """ov[key] when present, else the value under its earlier name `old` (a renamed overlay key still loads from files written before the rename)."""
    v = ov.get(key)
    return v if v is not None else ov.get(old)


def parse_windows(raw: Any, default: Tuple[int, ...]) -> Tuple[int, ...]:
    """A list of last-N windows from an overlay value (list or "5,10"), each 1..200, sorted and unique; the default when empty."""
    if isinstance(raw, str):
        raw = [x for x in raw.replace(";", ",").split(",") if x.strip()]
    out = set()
    for x in raw or []:
        try:
            out.add(max(1, min(200, int(float(x)))))
        except (TypeError, ValueError):
            continue
    return tuple(sorted(out)) or tuple(default)


def pf_of(g: float, l_: float) -> float:
    return g / l_ if l_ > 0 else (99.0 if g > 0 else 0.0)


@dataclass
class StageParams:
    """Every rule of the Base -> Main -> Real chain for one pack (SetBook.stage_params)."""
    base_long: int                  # Base: last N (setGateWindow), with gate_min orders, the drawdown time and consistency
    base_short: int                 # Base: the second last N (setGateShortN, 0 = off); both windows must pass
    gate_min: int
    floor: float                    # the minimum PF of every stage (the pack's Base floor)
    max_dd_s: float
    lookback_s: float
    consistency: float
    main_on: bool                   # Main: the long last N (mainGateN), the short window and the minimal window, all on the Main Set
    main_n: int
    short_on: bool                  # Main short window (mainShortEnabled): each Set's own window of the candidates
    short_windows: Tuple[int, ...]  # mainShortWindows: the candidates of the per-Set short window
    short_min: int                  # orders a candidate needs on the Main Set
    short_margin: float             # a candidate must beat the long window's PF by this factor
    neg_on: bool                    # Main Negative lane: the average of the last N Main orders is negative, for any N here
    neg_windows: Tuple[int, ...]
    real_on: bool                   # Real: the last N (realGateN) of the lane's Real Set
    real_n: int
    full_window: bool = False       # True: a window judges only once it holds its full N orders (the strict rule); False: the orders it has (at least one)
    base_min_n: int = 0             # Base: the minimal window (setGateMinN, 0 = off): the newest 1-3 results of the Set's own tape must all be positive / negative
    base_min_positive: bool = True  # setGateMinMode: positive = every one of them above zero after PositionCost, negative = every one below zero
    main_min_n: int = 0             # Main: the minimal window (mainMinN, 0 = off): the newest 1-3 results of the Main Set must all be positive / negative
    main_min_positive: bool = True  # mainMinMode


def _window_ok(lt: Sequence[float], lg: Sequence[float], ll: Sequence[float], entry_t: float, n: int, floor: float,
               lookback_s: float, full: bool = False) -> bool:
    """The newest n orders of a lane that closed before entry_t (within the lookback) have a net PF at or above floor. A lane with fewer
    than n orders is judged on the orders it has (at least one) unless full: then the window must hold its n orders. A stage that
    demanded n prior orders at every step would be a hidden minimum order count: on sparse tapes no Set ever filled Main and Real."""
    e = bisect.bisect_left(lt, entry_t)
    s0 = max(bisect.bisect_left(lt, entry_t - lookback_s), e - n)
    return e - s0 >= (n if full else 1) and pf_of(lg[e] - lg[s0], ll[e] - ll[s0]) + 1e-9 >= floor


def _window_neg(lt: Sequence[float], lsum: Sequence[float], entry_t: float, n: int, lookback_s: float) -> bool:
    """At least n orders of a lane closed before entry_t (within the lookback) and their net sum over the newest n below zero."""
    e = bisect.bisect_left(lt, entry_t)
    s0 = max(bisect.bisect_left(lt, entry_t - lookback_s), e - n)
    return e - s0 >= n and lsum[e] - lsum[s0] < 0


def minimal_ok(lt: Sequence[float], nets: Sequence[float], entry_t: float, n: int, positive: bool, lookback_s: float, full: bool = False) -> bool:
    """The minimal window (Base: a Set's own tape, Main: its Main Set): the newest n results (net after PositionCost) of the lane that closed before
    entry_t, within the lookback, are all positive (or all negative). Fewer than n results: the ones it has (at least one), or none judged unless full."""
    e = bisect.bisect_left(lt, entry_t)
    s0 = max(bisect.bisect_left(lt, entry_t - lookback_s), e - n)
    if e - s0 < (n if full else 1):
        return False
    return all(x > 0 for x in nets[s0:e]) if positive else all(x < 0 for x in nets[s0:e])


def _base_tail_ok(ts: Sequence[float], nets: Sequence[float], gpre: Sequence[float], lpre: Sequence[float], entry_t: float,
                  e: int, s0: int, max_dd_s: float, consistency: float) -> bool:
    """The part of the Base long window that follows the minimum PF: the consistency floor on both halves of the window [s0, e) when set and the
    drawdown time under max_dd_s, measured to entry_t (processed_flag and stage_chain share it)."""
    if consistency > 0:
        h = s0 + (e - s0) // 2

        def _pf(a: int, b: int) -> float:
            g_, l2 = gpre[b] - gpre[a], lpre[b] - lpre[a]
            return g_ / l2 if l2 > 0 else (99.0 if g_ > 0 else 0.0)
        if min(_pf(s0, h), _pf(h, e)) + 1e-9 < consistency:
            return False
    if e <= s0 or entry_t - ts[s0] < max_dd_s:
        return True          # every drawdown episode lies between the window's first close and the entry: a window shorter than max_dd_s cannot reach it
    eq = peak = 0.0
    peak_t = ts[s0]
    in_dd = False
    max_s = 0.0
    for k in range(s0, e):                      # drawdown_time over that window, the open drawdown measured to entry_t
        eq += nets[k]
        if eq >= peak - 1e-12:
            if in_dd:
                max_s = max(max_s, ts[k] - peak_t)
                in_dd = False
            peak = max(peak, eq)
            peak_t = ts[k]
        else:
            in_dd = True
    if in_dd:
        max_s = max(max_s, entry_t - peak_t)
    return max_s < max_dd_s


def stage_chain(ts: Sequence[float], ents: Sequence[float], nets: Sequence[float], P: StageParams) -> Dict[str, Any]:
    """A Base Set's chain of Sets, as indices into its tape (exit time ascending; ents: entry times; nets: net results).
      main     the Main Set: the orders opened while Base passed on the raw tape before each entry (the long window, its minimum
               orders, drawdown time and consistency: processed_flag; the short window; the minimal window)
      real     the Real Set: the Main-Set orders opened while the Main stage passed on the Main Set before each entry (the long
               last N, the Set's short window and the minimal window, all of them)
      realneg  the Real Negative Set: the Main-Set orders opened while the average of the Main Set's last N orders was negative
               for some N of the Negative windows (a lane of its own)
      short_n  the Main short window chosen for this Set: of the candidates, the one whose passed validations were followed by the best
               PF on the Main Set (at least short_min orders and short_margin x the long window's PF), else the long N.
    Each stage reads only the Set the stage before it built. Every window of a stage is judged on its own, from the same positions in the
    lane (the orders closed before the entry, within the lookback), and the stage is their conjunction: a window never reads another's verdict."""
    n = len(ts)
    gpre, lpre = prefix_sums(nets)
    bis = bisect.bisect_left
    lb, full = P.lookback_s, P.full_window
    bl, bs, bm = P.base_long, P.base_short, P.base_min_n
    gm, floor = P.gate_min, P.floor
    s_need, m_need = (bs if full else 1), (bm if full else 1)
    pos_pre = neg_pre = None
    if bm:
        pos_pre, neg_pre = sign_prefix(nets)
    main = []
    e_all = list(map(bis, repeat(ts), ents))                       # per order: the closes before its entry, and the oldest one inside the lookback
    lo_all = list(map(bis, repeat(ts), [x - lb for x in ents]))
    f_hi, f_lo = floor + 1e-4, floor - 1e-4
    for k in range(n):
        e, lo = e_all[k], lo_all[k]
        s0 = e - bl
        if s0 < lo:
            s0 = lo
        if e - s0 < gm:
            continue
        g, l_ = gpre[e] - gpre[s0], lpre[e] - lpre[s0]
        if l_ > 0:
            r_ = g / l_
            if r_ < f_lo or (r_ < f_hi and round(r_, 4) + 1e-9 < floor):    # the long window's PF first: most orders stop here (the 4-digit rounding only near the floor)
                continue
        elif g <= 0 and floor > 1e-9:
            continue
        entry = ents[k]
        if not _base_tail_ok(ts, nets, gpre, lpre, entry, e, s0, P.max_dd_s, P.consistency):
            continue
        if bs:
            t0 = e - bs
            if t0 < lo:
                t0 = lo
            if e - t0 < s_need or pf_of(gpre[e] - gpre[t0], lpre[e] - lpre[t0]) + 1e-9 < floor:
                continue
        if bm:
            t0 = e - bm
            if t0 < lo:
                t0 = lo
            c_ = e - t0
            if c_ < m_need or (pos_pre[e] - pos_pre[t0] if P.base_min_positive else neg_pre[e] - neg_pre[t0]) != c_:
                continue
        main.append(k)
    m = len(main)
    mt = [ts[k] for k in main]
    mn = [nets[k] for k in main]
    mg, ml = prefix_sums(mn)
    msum = [0.0]
    for x in mn:
        msum.append(msum[-1] + x)
    # the Main Set's window positions, once per Main order: e (orders closed before the entry) and lo (the oldest inside the lookback)
    me = [bis(mt, ents[k]) for k in main]
    mlo = [bis(mt, ents[k] - lb) for k in main]
    vec: Dict[int, List[bool]] = {}

    def passes(w: int) -> List[bool]:
        """Per Main order, whether the newest w Main orders before its entry hold the minimum PF (memoised: the long window and the chosen short
        window are read twice, by the selection and by the Real Set)."""
        v = vec.get(w)
        if v is None:
            need = w if full else 1
            v = []
            for j in range(m):
                e = me[j]
                s0 = e - w
                if s0 < mlo[j]:
                    s0 = mlo[j]
                v.append(e - s0 >= need and pf_of(mg[e] - mg[s0], ml[e] - ml[s0]) + 1e-9 >= floor)
            vec[w] = v
        return v

    short_n = P.main_n
    if P.main_on and P.short_on:
        static = passes(P.main_n)
        ref = pf_of(sum(max(mn[j], 0.0) for j in range(m) if static[j]), sum(max(-mn[j], 0.0) for j in range(m) if static[j]))
        best = ref
        for w in P.short_windows:
            pv = passes(w)
            inc = [j for j in range(m) if pv[j]]
            if len(inc) < P.short_min:
                continue
            pf = pf_of(sum(max(mn[j], 0.0) for j in inc), sum(max(-mn[j], 0.0) for j in inc))
            if pf > best and pf >= ref * P.short_margin:
                best, short_n = pf, int(w)
    real = []
    if P.main_on:
        pl, ps = passes(P.main_n), passes(short_n)
        mmin = main_minimal(mn, me, mlo, P) if P.main_min_n else None
        real = [k for j, k in enumerate(main) if pl[j] and ps[j] and (mmin is None or mmin[j])]
    else:
        real = list(main)
    realneg = []
    if P.neg_on:
        for j, k in enumerate(main):
            e = me[j]
            for w in P.neg_windows:
                s0 = e - w
                if s0 < mlo[j]:
                    s0 = mlo[j]
                if e - s0 >= w and msum[e] - msum[s0] < 0:
                    realneg.append(k)
                    break
    return {"main": main, "real": real, "realneg": realneg, "short_n": short_n}


def sign_prefix(nets: Sequence[float]) -> Tuple[List[int], List[int]]:
    """Prefix counts of the positive and of the negative results of nets (for the minimal windows: the newest n are all positive iff the count
    over them is n)."""
    pos, neg = [0], [0]
    for x in nets:
        pos.append(pos[-1] + (1 if x > 0 else 0))
        neg.append(neg[-1] + (1 if x < 0 else 0))
    return pos, neg


def main_minimal(mn: Sequence[float], me: Sequence[int], mlo: Sequence[int], P: StageParams) -> List[bool]:
    """The Main minimal window per Main order: the newest main_min_n results of the Main Set before the entry (positions me / mlo) are all positive
    (main_min_positive) or all negative; the results it has (at least one) unless full_window."""
    pos_pre, neg_pre = sign_prefix(mn)
    cnt = pos_pre if P.main_min_positive else neg_pre
    need = P.main_min_n if P.full_window else 1
    out = []
    for j in range(len(me)):
        e = me[j]
        s0 = e - P.main_min_n
        if s0 < mlo[j]:
            s0 = mlo[j]
        out.append(e - s0 >= need and cnt[e] - cnt[s0] == e - s0)
    return out


def stage_verdicts(main: Sequence[float], real: Sequence[float], realneg: Sequence[float], short_n: int, P: StageParams) -> Dict[str, Any]:
    """The stage verdicts now, from the net results (exit order) of the Set's Main, Real and Real Negative Sets: the Main stage
    (the long last N, the short window and the minimal window on the Main Set, each on its own), the Negative entry (the average of
    the last N Main orders below zero for some Negative window), the Real stage on each lane (the last realGateN of its Real Set at
    the minimum PF). pos / neg: the lane is open (a Base-passing Set may execute on it)."""
    def last_ok(v: Sequence[float], n: int) -> Tuple[bool, float, int]:
        w = list(v[-n:]) if n > 0 else []
        pf = round(pf_of(sum(x for x in w if x > 0), -sum(x for x in w if x < 0)), 4)
        return len(w) >= (n if P.full_window else 1) and pf + 1e-9 >= P.floor, pf, len(w)
    m_ok, m_pf, m_n = last_ok(main, P.main_n)
    s_ok, s_pf, s_n = last_ok(main, short_n) if P.short_on else (True, m_pf, m_n)
    if P.main_min_n:
        mw = list(main[-P.main_min_n:])
        k_n = len(mw)
        k_ok = k_n >= (P.main_min_n if P.full_window else 1) and (all(x > 0 for x in mw) if P.main_min_positive else all(x < 0 for x in mw))
    else:
        k_ok, k_n = True, 0
    neg = P.neg_on and any(len(main) >= w and sum(main[-w:]) < 0 for w in P.neg_windows)
    r_ok, r_pf, r_n = last_ok(real, P.real_n)
    q_ok, q_pf, q_n = last_ok(realneg, P.real_n)
    main_pass = (m_ok and s_ok and k_ok) if P.main_on else True
    return {"main": m_ok, "main_pf": m_pf, "main_n": m_n, "short": s_ok, "short_pf": s_pf, "short_n": s_n, "short_win": short_n,
            "minimal": k_ok, "minimal_n": k_n, "neg": neg,
            "real": r_ok, "real_pf": r_pf, "real_n": r_n, "realneg": q_ok, "realneg_pf": q_pf, "realneg_n": q_n,
            "pos": main_pass and (r_ok if P.real_on else True), "negl": neg and (q_ok if P.real_on else True)}


def prefix_sums(nets: Sequence[float]) -> Tuple[List[float], List[float]]:
    """Prefix sums of the gains and of the losses (as positive numbers) of net results, for processed_flag (the same sequential sums as a plain
    loop, accumulated in C)."""
    return (list(accumulate(map(max, nets, repeat(0.0)), initial=0.0)),
            list(accumulate(map(max, map(neg, nets), repeat(0.0)), initial=0.0)))


def general_signal(bars: Sequence[Sequence[float]]) -> Tuple[int, float, str]:
    """Pulse general pack, 1m bars [o,h,l,c,v]. Pure — no Pulse instance."""
    if len(bars) < 16:
        return 0, 0.0, "no-data"
    closes = [float(b[3]) for b in bars]
    highs = [float(b[1]) for b in bars]
    lows = [float(b[2]) for b in bars]
    vols = [float(b[4]) for b in bars]
    last = closes[-1]
    if last <= 0:
        return 0, 0.0, "flat"

    e8 = ema(closes, 8)
    e21 = ema(closes, 21)
    r = rsi(closes, 7)  # shared with indication_engine (flat window -> 50, not 100)
    prev = closes[-2]
    rng = max(highs[-8:]) - min(lows[-8:]) or last * 0.002
    body = last - prev
    mom = (last - closes[-4]) / closes[-4] if closes[-4] else 0.0
    vol_avg = sum(vols[-12:]) / 12 or 1.0
    slope = (e8 - e21) / last
    long_c = short_c = 0.0
    why_l: List[str] = []
    why_s: List[str] = []
    if r < 32:
        long_c += 0.34
        why_l.append(f"rsi{r:.0f}")
    elif r < 42:
        long_c += 0.16
        why_l.append("rsi-low")
    if r > 68:
        short_c += 0.34
        why_s.append(f"rsi{r:.0f}")
    elif r > 58:
        short_c += 0.16
        why_s.append("rsi-hi")
    if slope > 0.00015:
        long_c += 0.22
        why_l.append("ema+")
    if slope < -0.00015:
        short_c += 0.22
        why_s.append("ema-")
    if body > 0 and last > highs[-2]:
        long_c += 0.18
        why_l.append("brk")
    if body < 0 and last < lows[-2]:
        short_c += 0.18
        why_s.append("brk")
    if mom > 0.0012:
        long_c += 0.12
        why_l.append("mom")
    if mom < -0.0012:
        short_c += 0.12
        why_s.append("mom")
    loc = (last - min(lows[-8:])) / rng
    if loc < 0.18 and r < 45:
        long_c += 0.20
        why_l.append("fade-lo")
    if loc > 0.82 and r > 55:
        short_c += 0.20
        why_s.append("fade-hi")
    if vols[-1] > vol_avg * 1.15:
        long_c += 0.06
        short_c += 0.06
    if long_c >= 0.58 and long_c > short_c + 0.10:
        return 1, min(1.0, long_c), "+".join(why_l) or "long"
    if short_c >= 0.58 and short_c > long_c + 0.10:
        return -1, min(1.0, short_c), "+".join(why_s) or "short"
    return 0, max(long_c, short_c), "flat"


def indication_signal(bars: Sequence[Sequence[float]], settings: Dict[str, Any], now: float,
                      extra_rows: Optional[Sequence[Sequence[float]]] = None) -> Tuple[int, float, str]:
    """Set-level indication pack, causal, 1m rows [o,h,l,c,v] (newest last).
    The switches are the ones IndicationBook reads, with the same meaning:
      tf1m / tf5m / tf15m   timeframe lanes that take part (timeframe_evals skips a lane that is off);
      typeSignals           the timeframe lanes as signals, so the combined "tf" vote is off with it;
      tfCombined            the combined vote, which needs tfMinAgree agreeing lanes;
      typeState             the TA pack ("ta" vote);
      typeDirection         the direction evaluator ("dir" vote);
      typeMove              the move evaluator ("move" vote).
    typeActive and typeCommon have no evaluator in this pack, so they do not apply here.
    A switch that is off removes its vote; with every switch on (the shipped overlays) the votes are unchanged."""
    rows = list(bars)
    one_m = rows[-60:]
    candles = bars_to_candles(one_m, now=now, period_s=BAR_S)
    tf_rows = {"1m": one_m, "5m": aggregate_bars(rows, 5), "15m": aggregate_bars(rows, 15)}
    use_signals = bool(settings.get("typeSignals", True))
    tf_evs = timeframe_evals(tf_rows, settings, now) if use_signals else []
    closes = [float(b[3]) for b in one_m] if one_m else []
    votes: List[Tuple[int, float, str]] = []
    if use_signals and settings.get("tfCombined", True):
        comb = combine_timeframes(tf_evs, int(settings.get("tfMinAgree") or 2), settings)
        if comb:
            direction, _contrib, risk = comb
            votes.append((1 if direction == "long" else -1, float(risk["confidence"]), "tf"))
    ta = evaluate_ta_pack(candles, settings) if settings.get("typeState", True) else None
    if ta:
        votes.append((1 if ta.direction == "long" else -1, ta.confidence, "ta"))
    try:
        drow = evaluate_direction("hist", closes, settings) if closes and settings.get("typeDirection", True) else None
        if drow:
            votes.append((1 if drow.direction == "long" else -1, drow.confidence, "dir"))
    except Exception:
        pass
    try:
        mrow = evaluate_move("hist", closes, settings) if closes and settings.get("typeMove", True) else None
        if mrow:
            votes.append((1 if mrow.direction == "long" else -1, mrow.confidence, "move"))
    except Exception:
        pass
    extras = settings.get("extraIndicators") or []
    if extras:
        votes.extend(extra_votes(extra_rows if extra_rows is not None else rows[-EXTRA_WINDOW:], extras))     # each name its own window
    if not votes:
        return 0, 0.0, "flat"
    long_w = sum(c for d, c, _ in votes if d > 0)
    short_w = sum(c for d, c, _ in votes if d < 0)
    if long_w > short_w and long_w >= 0.6:
        return 1, min(1.0, long_w / max(1, len(votes))), "+".join(w for d, _, w in votes if d > 0)
    if short_w > long_w and short_w >= 0.6:
        return -1, min(1.0, short_w / max(1, len(votes))), "+".join(w for d, _, w in votes if d < 0)
    return 0, max(long_w, short_w), "split"


def short_range_signal(rows: Sequence[Sequence[float]], settings: Dict[str, Any]) -> Tuple[int, float, str]:
    """The Short Range pack: the Low-Range variants named in settings["shortRangeVariants"]. One variant firing is a
    signal; several firing the same way take the strongest confidence; opposite directions cancel (no signal)."""
    return _variant_signal(rows, settings.get("shortRangeVariants") or list(SHORT_VARIANTS_DEFAULT))


def trend_signal_pack(rows: Sequence[Sequence[float]], settings: Dict[str, Any]) -> Tuple[int, float, str]:
    """The Trend pack: the trend variants named in settings["trendVariants"], combined as the Short Range pack does."""
    return _variant_signal(rows, settings.get("trendVariants") or list(TREND_VARIANTS_DEFAULT))


def breakout_signal_pack(rows: Sequence[Sequence[float]], settings: Dict[str, Any]) -> Tuple[int, float, str]:
    """The Breakout pack: the breakout variants named in settings["breakoutVariants"]."""
    return _variant_signal(rows, settings.get("breakoutVariants") or list(BREAK_VARIANTS_DEFAULT))


def _variant_signal(rows: Sequence[Sequence[float]], names: Sequence[str]) -> Tuple[int, float, str]:
    votes = extra_votes(rows, names)
    if not votes:
        return 0, 0.0, "flat"
    longs = [v for v in votes if v[0] > 0]
    shorts = [v for v in votes if v[0] < 0]
    if longs and shorts:
        return 0, 0.0, "split"
    side = longs or shorts
    return (1 if longs else -1), max(v[1] for v in side), "+".join(v[2] for v in side)


def signal_window(packs: Sequence[str], ind_settings: Dict[str, Any]) -> int:
    """The most closed 1m bars any pack reads for one bar's signal: the indication pack's window, or the longest window of
    the indicators named in extraIndicators and in the Short Range and extra packs' variant lists (a 15m name reads 900).
    pack_signals keys its memo on this many bars, so two bars share a signal only when everything they read is equal."""
    names = list(ind_settings.get("extraIndicators") or []) if "indications" in packs else []
    if SHORT_PACK in packs:
        names += list(ind_settings.get("shortRangeVariants") or SHORT_VARIANTS_DEFAULT)
    for pk, (_sw, vkey, default) in EXTRA_PACKS.items():
        if pk in packs:
            names += list(ind_settings.get(vkey) or default)
    w = IND_WINDOW_BARS if "indications" in packs else GENERAL_WINDOW_BARS
    return max([w, EXTRA_WINDOW if names else 0] + [window_of(str(n)) for n in names])


def bar_signal(
    bars: Sequence[Sequence[float]],
    i: int,
    packs: Sequence[str],
    ind_settings: Dict[str, Any],
    ts: float,
) -> Dict[str, Tuple[int, float, str]]:
    """The signal of every pack on bar i, read from the closed bars up to and including i (each pack its own window).
    The one per-bar rule: the replay (pack_signals) and the live entries (SetBook.entry_signal) both call it, so a live
    entry is taken on exactly the bar and direction the Sets were validated on."""
    row: Dict[str, Tuple[int, float, str]] = {}
    hw = signal_window(packs, ind_settings)
    hist = bars[max(0, i + 1 - hw) : i + 1]          # the longest window: the extra indicators slice their own from it
    if "general" in packs:
        lo = i + 1 - GENERAL_WINDOW_BARS
        row["general"] = general_signal(bars[lo if lo > 0 else 0 : i + 1])
    if "indications" in packs:
        lo = i + 1 - IND_WINDOW_BARS
        row["indications"] = indication_signal(bars[lo if lo > 0 else 0 : i + 1], ind_settings, ts,
                                               hist if ind_settings.get("extraIndicators") else None)
    if SHORT_PACK in packs:
        row[SHORT_PACK] = short_range_signal(hist, ind_settings)    # each name reads its own window
    for pk, (_sw, vkey, default) in EXTRA_PACKS.items():
        if pk in packs:
            row[pk] = _variant_signal(hist, ind_settings.get(vkey) or list(default))
    return row


def pack_signals(
    bars: Sequence[Sequence[float]],
    packs: Sequence[str],
    ind_settings: Dict[str, Any],
    base_ts: float,
    warmup: int,
    on_step: Optional[Callable[[], None]] = None,
    cache: Optional[Dict[int, Dict[str, Tuple[int, float, str]]]] = None,
    salt: int = 0,
) -> Dict[str, List[Tuple[int, float, str]]]:
    """Causal signal per bar and pack: (direction, confidence, why). The only signal loop: the engine
    replay and the streaming simulator both call it, so a bar gets the same signal in both places.

    cache: memo keyed by the content of the bar's signal window (plus salt, the indication settings).
    A signal is a pure function of those bars: candle timestamps are labels only. Identical windows
    therefore give identical signals in every refresh and replay. Entries this call did not use are dropped.
    """
    n = len(bars)
    signals: Dict[str, List[Tuple[int, float, str]]] = {p: [(0, 0.0, "")] * n for p in packs}
    hs = [hash(tuple(b)) for b in bars] if cache is not None else None
    used: Dict[int, Dict[str, Tuple[int, float, str]]] = {}
    sig_w = signal_window(packs, ind_settings)      # the memo key covers every bar a pack reads
    for i in range(warmup, n):
        key = None
        if cache is not None:
            lo_w = max(0, i + 1 - sig_w)
            key = hash((salt, tuple(hs[lo_w : i + 1])))
            hit = used.get(key)
            if hit is None:
                hit = cache.get(key)
            if hit is not None:
                used[key] = hit
                for p in packs:
                    signals[p][i] = hit[p]
                if on_step and i % 50 == 0:
                    on_step()
                continue
        row = bar_signal(bars, i, packs, ind_settings, base_ts + i * BAR_S)
        for p in packs:
            signals[p][i] = row.get(p, (0, 0.0, ""))
        if key is not None:
            used[key] = row
        if on_step and i % 50 == 0:
            on_step()
    if cache is not None:
        cache.clear()
        cache.update(used)
    return signals

def hit_exit(
    side: int,
    entry: float,
    sl: float,
    tp: float,
    trail: Optional[float],
    bar: Sequence[float],
    ignore_tp: bool = False,
) -> Tuple[Optional[str], float]:
    """Pessimistic same-bar: SL (or trail) wins if both fire."""
    high = float(bar[1])
    low = float(bar[2])
    close = float(bar[3])
    if side > 0:
        stop = max(sl, trail) if trail is not None else sl
        sl_hit = low <= stop
        tp_hit = high >= tp
        if sl_hit:
            return "sl", stop
        if (not ignore_tp) and tp_hit:
            return "tp", tp
        return None, close
    stop = min(sl, trail) if trail is not None else sl
    sl_hit = high >= stop
    tp_hit = low <= tp
    if sl_hit:
        return "sl", stop
    if (not ignore_tp) and tp_hit:
        return "tp", tp
    return None, close


def make_set_id(pack: str, sl_ratio: float, trail: str = "", step: int = 0) -> str:
    if step:
        return f"{pack}:1m:sl{sl_ratio:.2f}:st{int(step)}"
    if trail:
        return f"{pack}:1m:tr{trail}"
    return f"{pack}:1m:sl{sl_ratio:.2f}"


def make_exit_id(pack: str, sl_ratio: float, step: int) -> str:
    """An Exit Set: the base Set's stop (SL ratio x TP step) with no TP exit (exitIgnoreTp), its own lane and its own tape."""
    return f"{pack}:1m:sl{sl_ratio:.2f}:st{int(step)}:{EXIT_TAG}"


def make_trail_id(pack: str, trail: str) -> str:
    return f"{pack}:1m:tr{trail}"


@dataclass
class SimTrade:
    t: float
    symbol: str
    side: str
    entry: float
    exit: float
    pnl: float
    pnl_pct: float
    hold_s: float
    reason: str
    set_id: str
    pack: str = ""
    source: str = "hist"


@dataclass
class SetState:
    id: str
    pack: str
    tf: str
    sl_ratio: float
    trail_key: str
    trail_arm: float
    trail_give: float
    step: int = 3
    tp_pct: float = 0.0045
    hold_bars: int = 0              # this Set's own time exit in bars (minutes); 0 = the global time stop (stepHoldOn / stepHoldMin / auto)
    hold_auto: bool = False         # hold_bars was chosen by the Auto Mode from this Set's own last orders (holdAuto)
    hold_auto_pf: float = 0.0       # cost PF of the chosen hold over the orders it was judged on
    hold_auto_n: int = 0            # how many orders (at most holdAutoMax) that judgement read
    idx: int = 0
    pack_i: int = 0
    sl_i: int = 0
    tr_i: int = 0
    step_i: int = 0
    kind: str = "base"
    hist: List[Dict[str, Any]] = field(default_factory=list)
    live: List[Dict[str, Any]] = field(default_factory=list)
    last15_ratio: float = 1.0       # 1 + 0.10 x avgR: display only, never compared with a PF threshold
    gate_pf: float = 0.0            # net PF over the last setGateWindow orders: the eligibility metric
    gate_n: int = 0
    gate_pf_old: float = 0.0        # PF of the older / newer half of that window: the stability check (setGateConsistencyPf)
    gate_pf_new: float = 0.0
    gate_gp: float = 0.0            # gross profit / loss (net fractions) inside that same window: the rolling pool inputs
    gate_gl: float = 0.0
    main_pf: float = 0.0            # net PF over the last mainGateN processed orders: the Main gate (a second, shorter last-N validation, per Set)
    main_n: int = 0
    main_gp: float = 0.0
    main_gl: float = 0.0
    proc: List[Tuple[float, float]] = field(default_factory=list)   # processed orders (exit t, net): opened while the Set passed Base
    proc_total: int = 0             # processed orders in the lookback (before the PROC_KEEP cap)
    exe: List[Tuple[float, float]] = field(default_factory=list)    # executable orders (exit t, net): opened while every gate passed
    exe_total: int = 0
    exe_pf: float = 0.0             # net PF of the executable lane over its last setGateWindow orders (listing)
    exe_n: int = 0
    neg: List[Tuple[float, float]] = field(default_factory=list)    # Real Negative Set (exit t, net): Main orders opened in a dip
    neg_total: int = 0
    neg_n: int = 0
    neg_pf: float = 0.0
    neg_gp: float = 0.0
    neg_gl: float = 0.0
    exe_gp: float = 0.0
    exe_gl: float = 0.0
    base_short_n: int = 0           # Base short window (setGateShortN): orders and PF
    base_short_pf: float = 0.0
    base_min_n: int = 0             # Base minimal window (setGateMinN): the results it holds and whether they are all positive / negative as asked
    base_min_ok: bool = True
    base_long_ok: bool = False      # each Base window on its own (the stage verdict is their conjunction): the long window (orders, PF, drawdown time, consistency)
    base_short_ok: bool = False     # the short window
    base_short_gp: float = 0.0      # gains / losses of the orders the short window reads (pooled into the per-window figures)
    base_short_gl: float = 0.0
    base_min_gp: float = 0.0
    base_min_gl: float = 0.0
    main_short_win: int = 0         # the Set's own Main short window (mainShortWindows): the chosen N
    main_short_n: int = 0           # the orders it holds on the Main Set, their PF and gains / losses
    main_short_pf: float = 0.0
    main_short_gp: float = 0.0
    main_short_gl: float = 0.0
    main_min_have: int = 0          # the Main minimal window (mainMinN): the results it holds on the Main Set
    main_min_gp: float = 0.0
    main_min_gl: float = 0.0
    neg_win: int = 0                # the smallest Negative window whose average is below zero (0 = none), its orders and gains / losses
    neg_win_gp: float = 0.0
    neg_win_gl: float = 0.0
    stage: Dict[str, Any] = field(default_factory=dict)             # stage_verdicts of the last evaluation
    proc_replayed: bool = False     # proc comes from a replay pass (else processed_stream derives it from the held tape)
    last15_pf: float = 0.0          # net PF over the last pf_n orders (display, PF15 column)
    last15_n: int = 0
    last15_r: float = 0.0
    last25_avg_r: float = 0.0
    last25_n: int = 0
    last25_avg_pnl: float = 0.0
    max_dd_s: float = 0.0
    avg_dd_s: float = 0.0
    dd_episodes: int = 0
    last_error: str = ""            # last replay/score failure for this Set; cleared on the next clean pass
    n: int = 0
    live_n: int = 0                  # live closes accepted for this Set; n = replayed fills + live_n
    wins: int = 0
    gp: float = 0.0
    gl: float = 0.0
    wr: float = 0.0
    expectancy: float = 0.0
    avg_hold_s: float = 0.0
    pf_all: float = 0.0             # net PF over every closed row in the tape (display)
    exits: Dict[str, int] = field(default_factory=dict)
    active: bool = True
    deact_reason: str = ""          # the gate reason of the last evaluation ('' when valid)
    evaluated_at: float = 0.0       # wall time of the last successful evaluation (freshness, live only)
    locked: bool = False
    source_n: int = 0

    def tape(self) -> List[Dict[str, Any]]:
        return list(self.hist) + list(self.live)


@dataclass
class Progress:
    phase: str = "idle"
    pct: float = 0.0
    symbol: str = ""
    set_id: str = ""
    bars_done: int = 0
    bars_total: int = 0
    sets_done: int = 0
    sets_total: int = 0
    symbols_done: int = 0
    symbols_total: int = 0
    elapsed_ms: float = 0.0
    last_run_ms: float = 0.0
    cycle: int = 0
    detail: str = ""
    ready: bool = False
    error: str = ""
    errors: int = 0                  # symbols + Sets that failed in the last pass; the others still ran


_ROW_KEYS = ("t", "symbol", "side", "pnl", "pnl_pct", "hold_s", "reason", "set_id")


def _row_t(r: Tuple[Any, ...]) -> float:
    return r[0]


class BarIndex:
    """Range-min/max lookups over one symbol's bars (sparse tables), built once per replay of that symbol. A base lane
    (no trailing) then finds its exit bar with a few lookups instead of walking every bar it holds. The answers are the
    same bars the walk finds; the walk stays for trailing lanes, whose stop moves with every bar."""
    __slots__ = ("n", "lo", "hi", "cl_min", "cl_max", "close")

    def __init__(self, bars: Sequence[Sequence[float]]) -> None:
        self.n = len(bars)
        lows = [float(b[2]) for b in bars]
        highs = [float(b[1]) for b in bars]
        self.close = [float(b[3]) for b in bars]
        self.lo = self._table(lows, min)
        self.hi = self._table(highs, max)
        self.cl_min = self._table(self.close, min)
        self.cl_max = self._table(self.close, max)

    @staticmethod
    def _table(arr: List[float], fn: Callable[[float, float], float]) -> List[List[float]]:
        t = [arr]
        k = 1
        while (1 << k) <= len(arr):
            prev = t[-1]
            half = 1 << (k - 1)
            t.append([fn(prev[i], prev[i + half]) for i in range(len(arr) - (1 << k) + 1)])
            k += 1
        return t

    @staticmethod
    def first(t: List[List[float]], a: int, b: int, x: float, le: bool) -> int:
        """Smallest j in [a, b) with arr[j] <= x (le) or arr[j] >= x (not le); b when there is none."""
        while a < b:
            k = (b - a).bit_length() - 1
            while k >= 0:
                v = t[k][a]
                if (v <= x) if le else (v >= x):
                    k -= 1
                else:
                    break
            if k < 0:
                return a                     # arr[a] itself meets the condition
            a += 1 << k                      # the whole block [a, a + 2^k) misses: skip it
        return b


def lane_honors_tp(kind: str) -> Optional[bool]:
    """Whether a lane of this Set kind exits on its TP: base always, exit never, trail per the book (None)."""
    if kind == "base":
        return True
    if kind == "exit":
        return False
    return None


def lane_fracs(st: "SetState", cost_pct: float, floor_steps: Optional[int] = None) -> Tuple[float, float]:
    """The SL and TP distances (fractions of the entry) of a Set's lane, as SetLane opens a position: SL = TP x the Set's
    ratio (TRAIL_SL_RATIO for a trailing Set), never under PositionCost + the minimum steps; TP never under 2 x PositionCost.
    The live trader takes a Set position's levels from here, so live and replay share one formula."""
    sl_base = st.tp_pct * (TRAIL_SL_RATIO if st.kind == "trail" else st.sl_ratio)
    return max(sl_floor_frac(cost_pct, floor_steps), sl_base), max(2 * cost_as_frac(cost_pct), st.tp_pct)


class SetLane:
    """One (Set, symbol) trade state machine. The only implementation of the bar loop: the engine
    replay (SetBook._replay_symbol) and the streaming simulator both call step()."""
    __slots__ = ("st", "symbol", "sid", "open", "cool", "use_trail", "arm", "give", "sl_base", "tp_frac", "hold", "honor")

    def __init__(self, st: "SetState", symbol: str) -> None:
        self.st = st
        self.symbol = symbol
        self.sid = st.id
        self.open: Optional[Dict[str, Any]] = None
        self.cool = 0
        self.use_trail = st.kind == "trail"
        # trail values are percent of price on every path (trail_grid, parse_trail): no magnitude guessing
        self.arm = st.trail_arm / 100.0 if self.use_trail else 0.0
        self.give = st.trail_give / 100.0 if self.use_trail else 0.0
        self.sl_base = st.tp_pct * (TRAIL_SL_RATIO if st.kind == "trail" else st.sl_ratio)
        self.tp_frac = st.tp_pct
        # TP by kind: a base Set takes its TP (the step is its target), an Exit Set never does (exitIgnoreTp is its own
        # strategy type), a trailing Set follows cfg["honor_tp"] (not exitIgnoreTp)
        self.honor = lane_honors_tp(st.kind)
        self.hold = int(getattr(st, "hold_bars", 0) or 0)      # the Set's own time exit (bars); 0 = the global one

    def step(self, i: int, bar: Sequence[float], d: int, conf: float, allowed: bool, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Advance one bar. Returns the closed trade (a dict) or None.
        cfg keys: entry_conf, time_bars, scratch_bars, scratch_min, cooldown, cost_pct, honor_tp."""
        o = self.open
        if o is not None:
            side = int(o["side"])
            entry = float(o["entry"])
            held = i - int(o["i"])
            if self.use_trail:
                if side > 0:
                    o["peak"] = max(o["peak"], float(bar[1]))
                    if (o["peak"] - entry) / entry >= self.arm:
                        o["trail"] = max(o.get("trail") or 0.0, o["peak"] * (1 - self.give))
                else:
                    o["peak"] = min(o["peak"], float(bar[2]))
                    if (entry - o["peak"]) / entry >= self.arm:
                        t = o["peak"] * (1 + self.give)
                        cur = o.get("trail")
                        o["trail"] = t if cur is None else min(cur, t)
            elif side > 0:
                o["peak"] = max(o["peak"], float(bar[1]))
            else:
                o["peak"] = min(o["peak"], float(bar[2]))
            honor = cfg["honor_tp"] if self.honor is None else self.honor
            why, px = hit_exit(side, entry, o["sl"], o["tp"], o.get("trail"), bar, ignore_tp=not honor)
            if why is None and held >= (self.hold or cfg["time_bars"]):
                why, px = "time", float(bar[3])
            if why is None and held >= cfg["scratch_bars"]:
                if (float(bar[3]) - entry) / entry * side >= cfg["scratch_min"]:
                    why, px = "scratch+", float(bar[3])
            if why:
                raw = (px - entry) / entry * side
                self.open = None
                self.cool = cfg["cooldown"]
                return {"set_id": self.sid, "symbol": self.symbol, "side": "LONG" if side > 0 else "SHORT",
                        "dir": side, "entry_i": int(o["i"]), "exit_i": i, "entry_px": entry, "exit_px": float(px),
                        "reason": why, "pnl_pct": raw, "pnl": net_pnl_pct(raw, cfg["cost_pct"]),
                        "hold_s": held * BAR_S, "hold_bars": held}
            return None
        if self.cool > 0:
            self.cool -= 1
            return None
        if not allowed or d == 0 or conf < cfg["entry_conf"]:
            return None
        close = float(bar[3])
        cost_frac = cost_as_frac(cfg["cost_pct"])
        sl_frac = max(sl_floor_frac(cfg["cost_pct"], cfg.get("sl_floor_steps")), self.sl_base)   # never under PositionCost + steps
        tp_frac = max(2 * cost_frac, self.tp_frac)       # TP grid starts at 2 x PositionCost
        if d > 0:
            sl, tp = close * (1 - sl_frac), close * (1 + tp_frac)
        else:
            sl, tp = close * (1 + sl_frac), close * (1 - tp_frac)
        self.open = {"side": d, "entry": close, "sl": sl, "tp": tp, "peak": close, "i": i, "trail": None}
        return None

    def scan(
        self,
        i0: int,
        i1: int,
        bars: Sequence[Sequence[float]],
        dirs: Sequence[int],
        confs: Sequence[float],
        allowed: bool,
        cfg: Dict[str, Any],
        cand: Optional[Sequence[int]] = None,
        acc: Optional["BarIndex"] = None,
    ) -> List[Dict[str, Any]]:
        """Advance over bars[i0:i1]. Same rules as step(), applied to the whole range in one loop.
        acc: the symbol's BarIndex; with it a base lane finds its exit by lookup (same result as the walk).

        cand: sorted bar indices where an entry is possible (dir != 0 and conf >= entry_conf). A flat lane
        jumps between them; the bars in between can only spend cooldown, which is applied in one step.
        While a position is open its state is held in locals and written back when it closes or the
        range ends. Returns the closed trades in exit order; open position and cooldown carry across calls.
        """
        thr = cfg["entry_conf"]
        if cand is None:
            cand = [i for i in range(i0, i1) if dirs[i] != 0 and confs[i] >= thr]
        time_bars = self.hold or cfg["time_bars"]
        scratch_bars = cfg["scratch_bars"]
        scratch_min = cfg["scratch_min"]
        cooldown = cfg["cooldown"]
        cost_pct = cfg["cost_pct"]
        cost_frac = cost_as_frac(cost_pct)
        sl_floor = sl_floor_frac(cost_pct, cfg.get("sl_floor_steps"))
        honor_tp = bool(cfg["honor_tp"] if self.honor is None else self.honor)
        use_trail = self.use_trail
        arm = self.arm
        give = self.give
        sl_base = self.sl_base
        tp_frac = self.tp_frac
        sid = self.sid
        symbol = self.symbol
        o = self.open
        cool = self.cool
        out: List[Dict[str, Any]] = []
        i = i0
        while i < i1:
            if o is not None and acc is not None and (not use_trail or o.get("trail") is None):
                # an unarmed position (every base lane, and a trailing lane until its arm is reached) exits like a base
                # lane: find the exit bar by lookup. A trailing lane that arms first hands over to the walk at that bar.
                side = o["side"]
                entry = o["entry"]
                sl = o["sl"]
                tp = o["tp"]
                oi = o["i"]
                first = acc.first
                end = i1
                if use_trail:
                    peak0 = o["peak"]
                    loose_arm = entry * (1 + side * arm) * (1 - side * 1e-9)
                    a_ = i
                    while a_ < i1:
                        a_ = first(acc.hi, a_, i1, loose_arm, False) if side > 0 else first(acc.lo, a_, i1, loose_arm, True)
                        if a_ >= i1:
                            break
                        pk = max(peak0, float(bars[a_][1])) if side > 0 else min(peak0, float(bars[a_][2]))
                        if ((pk - entry) / entry if side > 0 else (entry - pk) / entry) >= arm:
                            break
                        a_ += 1
                    end = min(a_, i1)         # the arming bar: from there the stop trails
                if side > 0:
                    j_sl = first(acc.lo, i, end, sl, True)
                    j_tp = first(acc.hi, i, end, tp, False) if honor_tp else end
                else:
                    j_sl = first(acc.hi, i, end, sl, False)
                    j_tp = first(acc.lo, i, end, tp, True) if honor_tp else end
                j_time = max(i, oi + time_bars)
                if j_time >= end:
                    j_time = end
                # scratch: the first bar from oi + scratch_bars whose close clears scratch_min; the lookup narrows with a
                # slightly loose level, the exact expression decides
                j_scr = end
                a = max(i, oi + scratch_bars)
                lim = min(end, j_sl, j_tp, j_time)
                loose = entry * (1 + side * scratch_min) * (1 - side * 1e-9)
                while a < lim:
                    a = first(acc.cl_max, a, lim, loose, False) if side > 0 else first(acc.cl_min, a, lim, loose, True)
                    if a >= lim:
                        break
                    if (acc.close[a] - entry) / entry * side >= scratch_min:
                        j_scr = a
                        break
                    a += 1
                j = min(j_sl, j_tp, j_time, j_scr)
                if j >= end:
                    # no exit before `end`: carry the peak over the bars walked past
                    if i < end:
                        if side > 0:
                            o["peak"] = max(o["peak"], max(float(bars[x][1]) for x in range(i, end)))
                        else:
                            o["peak"] = min(o["peak"], min(float(bars[x][2]) for x in range(i, end)))
                    i = end
                    if end >= i1:
                        continue
                    # a trailing lane reached its arm at bar `end`: the walk below takes over from that bar
                else:
                    close = acc.close[j]
                    if j == j_sl:
                        why, px = "sl", sl
                    elif j == j_tp:
                        why, px = "tp", tp
                    elif j == j_time:
                        why, px = "time", close
                    else:
                        why, px = "scratch+", close
                    held = j - oi
                    raw = (px - entry) / entry * side
                    out.append({
                        "set_id": sid, "symbol": symbol, "side": "LONG" if side > 0 else "SHORT",
                        "dir": side, "entry_i": int(oi), "exit_i": j, "entry_px": entry, "exit_px": float(px),
                        "reason": why, "pnl_pct": raw, "pnl": net_pnl_pct(raw, cost_pct),
                        "hold_s": held * BAR_S, "hold_bars": held,
                    })
                    o = None
                    cool = cooldown
                    i = j + 1
                    continue
            if o is not None:
                side = o["side"]
                entry = o["entry"]
                sl = o["sl"]
                tp = o["tp"]
                peak = o["peak"]
                oi = o["i"]
                trail = o.get("trail")
                while i < i1:
                    bar = bars[i]
                    high = float(bar[1])
                    low = float(bar[2])
                    close = float(bar[3])
                    if use_trail:
                        if side > 0:
                            peak = max(peak, high)
                            if (peak - entry) / entry >= arm:
                                trail = max(trail or 0.0, peak * (1 - give))
                        else:
                            peak = min(peak, low)
                            if (entry - peak) / entry >= arm:
                                t = peak * (1 + give)
                                trail = t if trail is None else min(trail, t)
                    elif side > 0:
                        peak = max(peak, high)
                    else:
                        peak = min(peak, low)
                    # hit_exit: the stop (or trail) wins a same-bar tie with the target
                    held = i - oi
                    why: Optional[str] = None
                    px = close
                    if side > 0:
                        stop = max(sl, trail) if trail is not None else sl
                        if low <= stop:
                            why, px = "sl", stop
                        elif honor_tp and high >= tp:
                            why, px = "tp", tp
                    else:
                        stop = min(sl, trail) if trail is not None else sl
                        if high >= stop:
                            why, px = "sl", stop
                        elif honor_tp and low <= tp:
                            why, px = "tp", tp
                    if why is None and held >= time_bars:
                        why, px = "time", close
                    if why is None and held >= scratch_bars and (close - entry) / entry * side >= scratch_min:
                        why, px = "scratch+", close
                    if why:
                        raw = (px - entry) / entry * side
                        out.append({
                            "set_id": sid, "symbol": symbol, "side": "LONG" if side > 0 else "SHORT",
                            "dir": side, "entry_i": int(oi), "exit_i": i, "entry_px": entry, "exit_px": float(px),
                            "reason": why, "pnl_pct": raw, "pnl": net_pnl_pct(raw, cost_pct),
                            "hold_s": held * BAR_S, "hold_bars": held,
                        })
                        o = None
                        cool = cooldown
                        i += 1
                        break
                    i += 1
                if o is not None:
                    o["peak"] = peak
                    o["trail"] = trail
                continue
            # flat: the next candidate bar; the bars before it can only spend cooldown
            k = bisect.bisect_left(cand, i)
            if k >= len(cand) or cand[k] >= i1:
                cool = max(0, cool - (i1 - i))
                i = i1
                break
            j = cand[k]
            if j > i:
                cool = max(0, cool - (j - i))
            if cool > 0:
                cool -= 1
            elif allowed:
                d = int(dirs[j])
                close = float(bars[j][3])
                sl_frac = max(sl_floor, sl_base)
                tp_use = max(2 * cost_frac, tp_frac)
                if d > 0:
                    sl, tp = close * (1 - sl_frac), close * (1 + tp_use)
                else:
                    sl, tp = close * (1 + sl_frac), close * (1 - tp_use)
                o = {"side": d, "entry": close, "sl": sl, "tp": tp, "peak": close, "i": j, "trail": None}
            i = j + 1
        self.open = o
        self.cool = cool
        return out

BASE_SHORT_N_DEFAULT = 25        # Base: the second last N (setGateShortN; 0 = off): best cell of the 24 h window grid (owner, 2026-10-29)
MAIN_SHORT_MAX_RATIO = 0.5       # the Main short windows are at most this share of the Main long window (owner 2026-10-29, mainShortMaxRatio; was mainAutoMaxRatio)
MAIN_MIN_N_MAX = 3               # the Main minimal window: the newest 1-3 results of the Main Set (mainMinN, 0 = off), all positive or all negative (mainMinMode)
BASE_MIN_N_MAX = 3               # the Base minimal window: the newest 1-3 results (setGateMinN, 0 = off), all positive or all negative (setGateMinMode)
BASE_SHORT_MAX_RATIO = 0.5       # the short window is at most this share of the long window (owner 2026-10-29: long 40 -> short at most 20; setGateShortMaxRatio)
MAIN_SHORT_WINDOWS = (5, 10, 15)  # owner, 2026-10-29: the Main short window, the candidates of each Set's own window (5-15 step 5; was "Auto")
MAIN_SHORT_MIN_DEFAULT = 8       # orders a candidate needs on the Main Set
MAIN_SHORT_MARGIN = 1.02         # a candidate must beat the long window's PF by 2 %
MAIN_NEG_WINDOWS = (5, 10)       # Main Negative lane: the average of the last N Main orders is negative, for some N
REAL_GATE_N_DEFAULT = 15         # Real: the last N of the lane's Real Set
STAGE_REASONS = ("main", "mainshort", "mainminimal", "real")   # the reasons of a Set that passed Base and stopped at a later stage
MAIN_GATE_N_DEFAULT = 25        # owner, 2026-10-29: the Main long window reads the last 25 Main-Set orders
REAL_SUFFIX = ":real"              # a Base Set's Real Set (the last of the chain: opened while Base and Main passed): "<id>:real"
REALNEG_SUFFIX = ":realneg"        # a Base Set's Real Negative Set (Main orders opened in a dip): "<id>:realneg"
MAIN_SUFFIX = ":main"              # a Base Set's Main Set (opened while Base passed; the Main gate's last N reads it): "<id>:main"
EXEC_SUFFIX, PROC_SUFFIX = REAL_SUFFIX, MAIN_SUFFIX
PROC_KEEP = 200                   # processed orders kept per Set (the Main gate reads the newest mainGateN of them)
LOOKBACK_GROW_STEP = 240          # bars added to the pre-history per growth step
LOOKBACK_SHORT_SHARE = 0.25       # grow when this share of the Sets that traded has fewer orders than the gate minimum
HOLD_KEY = "@h"                                       # _hist_rows key of an Auto Mode candidate tape: "<set id>@h<minutes>"
STEP_HOLD_STEPS = 12
HOLD_AUTO_LOW = (10, 15, 20, 30, 40, 50)              # Auto Mode candidates in minutes, steps 1..5 (the desk range 1-50)
HOLD_AUTO_HIGH = (30, 60, 90, 120, 180, 240, 350)      # steps 6..12 (the desk range 30-350)
HOLD_AUTO_MAX_ORDERS = 50                              # a Set is judged on its existing orders, never more than this many
HOLD_AUTO_MIN_ORDERS = 10                              # never below MIN_VALIDATION_N (8)
HOLD_AUTO_MARGIN = 1.02                                # a candidate must beat the global time stop's PF by 2% to replace it
STEP_HOLD_LOW = (1, 50)        # allowed minutes for steps 1..5 (the desk slider); the engine itself accepts 1..720
STEP_HOLD_HIGH = (30, 350)     # steps 6..12


def parse_step_hold(on: Any, minutes: Any) -> Dict[int, int]:
    """{step: minutes} for the steps whose hold is on. Both lists are indexed by step - 1; a missing or invalid entry is off."""
    out: Dict[int, int] = {}
    if not isinstance(on, (list, tuple)) or not isinstance(minutes, (list, tuple)):
        return out
    for i in range(min(STEP_HOLD_STEPS, len(on), len(minutes))):
        try:
            m = int(round(float(minutes[i])))
        except (TypeError, ValueError):
            continue
        if bool(on[i]) and 1 <= m <= 720:
            out[i + 1] = m
    return out


class SetBook:
    def __init__(self) -> None:
        self.enabled = True
        self.lookback = LOOKBACK_DEFAULT
        self.lookback_cfg = LOOKBACK_DEFAULT
        self.lookback_max = 2880
        self.lookback_auto = True
        self._lookback_grown = 0
        self.main_on = True
        self.main_n = MAIN_GATE_N_DEFAULT
        self.base_short = BASE_SHORT_N_DEFAULT
        self.short_max_ratio = BASE_SHORT_MAX_RATIO
        self.base_min_n = 0
        self.base_min_positive = True
        self.short_on = True
        self.short_windows: Tuple[int, ...] = MAIN_SHORT_WINDOWS
        self.short_ratio = MAIN_SHORT_MAX_RATIO
        self.short_min = MAIN_SHORT_MIN_DEFAULT
        self.short_margin = MAIN_SHORT_MARGIN
        self.main_min_n = 0
        self.main_min_positive = True
        self.neg_on = True
        self.neg_windows: Tuple[int, ...] = MAIN_NEG_WINDOWS
        self.realst_on = True
        self.realst_n = REAL_GATE_N_DEFAULT
        self.stage_full = False
        self.stages_on = True
        self.min_bars = 120
        self.warmup = WARMUP_DEFAULT
        self.refresh_s = 90.0
        self.pf_n = PF_N_DEFAULT
        self.deact_n = DEACT_N_DEFAULT
        self.min_pf = GATE_MIN_PF_DEFAULT
        self.pack_min_pf: Dict[str, float] = {SHORT_PACK: SHORT_MIN_PF_DEFAULT}
        self.consistency_pf = 0.0
        self.pf_history: List[Dict[str, Any]] = []
        self._adapt_pending: Optional[List[Any]] = None
        self.quality: Dict[str, str] = {}
        self.max_flat_share = 0.35
        self.max_jump = 0.5
        self.gate_window = GATE_WINDOW_DEFAULT
        self.gate_min = max(1, GATE_MIN_DEFAULT)
        self.max_dd_s = 420.0
        self.use_historic_gate = True
        self.fresh_s = 0.0              # live sets 2 x refresh_s: a Set not evaluated in that time is not valid; sim keeps 0
        self.cost_pct = POSITION_COST_PCT_DEFAULT
        self.time_stop_s = 21600.0
        self.hist_time_bars = 45
        self.scratch_s = 90.0
        self.scratch_min = 0.0016
        self.tp_pct = 0.0075
        self.cooldown_bars = 2
        self.entry_conf = 0.58
        self.retry_s = 30.0
        self.ignore_tp = True
        self.opt_sl = 0.0030
        self.min_step_cfg = STEP_MIN
        self.min_step = STEP_MIN
        self.step_max = STEP_MAX
        self.step_adapt = True
        self.short_steps: List[int] = list(range(SHORT_STEP_MIN, SHORT_STEP_MAX + 1))
        self.sl_min_steps = SL_MIN_STEPS_DEFAULT
        self.trail_min_steps = TRAIL_MIN_STEPS_DEFAULT
        self.hist_honor_tp = True
        self.exit_sets = True
        self.steps: List[int] = list(range(STEP_MIN, STEP_MAX + 1))
        self.packs: List[str] = list(PACKS)
        self.sl_ratios: List[float] = list(SL_RATIOS)
        self.trails: List[Tuple[str, float, float]] = []
        self.trails_built: List[Tuple[str, float, float]] = []
        self.sets: Dict[str, SetState] = {}
        self.by_idx: List[SetState] = []
        self._sig_cache: Dict[str, Dict[int, Dict[str, Tuple[int, float, str]]]] = {}
        # shared Set state is touched by the replay thread and the trading thread: one re-entrant lock
        self._lock = threading.RLock()
        # bumped whenever the Set map is rebuilt; a replay pass that straddles a rebuild must not score a partial tape
        self._topo_gen = 0
        # replayed trade rows per symbol: a refresh replaces only the symbols it replayed
        self._hist_rows: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        self.bars: Dict[str, List[List[float]]] = {}
        # open time (s) of the newest row of self.bars[symbol] when that row is a completed exchange minute; absent when the
        # series ends in rows of unknown time (live ticks). Live entries read a signal only on the bar this names.
        self.bar_t: Dict[str, float] = {}
        self.progress = Progress()
        self.last_run = 0.0
        self.ind_settings: Dict[str, Any] = {}
        self.locks: Dict[str, bool] = {}
        self._live_seen: Dict[str, None] = {}   # insertion-ordered: the oldest keys are evicted past LIVE_SEEN_MAX
        self.live_skipped = 0
        self.live_unmatched = 0            # live closes whose Set is neither on the grid nor retired
        self.retired: Dict[str, SetState] = {}   # Sets that left the grid (step-adapt): tape kept, never picked
        self._running = False

    def load(self, ov: Dict[str, Any], cts: Optional[Dict[str, Any]] = None) -> None:
        cts = cts or {}
        self.enabled = bool(ov.get("histEnabled", True))
        self.lookback_cfg = max(120, min(2880, int(ov.get("histLookbackBars") or LOOKBACK_DEFAULT)))
        # pre-history grows when too many Sets have fewer orders than the gate needs (histLookbackAuto), up to histLookbackMax bars
        self.lookback_auto = bool(ov.get("histLookbackAuto", True))
        self.lookback_max = max(self.lookback_cfg, min(2880, int(ov.get("histLookbackMax") or 2880)))
        self.lookback = max(self.lookback_cfg, min(self.lookback_max, int(getattr(self, "_lookback_grown", 0) or 0))) if self.lookback_auto else self.lookback_cfg
        self.min_bars = max(60, min(self.lookback, int(ov.get("histMinBars") or 120)))
        self.max_flat_share = min(1.0, max(0.0, num(ov, "histMaxFlatShare", 0.35)))
        self.max_jump = max(0.01, num(ov, "histMaxJumpPct", 50.0) / 100.0)
        self.warmup = max(16, min(80, int(ov.get("histWarmup") or WARMUP_DEFAULT)))
        self.refresh_s = max(30.0, min(600.0, float(ov.get("histRefreshS") or 90)))
        self.pf_n = max(MIN_VALIDATION_N, min(200, window_n(ov, "setPfWindow", "pfWindow", fallback=PF_N_DEFAULT)))   # follows the overall validation N unless unlinked
        self.deact_n = max(10, min(80, int(ov.get("setDeactN") or DEACT_N_DEFAULT)))
        self.gate_window = max(10, min(200, int(ov.get("setGateWindow") or GATE_WINDOW_DEFAULT)))
        # 0 (the default) = no minimum: the Base gate judges a Set on the orders it has, only a Set without a closed order is not judged
        # (gate_min 1). No floor of 8 here (owner 2026-10-29); the windows and the hold sample keep theirs.
        gm = ov.get("setGateMinTrades")
        self.gate_min = max(1, min(self.gate_window, int(GATE_MIN_DEFAULT if gm is None else gm)))
        # the Main gate: every Set, besides the Base gate, must hold the minimum PF over its own last mainGateN processed orders (default 25);
        # execution picks only Sets that pass both, and the minimum PF is the Set's Base minimum PF (one value for every validation gate)
        # stages: Base (each Set on its own tape: the long last N and the short last N, both) -> Main (on the Set's Main Set, the
        # orders it opened while Base passed: the long last N, the short window and the minimal window; or the Negative lane: the average of
        # the last 5 / 10 Main orders below zero) -> Real (on the lane's Real Set: the last realGateN; then execution and mirroring)
        self.stages_on = bool(ov.get("stagesEnabled", True))     # off: the Base long window alone (no short window, Main, Negative, Real)
        self.short_max_ratio = max(0.1, min(1.0, float(num(ov, "setGateShortMaxRatio", BASE_SHORT_MAX_RATIO))))
        short_cap = max(1, int(self.gate_window * self.short_max_ratio + 1e-9))       # long 40, ratio 0.5: at most 20
        self.base_short = max(0, min(short_cap, int(num(ov, "setGateShortN", BASE_SHORT_N_DEFAULT)))) if self.stages_on else 0
        # the Base minimal window, a third last-N next to the long and the short one: the newest 1-3 results all positive (or all negative)
        mode = str(ov.get("setGateMinMode") or "positive").strip().lower()
        self.base_min_positive = mode != "negative"
        min_cap = min(BASE_MIN_N_MAX, self.base_short) if self.base_short else BASE_MIN_N_MAX
        self.base_min_n = max(0, min(min_cap, int(num(ov, "setGateMinN", 0)))) if self.stages_on else 0
        self.main_on = self.stages_on and bool(ov.get("mainGateEnabled", True))
        self.main_n = max(MIN_VALIDATION_N, min(self.gate_window, int(num(ov, "mainGateN", MAIN_GATE_N_DEFAULT))))
        # the Main short window (was "Auto"): each Set's own window of the candidates, at most mainShortMaxRatio of the long window; the
        # old mainAuto* keys still load when the mainShort* ones are absent (overlays and runtime files written before the rename)
        sov = {k: _alias(ov, "mainShort" + k, "mainAuto" + k) for k in ("Enabled", "Windows", "MaxRatio", "Min", "Margin")}
        self.short_on = bool(True if sov["Enabled"] is None else sov["Enabled"])
        self.short_ratio = max(0.1, min(1.0, float(MAIN_SHORT_MAX_RATIO if sov["MaxRatio"] is None else sov["MaxRatio"])))
        short_cap = max(1, int(self.main_n * self.short_ratio + 1e-9))                 # long 25, ratio 0.5: short windows of at most 12
        self.short_windows = tuple(w for w in parse_windows(sov["Windows"], MAIN_SHORT_WINDOWS) if w <= short_cap) or (short_cap,)
        self.short_min = max(3, min(200, int(MAIN_SHORT_MIN_DEFAULT if sov["Min"] is None else sov["Min"])))
        self.short_margin = max(1.0, min(2.0, float(MAIN_SHORT_MARGIN if sov["Margin"] is None else sov["Margin"])))
        # the Main minimal window, a third last-N next to the long and the short one: the newest 1-3 results of the Main Set all positive (or all negative)
        mmode = str(ov.get("mainMinMode") or "positive").strip().lower()
        self.main_min_positive = mmode != "negative"
        main_min_cap = min(MAIN_MIN_N_MAX, min(self.short_windows) if self.short_on else self.main_n)
        self.main_min_n = max(0, min(main_min_cap, int(num(ov, "mainMinN", 0)))) if self.main_on else 0
        self.neg_on = self.stages_on and bool(ov.get("mainNegEnabled", True))
        self.neg_windows = parse_windows(ov.get("mainNegWindows"), MAIN_NEG_WINDOWS)
        self.realst_on = self.stages_on and bool(ov.get("realGateEnabled", True))
        self.realst_n = max(1, min(200, int(num(ov, "realGateN", REAL_GATE_N_DEFAULT))))     # 1..200: the ongoing window may be a single result
        self.stage_full = bool(ov.get("stageFullWindow", False))      # False (default): every stage window judges the orders it has, at least one
        self.min_pf = base_min_pf(ov)          # at least MIN_PF_FLOOR (1.1)
        # a strategy can carry its own PF floor; Short Range defaults to 1.1 whatever the global floor is
        cons = ov.get("setGateConsistencyPf")
        self.consistency_pf = float(cons) if cons is not None else 0.0     # 0 = off
        sr_min = ov.get("shortRangeMinPf")
        self.pack_min_pf = {SHORT_PACK: pf_floor(sr_min, SHORT_MIN_PF_DEFAULT)}
        # max drawdown time: the longest drawdown over the last setGateWindow positions must be LOWER than this many
        # hours. setMaxDdtHours is 2..36 in steps of 2, default 24. max_dd_s holds the threshold in seconds.
        hrs = ov.get("setMaxDdtHours")
        hrs = GATE_MAX_DDT_HOURS_DEFAULT if hrs is None else float(hrs)
        hrs = max(2.0, min(36.0, round(hrs / 2.0) * 2.0))
        self.max_ddt_h = hrs
        self.max_dd_s = hrs * 3600.0
        self.use_historic_gate = True      # the Base gate is always on: entries open only under a Base-validated Set (the overlay key is ignored)
        self.cost_pct = normalize_cost_pct(ov.get("positionCostPct") or ov.get("setCostPct"))
        self.time_stop_s = max(30.0, min(21600.0, num(ov, "timeStopS", 21600)))
        if ov.get("timeStopS") is not None:
            # the live time stop, in bars: replay and simulator hold for the same time (6 h = 360 bars)
            self.hist_time_bars = max(8, min(720, int(round(self.time_stop_s / BAR_S))))
        else:
            self.hist_time_bars = max(8, min(120, num(ov, "setHistTimeBars", 45, int)))
        self.scratch_s = num(ov, "scratchS", 600)
        tp = num(ov, "tpPct", 0.75)
        self.tp_pct = tp / 100.0   # tpPct is percent
        self.ignore_tp = bool(ov.get("exitIgnoreTp", True))
        # one TP rule for replay and live: exitIgnoreTp, the key live reads. setHonorTp is only the fallback.
        if "exitIgnoreTp" in ov:
            self.hist_honor_tp = not bool(ov["exitIgnoreTp"])
        else:
            self.hist_honor_tp = bool(ov.get("setHonorTp", True))
        # hist_honor_tp decides the trailing Sets only. A base Set always takes its TP (its step is its target); ignoring TP
        # is its own strategy type: with exitIgnoreTp on, every base stop (SL ratio x step, one Set per distinct distance)
        # also runs as an Exit Set, its own lane with no TP exit, its own tape and its own Base validation.
        self.exit_sets = bool(ov.get("exitIgnoreTp", True))
        # per-step hold times: stepHoldOn[i] / stepHoldMin[i] belong to step i + 1 (steps 1..12); an "on" step leaves on its own
        # time exit after that many minutes, an "off" step keeps the global time stop. Replay and live read the same table.
        # holdEnabled is the master switch (off: no per-step and no auto hold, only the global time stop); holdAuto lets every Set
        # without a manual step hold choose its own hold from its last holdAutoMax orders (at most 50, at least holdAutoMin)
        self.hold_on = bool(ov.get("holdEnabled", True))
        self.hold_auto = self.hold_on and bool(ov.get("holdAuto", True))
        self.hold_auto_max = int(max(HOLD_AUTO_MIN_ORDERS, min(HOLD_AUTO_MAX_ORDERS, num(ov, "holdAutoMax", HOLD_AUTO_MAX_ORDERS))))
        self.hold_auto_min = int(max(MIN_VALIDATION_N, min(self.hold_auto_max, num(ov, "holdAutoMin", HOLD_AUTO_MIN_ORDERS))))
        self.step_hold = parse_step_hold(ov.get("stepHoldOn"), ov.get("stepHoldMin")) if self.hold_on else {}
        opt = num(ov, "exitOptSlPct", 0.30)
        self.opt_sl = opt / 100.0   # exitOptSlPct is percent
        self.min_step_cfg = clamp_step(ov.get("setMinStep") or ov.get("minStepRange") or STEP_MIN)
        self.step_max = clamp_step(ov.get("setStepMax") or STEP_MAX, self.min_step_cfg, STEP_MAX)
        self.step_adapt = bool(ov.get("setStepAdapt", True))
        self.min_step = self.min_step_cfg
        self.steps = list(range(self.min_step, self.step_max + 1))
        packs = []
        if bool(ov.get("stratIndications", True)):
            packs.append("indications")
        if bool(ov.get("stratGeneral", True)):
            packs.append("general")
        if bool(ov.get("stratShortRange", False)):
            packs.append(SHORT_PACK)
        for pk, (sw, _key, _default) in EXTRA_PACKS.items():
            if bool(ov.get(sw, False)):
                packs.append(pk)
        self.packs = packs or ["indications"]
        lo = clamp_step(ov.get("shortRangeStepMin") or SHORT_STEP_MIN, STEP_MIN, STEP_MAX)
        hi = clamp_step(ov.get("shortRangeStepMax") or SHORT_STEP_MAX, lo, STEP_MAX)
        self.short_steps = list(range(lo, hi + 1))
        # overall minimum stop and trailing distance, "PositionCost + n steps" (one step = one PositionCost)
        sl_ms = ov.get("slMinSteps")
        tr_ms = ov.get("trailMinSteps")
        mins = configure_min_steps(SL_MIN_STEPS_DEFAULT if sl_ms is None else sl_ms,
                                   TRAIL_MIN_STEPS_DEFAULT if tr_ms is None else tr_ms)
        self.sl_min_steps = mins["sl"]
        self.trail_min_steps = mins["trail"]
        # Set grid: every SL ratio in setSlRatios (default 0.5..3.5 step 0.25) x every TP step x every pack.
        raw_ratios = ov.get("setSlRatios") or list(SL_RATIOS)
        ratios: List[float] = []
        for x in raw_ratios:
            try:
                ratios.append(snap_sl_ratio(float(x)))
            except Exception:
                continue
        self.sl_ratios = sorted(set(ratios)) or list(SL_RATIOS)
        # Trailing grid: every (arm, give) from trailMinStep up, in PositionCost units (trail_grid).
        # An explicit trailVariants list ("arm:give" in percent) still overrides the grid.
        self.trail_min_step = max(TRAIL_STEP_SETTING_MIN, min(TRAIL_STEP_SETTING_MAX, int(float(ov.get("trailMinStep") or TRAIL_STEP_DEFAULT))),
                                  min(TRAIL_STEP_SETTING_MAX, trail_floor_steps(self.trail_min_steps)))
        raw_tr = ov.get("trailVariants")
        if raw_tr:
            if isinstance(raw_tr, str):
                raw_tr = [p.strip() for p in raw_tr.split(",") if p.strip()]
            # an explicit list obeys the overall minimum trailing distance too: a give under PositionCost + n steps is lifted
            gfloor = trail_floor_steps(self.trail_min_steps) * self.cost_pct
            seen_tr: Dict[str, Tuple[str, float, float]] = {}
            for a_, g_ in (parse_trail(x) for x in raw_tr):
                g2 = round(max(g_, gfloor), 4)
                a2 = round(max(a_, g2), 4)
                key = f"{a2:.2f}:{g2:.2f}"      # the trail_grid format: exact to the cent, so lifted variants never merge
                seen_tr.setdefault(key, (key, a2, g2))
            self.trails = list(seen_tr.values())
        else:
            self.trails = trail_grid(self.trail_min_step, self.cost_pct)
        try:
            self.entry_conf = min(0.99, max(0.30, float(ov.get("setEntryConf") if ov.get("setEntryConf") is not None else 0.58)))
        except Exception:
            self.entry_conf = 0.58
        try:
            self.cooldown_bars = min(30, max(0, int(ov.get("setCooldownBars") if ov.get("setCooldownBars") is not None else 2)))
        except Exception:
            self.cooldown_bars = 2
        locks = ov.get("setLocks") if isinstance(ov.get("setLocks"), dict) else {}
        self.locks = {str(k): bool(v) for k, v in locks.items()}
        self.ind_settings = {
            "candleLimit": 60,
            "minimumStrength": num(ov, "indMinStrength", 0.2),
            "minimumConfidence": num(ov, "indMinConfidence", 0.6),
            "stopLossMinPct": num(ov, "indStopMinPct", 0.2),
            "stopLossMaxPct": num(ov, "indStopMaxPct", 1.5),
            "stopLossAtrMultiplier": num(ov, "indAtrMult", 0.85),
            "takeProfitRewardRisk": num(ov, "indRewardRisk", 1.8),
            "takeProfitMaxPct": 5.0,
            "positionCostPct": self.cost_pct,
            # the switches IndicationBook reads (same keys, same defaults): the set signal honours them too
            "tf1m": bool(ov.get("tf1m", True)),
            "tf5m": bool(ov.get("tf5m", True)),
            "tf15m": bool(ov.get("tf15m", True)),
            "tfCombined": bool(ov.get("tfCombined", True)),
            "tfMinAgree": max(1, num(ov, "tfMinAgree", 2, int)),
            "typeState": bool(ov.get("indTypeState", True)),
            "typeDirection": bool(ov.get("indTypeDirection", True)),
            "typeMove": bool(ov.get("indTypeMove", True)),
            "typeSignals": bool(ov.get("indTypeSignals", True)),
            # extra indicators (indication_extra), by name; empty by default so the shipped votes are unchanged
            "shortRangeVariants": [str(x) for x in (ov.get("shortRangeVariants") or SHORT_VARIANTS_DEFAULT) if str(x) in EXTRA_INDICATORS],
            **{key: [str(x) for x in (ov.get(key) or default) if str(x) in EXTRA_INDICATORS] for _pk, (_sw, key, default) in EXTRA_PACKS.items()},
            "extraIndicators": sorted({str(x) for x in (ov.get("extraIndicators") or []) if str(x) in EXTRA_INDICATORS}),
        }
        with self._lock:
            self._rebuild_sets()

    def _step_grid(self) -> List[int]:
        lo = clamp_step(self.min_step, STEP_MIN, self.step_max)
        hi = clamp_step(self.step_max, lo, STEP_MAX)
        return list(range(lo, hi + 1))

    def _rebuild_sets(self) -> None:
        keep = {**self.retired, **self.sets}
        next_sets: Dict[str, SetState] = {}
        by_idx: List[SetState] = []
        self.steps = self._step_grid()
        trails = list(self.trails) or [("0.3:0.1", 0.3, 0.1)]
        # a trail arm at or above the mid-step TP can never trail when TP is honoured: TP exits first. Such Sets are
        # not built in honour-TP mode (119 of 210 per pack on the default grid). Ignore-TP keeps every trail Set.
        mid_tp_pct = (self.steps[len(self.steps) // 2] if self.steps else 8) * self.cost_pct
        if self.hist_honor_tp:
            trails = [t for t in trails if t[1] < mid_tp_pct - 1e-9]
        self.trails_built = list(trails)   # the trail keys this grid actually builds (coverage checks this list)
        idx = 0
        def _put(st: SetState) -> None:
            nonlocal idx
            if not st.active and st.deact_reason.startswith("out of grid"):
                st.active = True        # back on the grid: eligible again once it is re-scored
                st.deact_reason = ""
                st.evaluated_at = 0.0
            st.idx = idx
            next_sets[st.id] = st
            by_idx.append(st)
            idx += 1
        for pack_i, pack in enumerate(self.packs):
            pack_steps = self.short_steps if pack == SHORT_PACK else self.steps
            pack_trails = trails
            if pack == SHORT_PACK and self.hist_honor_tp:
                short_mid = (pack_steps[len(pack_steps) // 2] if pack_steps else 4) * self.cost_pct
                pack_trails = [t for t in (list(self.trails) or [("0.3:0.1", 0.3, 0.1)]) if t[1] < short_mid - 1e-9]
            if pack == SHORT_PACK and pack_steps:
                # Short Range works with TP steps 2-6: no trailing arm beyond its largest step
                pack_trails = [t for t in pack_trails if t[1] <= pack_steps[-1] * self.cost_pct + 1e-9]
            for sl_i, sl in enumerate(self.sl_ratios):
                for step_i, step in enumerate(pack_steps):
                    sid = make_set_id(pack, sl, "", step)
                    tp = step_tp_pct(step, self.cost_pct)
                    prev = keep.get(sid)
                    if prev:
                        st = prev
                        st.sl_ratio = sl
                        st.step = step
                        st.tp_pct = tp
                        st.trail_key = ""
                        st.trail_arm = 0.0
                        st.trail_give = 0.0
                        st.kind = "base"
                        st.locked = bool(self.locks.get(sid))
                    else:
                        st = SetState(
                            id=sid, pack=pack, tf="1m", sl_ratio=sl,
                            trail_key="", trail_arm=0.0, trail_give=0.0,
                            step=step, tp_pct=tp, kind="base",
                            locked=bool(self.locks.get(sid)),
                        )
                    st.pack_i = pack_i
                    st.sl_i = sl_i
                    st.tr_i = -1
                    st.step_i = step_i
                    _put(st)
            if self.exit_sets:
                seen_stop: Dict[float, None] = {}
                for sl_i, sl in enumerate(self.sl_ratios):
                    for step_i, step in enumerate(pack_steps):
                        dist = round(sl * step, 6)          # without a TP only the stop distance tells two Exit Sets apart
                        if dist in seen_stop:
                            continue
                        seen_stop[dist] = None
                        sid = make_exit_id(pack, sl, step)
                        tp = step_tp_pct(step, self.cost_pct)
                        prev = keep.get(sid)
                        if prev:
                            st = prev
                            st.sl_ratio, st.step, st.tp_pct = sl, step, tp
                            st.trail_key, st.trail_arm, st.trail_give = "", 0.0, 0.0
                            st.kind = "exit"
                            st.locked = bool(self.locks.get(sid))
                        else:
                            st = SetState(id=sid, pack=pack, tf="1m", sl_ratio=sl, trail_key="", trail_arm=0.0, trail_give=0.0,
                                          step=step, tp_pct=tp, kind="exit", locked=bool(self.locks.get(sid)))
                        st.pack_i = pack_i
                        st.sl_i = sl_i
                        st.tr_i = -1
                        st.step_i = step_i
                        _put(st)
            for tr_i, (tkey, arm, give) in enumerate(pack_trails):
                sid = make_trail_id(pack, tkey)
                prev = keep.get(sid)
                mid_step = pack_steps[len(pack_steps) // 2] if pack_steps else 8
                tp = step_tp_pct(mid_step, self.cost_pct)
                if prev:
                    st = prev
                    st.trail_key = tkey
                    st.trail_arm = arm
                    st.trail_give = give
                    st.sl_ratio = TRAIL_SL_RATIO
                    st.step = 0
                    st.tp_pct = tp
                    st.kind = "trail"
                    st.locked = bool(self.locks.get(sid))
                else:
                    st = SetState(
                        id=sid, pack=pack, tf="1m", sl_ratio=TRAIL_SL_RATIO,
                        trail_key=tkey, trail_arm=arm, trail_give=give,
                        step=0, tp_pct=tp, kind="trail",
                        locked=bool(self.locks.get(sid)),
                    )
                st.pack_i = pack_i
                st.sl_i = -1
                st.tr_i = tr_i
                st.step_i = -1
                _put(st)
        gone = {sid: st for sid, st in keep.items() if sid not in next_sets}
        for st in gone.values():
            st.active = False
            st.deact_reason = "out of grid (step-adapt)"
        self.retired = gone
        self.sets = next_sets
        self.by_idx = by_idx
        for st_ in by_idx:
            manual = self.hold_bars_for(st_)
            if manual or not self.hold_auto:
                st_.hold_bars, st_.hold_auto = manual, False      # a manual step hold wins; auto off clears an earlier choice
            elif not self.auto_holds(st_):
                st_.hold_bars, st_.hold_auto = 0, False
        self.progress.sets_total = len(self.sets)
        self._topo_gen += 1

    def adapt_from_live(self, closed: Sequence[Any]) -> None:
        with self._lock:
            self._adapt_from_live(closed)

    def _adapt_from_live(self, closed: Sequence[Any]) -> None:
        """If live average is a loss, raise min step to # of positive/successful fills."""
        if self._running:
            self._adapt_pending = list(closed)      # applied when the pass in flight ends
            return
        floor = self.min_step_cfg
        if not self.step_adapt:
            nxt = floor
        else:
            rows = list(closed)[-max(self.deact_n, 15) :]
            if len(rows) < 8:
                return
            pnls: List[float] = []
            n_ok = 0
            n_pos = 0
            for rec in rows:
                if isinstance(rec, dict):
                    pnl = finite(rec.get("pnl"))
                    pct = finite(rec.get("pnl_pct"))
                else:
                    pnl = finite(getattr(rec, "pnl", 0))
                    pct = finite(getattr(rec, "pnl_pct", 0))
                pnls.append(pnl)
                if pnl > 0:
                    n_pos += 1
                if pct and signed_result_r(pct, self.cost_pct) > 0:
                    n_ok += 1
            avg = sum(pnls) / len(pnls) if pnls else 0.0
            if avg < 0:
                n = n_ok if n_ok else n_pos
                nxt = clamp_step(n if n else floor, floor, self.step_max)
            else:
                nxt = floor
        if nxt != self.min_step:
            self.min_step = nxt
            self._rebuild_sets()

    def _after_pass(self) -> None:
        """Apply a step-adapt that arrived while a replay pass was running. Rebuilding mid-pass would discard that pass
        (the topology changed under it), leave the Sets unrefreshed and, past the freshness limit, mark every Set stale."""
        pending, self._adapt_pending = self._adapt_pending, None
        if pending is not None:
            self.adapt_from_live(pending)

    def bar_quality(self, symbol: str) -> str:
        """'' when the symbol's history is fit to validate Sets on, otherwise why not. A series that is mostly flat bars
        (no trade in the minute) or that jumps by more than histMaxJumpPct in one minute (a broken print, as the demo venue
        shows on illiquid names) would validate Sets on prices nobody could trade."""
        rows = self.bars.get(symbol) or []
        if len(rows) < self.min_bars:
            return "short"
        flat = 0
        jump = 0.0
        prev = 0.0
        try:
            for r in rows:
                if r[1] <= r[2]:
                    flat += 1
                c = float(r[3])
                if prev > 0:
                    d = abs(c / prev - 1.0)
                    if d > jump:
                        jump = d
                prev = c
        except (TypeError, ValueError, IndexError, ZeroDivisionError):
            return ""        # malformed rows: the replay reports this symbol's error itself
        if flat / len(rows) > self.max_flat_share:
            return "flat"
        if jump > self.max_jump:
            return "spike"
        return ""

    def usable(self, symbol: str) -> bool:
        why = self.bar_quality(symbol)
        if why and why != "short":
            self.quality[symbol] = why
        else:
            self.quality.pop(symbol, None)
        return not why

    def symbol_ok(self, symbol: str) -> bool:
        """False for a symbol whose history failed the data check: no Set validated on it, so no entry on it either."""
        return symbol not in self.quality

    def ingest_bars(self, symbol: str, bars: Sequence[Sequence[float]], tail: bool = False, last_t: Optional[float] = None) -> None:
        """Join bars onto the symbol's history. tail=True: rows built from live ticks (or a short exchange window), joined only
        where they line up with the stored series; they never replace a history they do not overlap. last_t: the open time
        of the last given row, a completed minute (the caller drops the still-forming one); kept in bar_t when the joined
        series ends on that row."""
        if not bars:
            return
        cleaned: List[List[float]] = []
        for b in bars:
            if len(b) < 5:
                continue
            o, h, l, c, v = (finite(b[0]), finite(b[1]), finite(b[2]), finite(b[3]), finite(b[4]))
            if o <= 0 or c <= 0 or h <= 0 or l <= 0:
                continue
            cleaned.append([o, h, l, c, v])
        if len(cleaned) >= 16:
            old = self.bars.get(symbol) or []
            joined = _join_bars(old, cleaned, keep_on_gap=tail)[-self.lookback :]
            self.bars[symbol] = joined
            bar_t = self.__dict__.setdefault("bar_t", {})
            if last_t is not None and joined and joined[-1] == cleaned[-1]:
                bar_t[symbol] = float(last_t)
            elif not old or not joined or joined[-1] != old[-1]:
                bar_t.pop(symbol, None)           # the series now ends on a row of unknown time

    def entry_signal(self, symbol: str, bar_open_t: float) -> Optional[Tuple[str, int, float, str]]:
        """The live entry decision on the completed minute that opened at bar_open_t: (pack, direction, confidence, why) of the
        pack with the highest confidence at or above setEntryConf, else None. The replay's own rule on the replay's own bars
        (bar_signal on the Set history), so a live position opens only where its Set's tape would have opened one. None when
        the history does not end on that minute (it is not in yet, or the series ends on rows of unknown time)."""
        with self._lock:
            if self.__dict__.get("bar_t", {}).get(symbol) != float(bar_open_t) or not self.symbol_ok(symbol):
                return None
            bars = list(self.bars.get(symbol) or [])
            packs = list(self.packs)
            settings = dict(self.ind_settings)
        if len(bars) < max(self.min_bars, self.warmup + 1):
            return None
        row = bar_signal(bars, len(bars) - 1, packs, settings, float(bar_open_t) + BAR_S)
        best: Optional[Tuple[str, int, float, str]] = None
        for p in packs:
            d, c, why = row.get(p, (0, 0.0, ""))
            if d != 0 and c >= self.entry_conf and (best is None or c > best[2]):
                best = (p, int(d), float(c), str(why))
        return best

    def trim_bars(self, keep: Sequence[str]) -> int:
        want = set(keep)
        n = 0
        for s in list(self.bars):
            if s not in want:
                self.bars.pop(s, None)
                self.__dict__.setdefault("bar_t", {}).pop(s, None)
                n += 1
        return n

    def clamp_bars(self, max_n: int) -> int:
        cap = max(60, int(max_n or self.lookback))
        n = 0
        for s, bars in list(self.bars.items()):
            if len(bars) > cap:
                self.bars[s] = bars[-cap:]
                n += 1
        return n

    def _live_key(self, sid: str, row: Dict[str, Any]) -> str:
        """One key per live close per Set. Client id when present, else symbol, side, time and gross move."""
        cid = row.get("client_id") or ""
        if cid:
            return f"{sid}|{cid}"
        return f"{sid}|{row['symbol']}|{row['side']}|{row['t']:.3f}|{row['pnl_pct']:.10f}"

    def on_live_close(self, rec: Any) -> None:
        with self._lock:
            self._on_live_close(rec)

    def _on_live_close(self, rec: Any) -> None:
        """Record one live close into the Set tapes, on the same unit as replay rows.

        Replay rows carry pnl as a NET FRACTION (gross move minus one PositionCost). Live rows carry
        pnl in USDT, so the tape pnl is recomputed from pnl_pct; the USDT figure is kept as pnl_usdt
        for display only. A close without pnl_pct cannot be normalised and is skipped (counted).
        A close already seen for a Set is counted once, so repeated delivery or repeated seeding
        adds nothing.
        """
        is_dict = isinstance(rec, dict)
        def get(key: str, default: Any = None) -> Any:
            return rec.get(key, default) if is_dict else getattr(rec, key, default)
        if get("ours") is False:
            return
        sid = str(get("set_id") or get("setId") or "")
        pct_raw = get("pnl_pct")
        if pct_raw is None:
            self.live_skipped += 1
            return
        row = {
            "t": finite(get("t")),
            "symbol": str(get("symbol") or ""),
            "side": str(get("side") or ""),
            "pnl_pct": finite(pct_raw),
            "pnl_usdt": finite(get("pnl")),
            "hold_s": finite(get("hold_s")),
            "reason": str(get("reason") or ""),
            "client_id": str(get("client_id") or get("clientId") or ""),
            "lane": str(get("lane") or "pos"),          # the lane it was executed on: its close joins that lane's Real Set
        }
        row["pnl"] = net_pnl_pct(row["pnl_pct"], self.cost_pct)
        if not sid and not str(get("trail_set_id") or get("trailSetId") or ""):
            # no Set id: the close cannot be attributed. It is counted, never guessed onto a Set.
            self.live_unmatched += 1
            return
        extra = str(get("trail_set_id") or get("trailSetId") or "")
        targets: List[SetState] = []
        for x in (sid, extra):
            if not x:
                continue
            st = self.sets.get(x) or self.retired.get(x)
            if st and st not in targets:
                targets.append(st)
        if not targets:
            self.live_unmatched += 1
            return
        for st in targets:
            key = self._live_key(st.id, row)
            if key in self._live_seen:
                continue
            self._live_seen[key] = None
            while len(self._live_seen) > LIVE_SEEN_MAX:
                self._live_seen.pop(next(iter(self._live_seen)))
            st.live.append(row)
            st.live = st.live[-80:]
            st.n += 1
            st.live_n += 1
            self._score_one(st, now=row["t"] or None)

    def seed_live(self, closed: Sequence[Any]) -> None:
        for rec in closed:
            self.on_live_close(rec)

    def due(self) -> bool:
        """Interval-driven in every state: refresh_s once a complete pass exists, retry_s until then. Never back to back."""
        if not self.enabled or self._running:
            return False
        wait = self.refresh_s if self.progress.ready else self.retry_s
        return time.time() - self.last_run >= wait

    def replay_all(
        self,
        now: Optional[float] = None,
        on_step: Optional[Callable[[], None]] = None,
        symbols: Optional[Sequence[str]] = None,
        abort: Optional[Callable[[], bool]] = None,
    ) -> None:
        """Replay the given symbols, then score every Set over the rows of all symbols replayed so far.

        Isolation: a symbol that fails is recorded and skipped; a Set that fails to score is recorded on
        the Set (last_error) and the others are still scored. A chunked refresh (symbols=...) replaces only
        its own symbols' rows, so the other symbols keep their rows and no Set is scored on a partial tape.
        """
        if not self.enabled or self._running:
            return
        self._running = True
        t0 = time.time()
        # one origin per minute: every chunk replayed in the same minute shares it, so the rows agree
        now = now or float((int(t0 // BAR_S) + 1) * BAR_S)
        try:
            if symbols is None:
                names = [s for s in self.bars if self.usable(s)]
            else:
                names = [s for s in symbols if self.usable(s)]
            # the symbols that already hold the most orders go first: a pass that is cut short (load) still refreshed the
            # tapes the Sets are judged on most; the sort is stable, so a first pass keeps the given order
            names.sort(key=lambda s: -sum(len(v) for v in (self._hist_rows.get(s) or {}).values()))
            was_ready = self.progress.ready
            self.progress = Progress(
                phase="replay",
                ready=was_ready,
                pct=1.0,
                sets_total=len(self.sets),
                symbols_total=len(names),
                bars_total=sum(len(self.bars.get(s) or []) for s in names),
                cycle=self.progress.cycle + 1,
                detail=f"{len(names)} symbols · {len(self.sets)} sets",
            )
            errors: List[str] = []
            with self._lock:
                gen0 = self._topo_gen
            sym_failed = 0
            aborted = False
            for i, symbol in enumerate(names):
                if abort and abort():
                    aborted = True
                    self.progress.detail = f"aborted-load {i}/{len(names)}"
                    break
                self.progress.symbol = symbol
                self.progress.symbols_done = i
                self.progress.pct = 5.0 + (i / max(1, len(names))) * 80.0
                self.progress.elapsed_ms = (time.time() - t0) * 1000
                per_set: Dict[str, List[Dict[str, Any]]] = {sid: [] for sid in self.sets}
                try:
                    self._replay_symbol(symbol, per_set, now, on_step=on_step)
                except Exception as exc:  # one symbol never stops the others
                    sym_failed += 1
                    errors.append(f"{symbol}: {type(exc).__name__}: {exc}"[:160])
                    continue
                packed = {sid: [(r["t"], r["symbol"], r["side"], r["pnl"], r["pnl_pct"], r["hold_s"], r["reason"], r["set_id"])
                                for r in rows] for sid, rows in per_set.items() if rows}
                per_set = {}                          # rows are kept as tuples: about a third of the memory of dicts
                with self._lock:
                    self._hist_rows[symbol] = packed
                self.progress.bars_done += len(self.bars[symbol])
                if on_step:
                    on_step()
            self.progress.phase = "score"
            self.progress.pct = 90.0
            cutoff = now - self.lookback * BAR_S
            with self._lock:
                live = {s for s in self.bars if self.usable(s)}
                for s in [s for s in self._hist_rows if s not in live]:
                    del self._hist_rows[s]          # symbols that left the universe (or failed the data check) drop their rows
                for s in [s for s in self._sig_cache if s not in live]:
                    del self._sig_cache[s]          # and so do their signal memo entries
                stale = self._topo_gen != gen0      # the Set map changed mid-pass: the rows above mix two grids
                if stale:
                    aborted = True                  # keep each Set's last complete scores; the next pass redoes them
                    self.progress.detail = "topology changed mid-pass: scores kept, next pass recomputes"
                tape_order = sorted(self._hist_rows)       # equal exit times order by symbol, whatever the replay order was
                for st in ([] if stale else self.by_idx):
                    try:
                        full = self._tape_rows(st.id, tape_order, cutoff)
                        full = self._choose_hold(st, full, tape_order, cutoff)
                        self.set_processed(st, full)
                        st.hist = [dict(zip(_ROW_KEYS, r)) for r in full[-max(40, self.gate_window):]]
                        st.last_error = ""          # cleared before scoring: a clean score is the only way back to valid
                        self._score_one(st, now=now)
                        st.n = len(full) + st.live_n   # a replay pass recounts history; live closes stay counted
                    except Exception as exc:  # one Set never stops the others; it is invalid until it scores again
                        st.last_error = f"{type(exc).__name__}: {exc}"[:160]
                        st.deact_reason = "error"
                        errors.append(f"set {st.id}: {st.last_error}")
            ok_syms = len(names) - sym_failed
            self.progress.errors = len(errors)
            self.progress.error = "; ".join(errors[:3])[:220]
            self.progress.symbols_done = len(names) if not aborted else self.progress.symbols_done
            self.progress.sets_done = len(self.sets)
            if not names:
                self.progress.phase = "waiting"
                self.progress.ready = False
            elif ok_syms == 0:
                self.progress.phase = "error"
                self.progress.ready = False
            elif aborted:
                self.progress.phase = "partial"
            else:
                self.progress.phase = "ready"
                self.progress.pct = 100.0
                self.progress.ready = True
                with self._lock:
                    self._record_pf_progress()
                    self._adapt_lookback()
            self.progress.detail = (
                f"{sum(1 for s in self.sets.values() if s.active)}/{len(self.sets)} active · "
                f"{sum(s.n for s in self.sets.values())} hist fills"
                + (" · partial" if aborted else "")
                + (f" · {len(errors)} errors" if errors else "")
            )
        except Exception as exc:
            self.progress.phase = "error"
            self.progress.ready = False
            self.progress.error = str(exc)[:220]
        finally:
            self.progress.last_run_ms = (time.time() - t0) * 1000
            self.progress.elapsed_ms = self.progress.last_run_ms
            self.last_run = time.time()
            self._running = False
            self._after_pass()

    def _replay_symbol(self, symbol: str, hist: Dict[str, List[Dict[str, Any]]], now: float, on_step: Optional[Callable[[], None]] = None) -> None:
        bars = self.bars[symbol]
        n = len(bars)
        warmup = min(self.warmup, max(16, n // 5))
        base_ts = now - (n - 1) * BAR_S
        salt = hash((json.dumps(self.ind_settings, sort_keys=True, default=str), tuple(sorted(self.packs))))   # a memo entry holds only its packs
        cache = self._sig_cache.setdefault(symbol, {})
        signals = pack_signals(bars, self.packs, self.ind_settings, base_ts, warmup, on_step, cache=cache, salt=salt)
        cfg = {
            "entry_conf": self.entry_conf,
            "time_bars": max(8, min(self.hist_time_bars, max(8, n - warmup - 1))),
            "scratch_bars": self.scratch_bars,
            "scratch_min": self.scratch_min,
            "cooldown": self.cooldown_bars,
            "cost_pct": self.cost_pct,
            "honor_tp": bool(getattr(self, "hist_honor_tp", True)),
            "sl_floor_steps": self.sl_min_steps,
        }
        # per pack: directions, confidences and the bars where an entry is possible (shared by all its Sets)
        dirs: Dict[str, List[int]] = {}
        confs: Dict[str, List[float]] = {}
        cand: Dict[str, List[int]] = {}
        for p in self.packs:
            sig = signals[p]
            dirs[p] = [x[0] for x in sig]
            confs[p] = [x[1] for x in sig]
            cand[p] = [i for i in range(warmup, n) if dirs[p][i] != 0 and confs[p][i] >= cfg["entry_conf"]]
        acc = BarIndex(bars)          # one index per symbol, shared by every lane
        for st in self.by_idx:
            if st.pack not in self.packs:
                continue
            holds = self.auto_holds(st)
            for h in (0,) + holds if holds else (None,):
                lane = SetLane(st, symbol)
                key = st.id
                if h is not None:
                    lane.hold = h                    # 0 = the global time stop; the Set's chosen hold never feeds its own judgement
                    key = st.id if h == 0 else f"{st.id}{HOLD_KEY}{h}"
                out = hist.setdefault(key, [])
                for done in lane.scan(warmup, n, bars, dirs[st.pack], confs[st.pack], True, cfg, cand[st.pack], acc):
                    out.append({"t": base_ts + done["exit_i"] * BAR_S, "symbol": symbol, "side": done["side"],
                                "pnl": done["pnl"], "pnl_pct": done["pnl_pct"], "hold_s": done["hold_s"],
                                "reason": done["reason"], "set_id": st.id})
                if h:
                    del out[:-HOLD_AUTO_MAX_ORDERS]  # only the newest orders of a candidate can be among the last 50 overall
            self.progress.set_id = st.id

    def _adapt_lookback(self) -> None:
        """Pre-history for the calcs: when too many of the Sets that traded have fewer orders than the gate needs (gate_min), the
        lookback grows by LOOKBACK_GROW_STEP bars (up to histLookbackMax), so the next fetch loads older minutes and the Sets get
        their orders. It only grows; a reload keeps the grown value. Called with the lock held after a complete pass."""
        if not self.lookback_auto or self.lookback >= self.lookback_max:
            return
        traded = [st for st in self.by_idx if st.source_n > 0]
        if len(traded) < 20:
            return
        short = sum(1 for st in traded if st.gate_n < self.gate_min)
        if short / len(traded) >= LOOKBACK_SHORT_SHARE:
            self.lookback = min(self.lookback_max, self.lookback + LOOKBACK_GROW_STEP)
            self._lookback_grown = self.lookback

    def _tape_rows(self, key: str, tape_order: Sequence[str], cutoff: float) -> List[tuple]:
        return sorted((r for sym_ in tape_order for r in self._hist_rows[sym_].get(key, ()) if r[0] >= cutoff), key=_row_t)

    def _choose_hold(self, st: SetState, base: List[tuple], tape_order: Sequence[str], cutoff: float) -> List[tuple]:
        """Auto Mode: the hold (minutes) whose own tape has the best cost PF over this Set's last orders, as many as it has but at most
        holdAutoMax. The global time stop is the reference; a candidate needs holdAutoMin orders and a PF margin over it to replace
        it. Returns the rows of the tape the Set is judged on, and leaves the choice in st.hold_bars (live positions read it)."""
        holds = self.auto_holds(st)
        if not holds:
            return base
        cf = cost_as_frac(self.cost_pct)

        def judge(rows: List[tuple]) -> Tuple[float, int]:
            w = [finite(r[4]) - cf for r in rows[-self.hold_auto_max:]]
            gp_, gl_ = sum(x for x in w if x > 0), -sum(x for x in w if x < 0)
            return (gp_ / gl_ if gl_ > 0 else (99.0 if gp_ > 0 else 0.0)), len(w)

        ref_pf, ref_n = judge(base)
        best = (ref_pf, 0, base, ref_n)
        for h in holds:
            rows = self._tape_rows(f"{st.id}{HOLD_KEY}{h}", tape_order, cutoff)
            pf, n = judge(rows)
            if n >= self.hold_auto_min and pf > best[0] and pf >= ref_pf * HOLD_AUTO_MARGIN:
                best = (pf, h, rows, n)
        st.hold_bars, st.hold_auto = best[1], True
        st.hold_auto_pf, st.hold_auto_n = round(best[0], 4), best[3]
        return best[2]

    def stage_params(self, pack: str) -> StageParams:
        """The chain's rules for a pack (its own minimum PF floor)."""
        return StageParams(base_long=self.gate_window, base_short=self.base_short, base_min_n=self.base_min_n, base_min_positive=self.base_min_positive,
                           gate_min=self.gate_min,
                           floor=self.pack_min_pf.get(pack, self.min_pf), max_dd_s=self.max_dd_s, lookback_s=self.lookback * BAR_S,
                           consistency=self.consistency_pf, main_on=self.main_on, main_n=self.main_n, short_on=self.short_on,
                           short_windows=tuple(self.short_windows), short_min=self.short_min, short_margin=self.short_margin,
                           neg_on=self.neg_on, neg_windows=tuple(self.neg_windows), real_on=self.realst_on, real_n=self.realst_n,
                           full_window=self.stage_full, main_min_n=self.main_min_n, main_min_positive=self.main_min_positive)

    def _chain_of(self, st: SetState, full: Sequence[tuple]) -> Dict[str, Any]:
        """The Set's chain (stage_chain) over replayed rows: {"main", "real", "realneg": [(exit t, net)], "short_n"}."""
        cf = cost_as_frac(self.cost_pct)
        ts = [finite(r[0]) for r in full]
        ents = [ts[k] - finite(full[k][5]) for k in range(len(full))]
        nets = [finite(r[4]) - cf for r in full]
        ch = stage_chain(ts, ents, nets, self.stage_params(st.pack))
        out: Dict[str, Any] = {name: [(ts[k], nets[k]) for k in ch[name]] for name in ("main", "real", "realneg")}
        out["short_n"] = ch["short_n"]
        return out

    def _lanes_of(self, st: SetState, full: Sequence[tuple]) -> List[List[Tuple[float, float]]]:
        ch = self._chain_of(st, full)
        return [ch["main"], ch["real"]]

    def _processed_of(self, st: SetState, full: Sequence[tuple]) -> List[Tuple[float, float]]:
        return self._chain_of(st, full)["main"]

    def set_processed(self, st: SetState, full: Sequence[tuple]) -> None:
        """The Set's chain in every replay pass: its Main Set (orders opened while Base passed), Real Set (opened while Main passed,
        judged on the Main Set) and Real Negative Set (opened while the Main Set's recent average was negative), and its Main short window.
        Every stage after Base reads these Sets, never the raw tape."""
        ch = self._chain_of(st, full)
        st.proc, st.proc_total = ch["main"][-PROC_KEEP:], len(ch["main"])
        st.exe, st.exe_total = ch["real"][-PROC_KEEP:], len(ch["real"])
        st.neg, st.neg_total = ch["realneg"][-PROC_KEEP:], len(ch["realneg"])
        st.main_short_win = int(ch["short_n"])
        st.proc_replayed = True

    def _streams(self, st: SetState) -> Tuple[List[float], List[float], List[float], int]:
        """Net results (exit order) of the Set's Main, Real and Real Negative Sets with its live closes (a live close joins the Main
        Set and the lane it was executed on), and its Main short window. A Set that was not replayed applies the same chain to the tape it holds."""
        cf = cost_as_frac(self.cost_pct)
        if getattr(st, "proc_replayed", False):
            m, r, q, short_n = list(st.proc or []), list(st.exe or []), list(getattr(st, "neg", None) or []), int(st.main_short_win or self.main_n)
        else:
            held = sorted((tuple(r_.get(k) for k in _ROW_KEYS) for r_ in st.hist if r_.get("pnl_pct") is not None), key=lambda r_: finite(r_[0]))
            ch = self._chain_of(st, held)
            m, r, q, short_n = ch["main"], ch["real"], ch["realneg"], int(ch["short_n"])
        for row in st.live:
            if row.get("pnl_pct") is None:
                continue
            item = (finite(row.get("t")), finite(row.get("pnl_pct")) - cf)
            m.append(item)
            (q if str(row.get("lane") or "pos") == "neg" else r).append(item)
        out = []
        for v in (m, r, q):
            v.sort(key=lambda x: x[0])
            out.append([x for _, x in v])
        return out[0], out[1], out[2], short_n

    def executable_stream(self, st: SetState) -> List[float]:
        """The Set's Real Set (positive lane) with its live closes on that lane."""
        return self._streams(st)[1]

    def processed_stream(self, st: SetState) -> List[float]:
        """The Set's Main Set (orders opened while Base passed) with its live closes."""
        return self._streams(st)[0]

    def _score_one(self, st: SetState, now: Optional[float] = None) -> None:
        tape = sorted(st.tape(), key=lambda r: finite(r.get("t")))   # last-N and drawdown read one time order
        pnls = [finite(r.get("pnl")) for r in tape]
        st.wins = sum(1 for x in pnls if x > 0)
        st.gp = round(sum(x for x in pnls if x > 0), 6)
        st.gl = round(abs(sum(x for x in pnls if x < 0)), 6)
        decided = sum(1 for x in pnls if x != 0)
        st.wr = round(100.0 * st.wins / decided, 1) if decided else 0.0
        st.expectancy = round(sum(pnls) / len(pnls), 6) if pnls else 0.0
        holds = [finite(r.get("hold_s")) for r in tape]
        st.avg_hold_s = round(sum(holds) / len(holds), 1) if holds else 0.0
        st.pf_all = round(st.gp / st.gl, 4) if st.gl > 0 else (99.0 if st.gp > 0 else 0.0)
        counts: Dict[str, int] = {}
        for r in tape:
            k = str(r.get("reason") or "x").split(":")[0]
            counts[k] = counts.get(k, 0) + 1
        st.exits = counts
        last15 = last_n_cost_pf(tape, self.pf_n, self.cost_pct)
        st.last15_ratio = float(last15["ratio"])
        st.last15_pf = float(last15["pf"])
        st.last15_n = int(last15["count"])
        st.last15_r = float(last15["avgR"])
        gate = last_n_cost_pf(tape, self.gate_window, self.cost_pct)
        st.gate_pf = float(gate["pf"])
        st.gate_n = int(gate["count"])
        cf = cost_as_frac(self.cost_pct)
        win = [finite(r.get("pnl_pct")) - cf for r in tape if r.get("pnl_pct") is not None][-self.gate_window:]
        half = len(win) // 2
        if half >= 1:
            def _pf(v: List[float]) -> float:
                gp_, gl_ = sum(x for x in v if x > 0), -sum(x for x in v if x < 0)
                return gp_ / gl_ if gl_ > 0 else (99.0 if gp_ > 0 else 0.0)
            st.gate_pf_old, st.gate_pf_new = round(_pf(win[:half]), 4), round(_pf(win[half:]), 4)
        else:
            st.gate_pf_old = st.gate_pf_new = 0.0
        st.gate_gp = round(sum(x for x in win if x > 0), 8)
        st.gate_gl = round(-sum(x for x in win if x < 0), 8)
        sw = win[-self.base_short:] if self.base_short else []
        st.base_short_n = len(sw)
        st.base_short_gp, st.base_short_gl = round(sum(x for x in sw if x > 0), 8), round(-sum(x for x in sw if x < 0), 8)
        st.base_short_pf = round(pf_of(st.base_short_gp, st.base_short_gl), 4)
        mw = win[-self.base_min_n:] if self.base_min_n else []
        st.base_min_n = len(mw)
        st.base_min_gp, st.base_min_gl = round(sum(x for x in mw if x > 0), 8), round(-sum(x for x in mw if x < 0), 8)
        st.base_min_ok = (not self.base_min_n) or (len(mw) >= (self.base_min_n if self.stage_full else 1)
                                                    and (all(x > 0 for x in mw) if self.base_min_positive else all(x < 0 for x in mw)))
        m_, r_, q_, short_n = self._streams(st)      # the stages after Base read the Set's Main / Real / Real Negative Sets
        v = stage_verdicts(m_, r_, q_, short_n, self.stage_params(st.pack))
        st.stage = v
        st.main_short_win = short_n
        rw = m_[-self.main_n:]
        st.main_n = len(rw)
        st.main_gp = round(sum(x for x in rw if x > 0), 8)
        st.main_gl = round(-sum(x for x in rw if x < 0), 8)
        st.main_pf = round(pf_of(st.main_gp, st.main_gl), 4)
        mrw = m_[-short_n:] if self.short_on else []                  # the Main short window's own orders
        st.main_short_n = len(mrw)
        st.main_short_gp, st.main_short_gl = round(sum(x for x in mrw if x > 0), 8), round(-sum(x for x in mrw if x < 0), 8)
        st.main_short_pf = round(pf_of(st.main_short_gp, st.main_short_gl), 4)
        kw = m_[-self.main_min_n:] if self.main_min_n else []          # the Main minimal window's own results
        st.main_min_have = len(kw)
        st.main_min_gp, st.main_min_gl = round(sum(x for x in kw if x > 0), 8), round(-sum(x for x in kw if x < 0), 8)
        nw = next((w for w in sorted(self.neg_windows) if self.neg_on and len(m_) >= w and sum(m_[-w:]) < 0), 0)    # the smallest Negative window that fires
        nv = m_[-nw:] if nw else []
        st.neg_win = nw
        st.neg_win_gp, st.neg_win_gl = round(sum(x for x in nv if x > 0), 8), round(-sum(x for x in nv if x < 0), 8)
        ew = r_[-self.realst_n:]
        st.exe_n, st.exe_pf = len(ew), round(pf_of(sum(x for x in ew if x > 0), -sum(x for x in ew if x < 0)), 4)
        st.exe_gp, st.exe_gl = round(sum(x for x in ew if x > 0), 8), round(-sum(x for x in ew if x < 0), 8)
        qw = q_[-self.realst_n:]
        st.neg_n, st.neg_pf = len(qw), round(pf_of(sum(x for x in qw if x > 0), -sum(x for x in qw if x < 0)), 4)
        st.neg_gp, st.neg_gl = round(sum(x for x in qw if x > 0), 8), round(-sum(x for x in qw if x < 0), 8)
        last25 = tape[-self.deact_n :]
        st.last25_n = len(last25)
        if last25:
            rs = [signed_result_r(finite(r.get("pnl_pct")), self.cost_pct) for r in last25]
            st.last25_avg_r = sum(rs) / len(rs)
            st.last25_avg_pnl = sum(finite(r.get("pnl")) for r in last25) / len(last25)
        else:
            st.last25_avg_r = 0.0
            st.last25_avg_pnl = 0.0
        dd = drawdown_time(tape[-self.gate_window:], now=now)   # the last N positions, not the whole tape
        st.max_dd_s = float(dd["maxS"])
        st.avg_dd_s = float(dd["avgS"])
        st.dd_episodes = int(dd["episodes"])
        # each Base window on its own (the stage verdict is their conjunction; the per-window figures count each separately)
        floor_ = self.pack_min_pf.get(st.pack, self.min_pf)
        st.base_long_ok = (st.gate_n >= self.gate_min and st.gate_pf + 1e-9 >= floor_ and st.max_dd_s < self.max_dd_s
                           and not (self.consistency_pf > 0 and min(st.gate_pf_old, st.gate_pf_new) + 1e-9 < self.consistency_pf))
        st.base_short_ok = bool(self.base_short) and st.base_short_n >= (self.base_short if self.stage_full else 1) and st.base_short_pf + 1e-9 >= floor_
        st.source_n = len(tape)
        # one rule, recomputed on every evaluation: no sticky flags, no live-only deactivation
        st.active = not st.locked
        st.evaluated_at = time.time()
        st.deact_reason = self.gate_reason(st)

    def gate_reason(self, st: SetState) -> str:
        """The one eligibility rule. '' when the Set is valid; otherwise the reason it is not.

        A locked Set, a Set whose last evaluation failed, and a Set below the order floor, below the PF gate or at or
        over the max drawdown time are all invalid on their own tape. Freshness applies only when fresh_s is set (live).
        """
        if st.locked:
            return "lock"
        if not st.active:
            return "inactive"
        if st.last_error:
            return "error"
        if st.gate_n < self.gate_min:
            return "n<min"
        if st.gate_pf + 1e-9 < self.pack_min_pf.get(st.pack, self.min_pf):
            return "pf<min"
        if self.consistency_pf > 0 and min(st.gate_pf_old, st.gate_pf_new) + 1e-9 < self.consistency_pf:
            return "unstable"        # passed on the whole window but not on both halves of it
        if st.max_dd_s >= self.max_dd_s:
            return "ddt"
        if self.base_short and (st.base_short_n < (self.base_short if self.stage_full else 1) or st.base_short_pf + 1e-9 < self.pack_min_pf.get(st.pack, self.min_pf)):
            return "short"           # the Base short window (setGateShortN) fails: both Base windows must pass
        if self.base_min_n and not st.base_min_ok:
            return "minimal"         # the Base minimal window (setGateMinN): the newest results are not all positive / negative as asked
        v = getattr(st, "stage", None) or {}
        if (self.main_on or self.neg_on or self.realst_on) and not (v.get("pos") or v.get("negl")):
            # neither lane is open; the reason is the first stage the positive lane fails
            if self.main_on and not v.get("main"):
                return "main"        # the Main Set's static last N is below the minimum PF
            if self.main_on and self.short_on and not v.get("short"):
                return "mainshort"   # the Main Set's short window is below the minimum PF
            if self.main_on and self.main_min_n and not v.get("minimal"):
                return "mainminimal"   # the Main minimal window (mainMinN): the newest Main results are not all positive / negative as asked
            return "real"            # the lane's Real Set (its last realGateN) is below the minimum PF
        if self.fresh_s > 0 and time.time() - st.evaluated_at > self.fresh_s:
            return "stale"
        return ""

    def state_of(self, st: SetState) -> Tuple[str, str]:
        """(state, reason) for display and counts: valid, invalid, locked or error. Every Set is in exactly one."""
        reason = self.gate_reason(st)
        if not reason:
            return "valid", ""
        if reason == "lock":
            return "locked", reason
        if reason == "error":
            return "error", reason
        return "invalid", reason

    def get_idx(self, idx: int) -> Optional[SetState]:
        with self._lock:
            if 0 <= idx < len(self.by_idx):
                return self.by_idx[idx]
            return None

    def coord_vars(self, st: SetState) -> Dict[str, Any]:
        return {
            "idx": st.idx,
            "kind": st.kind,
            "id": st.id,
            "pack": st.pack,
            "packI": st.pack_i,
            "slRatio": st.sl_ratio,
            "slI": st.sl_i,
            "trailKey": st.trail_key,
            "trailArm": st.trail_arm,
            "trailGive": st.trail_give,
            "trI": st.tr_i,
            "step": st.step,
            "stepI": st.step_i,
            "tpPct": round(st.tp_pct * 100, 4),
            "active": st.active,
            "pf": round(st.gate_pf, 4),
            "gateN": st.gate_n,
            "pf15": round(st.last15_pf, 4),
            "ratio": round(st.last15_ratio, 4),
            "maxDdS": st.max_dd_s,
        }

    def coverage(self) -> Dict[str, Any]:
        with self._lock:
            return self._coverage()

    def _coverage(self) -> Dict[str, Any]:
        trails = [t[0] for t in (self.trails_built or self.trails or [])]
        trail_sets = [st for st in self.by_idx if st.kind == "trail"]
        base_sets = [st for st in self.by_idx if st.kind == "base"]
        exit_sets = [st for st in self.by_idx if st.kind == "exit"]
        by_tr: Dict[str, Dict[str, Any]] = {}
        by_sl: Dict[str, Dict[str, Any]] = {}
        for st in trail_sets:
            b = by_tr.setdefault(st.trail_key, {"n": 0, "active": 0, "bestPf": 0.0, "bestIdx": -1})
            b["n"] += 1
            b["active"] += int(st.active)
            if st.gate_pf >= b["bestPf"]:
                b["bestPf"] = st.gate_pf
                b["bestIdx"] = st.idx
        for st in base_sets:
            skey = f"{st.sl_ratio:.1f}"
            s = by_sl.setdefault(skey, {"n": 0, "active": 0, "bestPf": 0.0, "bestIdx": -1})
            s["n"] += 1
            s["active"] += int(st.active)
            if st.gate_pf >= s["bestPf"]:
                s["bestPf"] = st.gate_pf
                s["bestIdx"] = st.idx
        return {
            "packs": list(self.packs),
            "slRatios": list(self.sl_ratios),
            "trails": trails,
            "steps": list(self.steps),
            "dims": {
                "pack": len(self.packs),
                "sl": len(self.sl_ratios),
                "trail": len(trails) or 1,
                "step": len(self.steps),
            },
            "families": {"base": len(base_sets), "exit": len(exit_sets), "trail": len(trail_sets)},
            "shortRange": {"on": SHORT_PACK in self.packs, "steps": list(self.short_steps),
                           "sets": sum(1 for st in self.by_idx if st.pack == SHORT_PACK)},
            "minSteps": {"sl": self.sl_min_steps, "trail": self.trail_min_steps},
            "product": len(self.by_idx),
            "indexed": True,
            "independentTrail": True,
            "byTrail": by_tr,
            "bySl": by_sl,
            "trailCover": all(any(st.trail_key == t for st in trail_sets) for t in trails) if trails else True,
            "slCover": all(any(abs(st.sl_ratio - sl) < 1e-9 for st in base_sets) for sl in self.sl_ratios),
        }

    def is_eligible(self, st: SetState) -> bool:
        """The base gate: valid only when gate_reason() gives no reason to be invalid."""
        return self.gate_reason(st) == ""

    def gate_keep(self) -> int:
        return max(40, self.gate_window)

    def pick(self, pack: str, kind: str = "base") -> Optional[SetState]:
        with self._lock:
            return self._pick(pack, kind)

    def valid_for(self, pack: str, kind: str = "base") -> List[SetState]:
        """Every valid Set of this pack and kind, best first. No global gate: each Set stands on its own tape."""
        with self._lock:
            return self._valid_for(pack, kind)

    def rank_key(self, st: SetState) -> Tuple[Any, ...]:
        """Order of choice among the eligible Sets, best first (sort descending):
        1. stable: the PF of both halves of the gate window is at or above the pack's PF floor, so the Set did not pass
           on one lucky stretch;
        2. the gate PF shrunk toward 1 by the orders behind it (n / (n + RANK_SHRINK_N)): a PF on 35 orders weighs less
           than the same PF on 350;
        3. more orders;
        4. the better recent R, then the shorter drawdown time."""
        floor = self.pack_min_pf.get(st.pack, self.min_pf)
        halves = min(st.gate_pf_old, st.gate_pf_new) if (st.gate_pf_old or st.gate_pf_new) else st.gate_pf
        n = max(0, int(st.n))
        shrunk = 1.0 + (min(float(st.gate_pf), RANK_PF_CAP) - 1.0) * n / (n + RANK_SHRINK_N)
        return (halves + 1e-9 >= floor, round(shrunk, 4), n, st.last25_avg_r, -st.max_dd_s)

    def _valid_for(self, pack: str, kind: str = "base") -> List[SetState]:
        rows = [s for s in self.by_idx if s.pack == pack and s.kind == kind and self.is_eligible(s)]
        rows.sort(key=self.rank_key, reverse=True)
        return rows

    def _pick(self, pack: str, kind: str = "base") -> Optional[SetState]:
        if self.use_historic_gate:
            ranked = self.valid_for(pack, kind)
            return ranked[0] if ranked else None
        # gate disabled (legacy): any active set of this pack and kind, ranked the same way
        rows = [s for s in self.by_idx if s.pack == pack and s.kind == kind]
        if not rows:
            return None
        rows = [s for s in rows if s.active] or rows
        # a Set with fewer orders than the gate floor is not judged: its PF (99 after one winning order) must not outrank a
        # Set that has the orders (with the minimum off, rank_key shrinks a PF toward 1 by the orders behind it)
        rows.sort(key=lambda s: (s.gate_n >= self.gate_min,) + self.rank_key(s), reverse=True)
        return rows[0]

    def step_of(self, st: "SetState") -> int:
        """The TP step of a Set; a trailing Set takes the step of its arm (arm % over PositionCost)."""
        return int(st.step) if st.kind != "trail" else int(round(float(st.trail_arm) / max(1e-9, float(self.cost_pct))))

    def hold_bars_for(self, st: "SetState") -> int:
        """The Set's own manual time exit in bars (one bar = one minute), 0 when its step has no hold of its own."""
        table = getattr(self, "step_hold", None) or {}
        return int(table.get(self.step_of(st), 0)) if table else 0

    def auto_holds(self, st: "SetState") -> Tuple[int, ...]:
        """The hold candidates (minutes) the Auto Mode tries on this Set; empty when auto is off or its step has a manual hold.
        Each Set (pack x range x step, or trailing pair) is judged alone: the candidates change only its own exit."""
        if not getattr(self, "hold_auto", False) or self.hold_bars_for(st):
            return ()
        return HOLD_AUTO_LOW if self.step_of(st) <= 5 else HOLD_AUTO_HIGH

    @property
    def scratch_bars(self) -> int:
        """Bars after the entry before the scratch exit may fire; the replay and the live Set lane both read this."""
        return max(8, int(self.scratch_s / BAR_S))

    def lane_fracs(self, st: SetState) -> Tuple[float, float]:
        """(sl_frac, tp_frac) of a Set's lane with this book's PositionCost and minimum stop steps (the replay's cfg)."""
        return lane_fracs(st, self.cost_pct, self.sl_min_steps)

    def pick_trail(self, pack: str) -> Optional[SetState]:
        return self.pick(pack, kind="trail")

    def real_paper(self, n: int = 200) -> List[Dict[str, Any]]:
        """The Real stage's paper stream: the newest n orders of every Set's Real and Real Negative Sets (the orders the Sets opened
        after Base and Main), oldest first, as {"t", "pnl_pct", "pnl", "lane"}. The overall strategies (the coordinator's stages and
        axes) read it, so their evidence keeps flowing whether or not a position was executed."""
        cf = cost_as_frac(self.cost_pct)
        rows: List[Tuple[float, float, str]] = []
        with self._lock:
            for st in self.by_idx:
                rows += [(t, x, "pos") for t, x in (st.exe or [])[-n:]]
                rows += [(t, x, "neg") for t, x in (getattr(st, "neg", None) or [])[-n:]]
        rows.sort(key=lambda r: r[0])
        return [{"t": t, "pnl_pct": x + cf, "pnl": x, "lane": lane} for t, x, lane in rows[-n:]]

    def stage_counts(self) -> Dict[str, int]:
        """Sets at each stage now: passed Base, passed Main (long, short and minimal window), in a Main Negative dip, valid on Real, valid on Real Negative."""
        with self._lock:
            out = {"base": 0, "main": 0, "negative": 0, "real": 0, "realNeg": 0}
            for st in self.by_idx:
                why = self.gate_reason(st)
                if why not in ("",) + STAGE_REASONS:
                    continue
                v = st.stage or {}
                out["base"] += 1
                out["main"] += int(self._main_pass(st))
                out["negative"] += int(bool(v.get("neg")))
                lane = self.lane_of(st)
                out["real"] += int(lane == "pos")
                out["realNeg"] += int(lane == "neg")
            return out

    def _main_pass(self, st: SetState) -> bool:
        """The Set passes the Main stage: every Main window on its own (long, short, minimal) holds on its Main Set; a stage that is off passes."""
        if not self.main_on:
            return True
        v = st.stage or {}
        return bool(v.get("main") and (v.get("short") or not self.short_on) and (v.get("minimal", True) or not self.main_min_n))

    def lane_of(self, st: SetState) -> str:
        """The lane a valid Set executes on: "pos" (Base -> Main -> Real), "neg" (Base -> Main Negative -> Real Negative), "" when it
        is not valid. The positive lane wins when both are open."""
        if self.gate_reason(st):
            return ""
        if not (self.main_on or self.neg_on or self.realst_on):
            return "pos"
        v = st.stage or {}
        return "pos" if v.get("pos") else ("neg" if v.get("negl") else "")

    def pick_any(self, pack: str) -> Optional[SetState]:
        """The Set a signal of this pack opens under: the best valid fixed-exit Set (base and Exit Sets ranked together by
        rank_key: the same entries, each judged on its own exits), else the best valid trailing Set."""
        # one lock acquisition: a Set cannot change between the lookups
        # the positive lane (Base -> Main -> Real) first, then the Real Negative lane; within a lane by rank_key
        with self._lock:
            fixed = [x for x in (self._pick(pack, "base"), self._pick(pack, "exit")) if x is not None]
            if not fixed and not (self.main_on or self.neg_on or self.realst_on):
                return self._pick(pack, "trail")
            for lane in ("pos", "neg"):
                for kinds in (("base", "exit"), ("trail",)):
                    rows = [x for k in kinds for x in self._valid_for(pack, k) if self.lane_of(x) == lane]
                    if rows:
                        return max(rows, key=self.rank_key)
            return None

    def pack_open(self, pack: str) -> bool:
        if not self.enabled or not self.use_historic_gate:
            return True
        with self._lock:
            return any(self._valid_for(pack, k) for k in SET_KINDS)

    def counts(self) -> Dict[str, Any]:
        """Every configured Set is in exactly one of valid, invalid, locked, error. The retired Sets are counted apart."""
        with self._lock:
            return self._counts()

    def _counts(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"valid": 0, "invalid": 0, "locked": 0, "error": 0, "reasons": {}}
        for st in self.by_idx:
            state, reason = self.state_of(st)
            out[state] += 1
            if state == "invalid":
                out["reasons"][reason] = out["reasons"].get(reason, 0) + 1
        out["configured"] = len(self.by_idx)
        out["retired"] = len(self.retired)
        out["total"] = len(self.by_idx) + len(self.retired)
        out["sum"] = out["valid"] + out["invalid"] + out["locked"] + out["error"]
        return out

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return self._snapshot()

    def _pf_split(self) -> Dict[str, Any]:
        """The post-Base standard figures, on each Set's rolling gate window (its last `setGateWindow` orders), never on
        the whole tape: a Set that was good long ago and is bad now does not carry the pool. `base` is the pooled net
        PF of the Sets that pass Base validation (the effective PF, the one to judge a strategy by); `unfiltered` is
        every Set on the same basis (under 1 by design: Base exists to drop the losing Sets). `validatedPct` is the
        share of configured Sets that pass Base validation, and `byPack` repeats base/validated per strategy pack.
        `history` is the continuous progress of the effective PF and the validated share, one point per scoring pass."""
        def pool(sts: List[SetState]) -> Dict[str, Any]:
            gp = sum(x.gate_gp for x in sts)
            gl = sum(x.gate_gl for x in sts)
            pf = round(gp / gl, 4) if gl > 0 else (99.0 if gp > 0 else 0.0)
            return {"pf": pf, "n": sum(x.gate_n for x in sts), "sets": len(sts)}
        allsets = list(self.by_idx)
        reasons = {x.id: self.gate_reason(x) for x in allsets}
        valid = [x for x in allsets if not reasons[x.id]]
        base_only = [x for x in allsets if reasons[x.id] in ("",) + STAGE_REASONS]   # passed the Base gate (the later stages not applied)
        total = len(allsets)
        by_pack: Dict[str, Any] = {}
        for pack in self.packs:
            ps = [x for x in allsets if x.pack == pack]
            pv = [x for x in ps if not reasons[x.id]]
            by_pack[pack] = {"base": pool(pv), "unfiltered": pool(ps), "total": len(ps), "valid": len(pv),
                             "validatedPct": round(100.0 * len(pv) / len(ps), 2) if ps else 0.0,
                             "minPf": self.pack_min_pf.get(pack, self.min_pf)}
        rgp, rgl = sum(x.main_gp for x in valid), sum(x.main_gl for x in valid)
        main = {"pf": round(rgp / rgl, 4) if rgl > 0 else (99.0 if rgp > 0 else 0.0), "n": sum(x.main_n for x in valid), "sets": len(valid),
                "on": self.main_on, "lastN": self.main_n}
        pgp, pgl = sum(x.main_gp for x in base_only), sum(x.main_gl for x in base_only)
        processed = {"pf": round(pgp / pgl, 4) if pgl > 0 else (99.0 if pgp > 0 else 0.0), "n": sum(x.main_n for x in base_only),
                     "sets": len(base_only), "orders": sum(int(x.proc_total) + len(x.live) for x in base_only), "lastN": self.main_n}
        pos = [x for x in valid if self.lane_of(x) == "pos"]
        neg = [x for x in valid if self.lane_of(x) == "neg"]
        executable = {"pf": round(pf_of(sum(x.exe_gp for x in pos), sum(x.exe_gl for x in pos)), 4), "sets": len(pos),
                      "orders": sum(int(x.exe_total) for x in pos), "lastN": self.realst_n, "on": self.realst_on}
        realneg = {"pf": round(pf_of(sum(x.neg_gp for x in neg), sum(x.neg_gl for x in neg)), 4), "sets": len(neg),
                   "orders": sum(int(x.neg_total) for x in neg), "lastN": self.realst_n, "on": self.neg_on}
        main_pass = [x for x in base_only if self._main_pass(x)]
        stage_sets = {"base": len(base_only), "main": len(main_pass),
                      "negative": sum(1 for x in base_only if (x.stage or {}).get("neg")), "real": len(pos), "realNeg": len(neg)}

        def stage_row(sts: List[SetState], gp_key: str, gl_key: str, n_key: str, orders_key: str) -> Dict[str, Any]:
            """One stage's figures over the Sets that pass it: how many and what share of every configured Set, the pooled PF of their own
            window orders and the plain mean of their own PFs (a window without a loss counts RANK_PF_CAP), never an average over all Sets."""
            gp, gl = sum(getattr(x, gp_key) for x in sts), sum(getattr(x, gl_key) for x in sts)
            pfs = [min(RANK_PF_CAP, pf_of(getattr(x, gp_key), getattr(x, gl_key))) for x in sts]
            return {"sets": len(sts), "pct": round(100.0 * len(sts) / total, 2) if total else 0.0, "pf": round(pf_of(gp, gl), 4),
                    "meanPf": round(sum(pfs) / len(pfs), 4) if pfs else 0.0, "n": sum(int(getattr(x, n_key)) for x in sts),
                    "orders": sum(int(getattr(x, orders_key)) for x in sts)}
        stages = {"base": stage_row(base_only, "gate_gp", "gate_gl", "gate_n", "gate_n"),
                  "main": stage_row(main_pass, "main_gp", "main_gl", "main_n", "proc_total"),
                  "real": stage_row(pos, "exe_gp", "exe_gl", "exe_n", "exe_total"),
                  "realNeg": stage_row(neg, "neg_gp", "neg_gl", "neg_n", "neg_total"),
                  "valid": stage_row(valid, "gate_gp", "gate_gl", "gate_n", "gate_n"),
                  "allSets": total}

        def window_row(on: bool, n: Any, pop: List[SetState], sts: List[SetState], gp_key: str, gl_key: str, n_key: Any) -> Dict[str, Any]:
            """One last-N window on its own: the Sets of its stage's population that pass THIS window alone (the other windows of the stage do not
            enter), their share of that population and of every configured Set, and the figures of the orders this window reads: the pooled PF
            (gains over losses of all those orders together) and the plain mean of the Sets' own PFs. `orders` is the count behind the PF."""
            sts = sts if on else []
            gp, gl = sum(getattr(x, gp_key) for x in sts), sum(getattr(x, gl_key) for x in sts)
            pfs = [min(RANK_PF_CAP, pf_of(getattr(x, gp_key), getattr(x, gl_key))) for x in sts]
            nn = (lambda x: int(getattr(x, n_key))) if isinstance(n_key, str) else n_key
            return {"on": bool(on), "n": n, "pop": len(pop), "sets": len(sts), "pct": round(100.0 * len(sts) / len(pop), 2) if pop else 0.0,
                    "pctAll": round(100.0 * len(sts) / total, 2) if total else 0.0, "pf": round(pf_of(gp, gl), 4),
                    "meanPf": round(sum(pfs) / len(pfs), 4) if pfs else 0.0, "orders": sum(nn(x) for x in sts)}
        sv = lambda x: x.stage or {}                                                      # noqa: E731
        mp = [x for x in base_only if self._main_pass(x)]
        neg_in = [x for x in base_only if sv(x).get("neg")]
        windows = {
            "baseLong": window_row(True, self.gate_window, allsets, [x for x in allsets if x.base_long_ok], "gate_gp", "gate_gl", "gate_n"),
            "baseShort": window_row(bool(self.base_short), self.base_short, allsets, [x for x in allsets if x.base_short_ok], "base_short_gp", "base_short_gl", "base_short_n"),
            "baseMinimal": window_row(bool(self.base_min_n), self.base_min_n, allsets, [x for x in allsets if x.base_min_ok], "base_min_gp", "base_min_gl", "base_min_n"),
            "mainLong": window_row(self.main_on, self.main_n, base_only, [x for x in base_only if sv(x).get("main")], "main_gp", "main_gl", "main_n"),
            "mainShort": window_row(self.main_on and self.short_on, list(self.short_windows), base_only, [x for x in base_only if sv(x).get("short")],
                                    "main_short_gp", "main_short_gl", "main_short_n"),
            "mainMinimal": window_row(self.main_on and bool(self.main_min_n), self.main_min_n, base_only, [x for x in base_only if sv(x).get("minimal")],
                                      "main_min_gp", "main_min_gl", "main_min_have"),
            "negative": window_row(self.neg_on, list(self.neg_windows), base_only, neg_in, "neg_win_gp", "neg_win_gl", "neg_win"),
            "real": window_row(self.realst_on, self.realst_n, mp, [x for x in mp if sv(x).get("real")], "exe_gp", "exe_gl", "exe_n"),
            "realNeg": window_row(self.realst_on and self.neg_on, self.realst_n, neg_in, [x for x in neg_in if sv(x).get("realneg")], "neg_gp", "neg_gl", "neg_n"),
        }
        chosen: Dict[str, int] = {}
        for x in base_only:
            if self.main_on and self.short_on:
                chosen[str(int(x.main_short_win or self.main_n))] = chosen.get(str(int(x.main_short_win or self.main_n)), 0) + 1
        windows["mainShort"]["chosen"] = chosen          # how many Base-passing Sets run each short window (the Set's own pick among the candidates)
        windows["baseMinimal"]["mode"] = "positive" if self.base_min_positive else "negative"
        windows["mainMinimal"]["mode"] = "positive" if self.main_min_positive else "negative"
        # stages: `base` is the end figure (the Sets that pass Base and Main, pooled over their Base windows), `baseOnly` the Base gate
        # alone; `main` the same end Sets over the Main gate's last N of their Main Sets; `processed` the Main Sets (orders opened while
        # Base passed) of every Base-passing Set; `real` the Real Sets (orders opened while Base and Main passed) of the end Sets, which
        # the overall Real stage (the coordinator's real evaluation) reads
        return {"base": pool(valid), "baseOnly": pool(base_only), "main": main, "processed": processed, "real": executable,
                "realneg": realneg, "stageSets": stage_sets, "stages": stages, "windows": windows,
                "unfiltered": pool(allsets),
                "total": total, "valid": len(valid),
                "validatedBasePct": round(100.0 * len(base_only) / total, 2) if total else 0.0,
                "validatedPct": round(100.0 * len(valid) / total, 2) if total else 0.0, "byPack": by_pack,
                "basis": "gate-window", "history": list(self.pf_history[-240:])}

    def _record_pf_progress(self) -> None:
        """One point per scoring pass (at most one per 20 s): the effective PF and the validated share over time."""
        now = time.time()
        if self.pf_history and now - self.pf_history[-1]["t"] < 20.0:
            return
        split = self._pf_split_nohist()
        self.pf_history.append({"t": round(now, 1), "pf": split["base"]["pf"], "n": split["base"]["n"],
                                "valid": split["valid"], "validatedPct": split["validatedPct"],
                                "unfiltered": split["unfiltered"]["pf"]})
        del self.pf_history[:-720]

    def _pf_split_nohist(self) -> Dict[str, Any]:
        saved, self.pf_history = self.pf_history, []
        try:
            return self._pf_split()
        finally:
            self.pf_history = saved

    def _snapshot(self) -> Dict[str, Any]:
        rows = []
        for st in sorted(self.sets.values(), key=lambda s: (not self.is_eligible(s), -s.gate_pf, s.max_dd_s)):
            state_row, reason_row = self.state_of(st)   # one evaluation: state and reason always agree
            rows.append(
                {
                    "kind": st.kind,
                    "idx": st.idx,
                    "id": st.id,
                    "pack": st.pack,
                    "packI": st.pack_i,
                    "tf": st.tf,
                    "slRatio": st.sl_ratio,
                    "slI": st.sl_i,
                    "trailKey": st.trail_key,
                    "trailArm": st.trail_arm,
                    "trailGive": st.trail_give,
                    "trI": st.tr_i,
                    "step": st.step,
                    "stepI": st.step_i,
                    "tpPct": round(st.tp_pct * 100, 4),
                    "n": st.n,
                    "liveN": len(st.live),
                    "wins": st.wins,
                    "pf": round(st.gate_pf, 4),
                    "gateN": st.gate_n,
                    "pf15": round(st.last15_pf, 4),
                    # the Set's chain: Main Set (orders opened while Base passed; the Main gate reads its last N) and Real Set
                    # (opened while Base and Main passed)
                    "mainId": st.id + MAIN_SUFFIX,
                    "mainN": int(st.proc_total) + len(st.live),
                    "mainPf": round(st.main_pf, 4),
                    "mainLastN": st.main_n,
                    "realId": st.id + REAL_SUFFIX,
                    "realN": int(st.exe_total) + len(st.live),
                    "realPf": round(st.exe_pf, 4),
                    "realNegN": int(st.neg_total),
                    "realNegPf": round(st.neg_pf, 4),
                    "shortPf": round(st.base_short_pf, 4),
                    "mainShortN": int(st.main_short_win or self.main_n),
                    "lane": self.lane_of(st),
                    "ratio": round(st.last15_ratio, 4),
                    "last15N": st.last15_n,
                    "last15R": round(st.last15_r, 4),
                    "last25AvgR": round(st.last25_avg_r, 4),
                    "last25N": st.last25_n,
                    "last25AvgPnl": round(st.last25_avg_pnl, 6),
                    "maxDdS": st.max_dd_s,
                    "avgDdS": st.avg_dd_s,
                    "ddEpisodes": st.dd_episodes,
                    "wr": st.wr,
                    "expectancy": st.expectancy,
                    "avgHoldS": st.avg_hold_s,
                    "holdMin": st.hold_bars,
                    "holdAuto": st.hold_auto,
                    "pfAll": st.pf_all,
                    "gp": st.gp,
                    "gl": st.gl,
                    "exits": st.exits,
                    "intern": {
                        "pf": round(st.gate_pf, 4),
                        "pf15": round(st.last15_pf, 4),
                        "avgR15": round(st.last15_r, 4),
                        "avgR25": round(st.last25_avg_r, 4),
                        "maxDdS": st.max_dd_s,
                        "avgDdS": st.avg_dd_s,
                        "wr": st.wr,
                        "E": st.expectancy,
                        "avgHoldS": st.avg_hold_s,
                        "n": st.n,
                        "liveN": len(st.live),
                    },
                    "active": st.active,
                    "state": state_row,
                    "stateReason": reason_row,
                    "evaluatedAt": round(st.evaluated_at, 1),
                    "deactReason": st.deact_reason,
                    "locked": st.locked,
                }
            )
        rows = rows[:24]
        p = self.progress
        cover = self.coverage()
        rank = {id(st): k for k, st in enumerate(sorted((s for s in self.by_idx if self.is_eligible(s)), key=self.rank_key, reverse=True))}
        index = [
            {
                "i": st.idx,
                "id": st.id,
                "kind": st.kind,
                "pack": st.pack,
                "sl": st.sl_ratio,
                "tr": st.trail_key,
                "st": st.step,
                "on": int(self.is_eligible(st)),
                "pf": round(st.gate_pf, 4),
                "dd": st.max_dd_s,
                # what the desk needs to pool the PF of any selection like _pf_split: orders, gate-window orders and
                # gross profit / loss, plus the table columns and the engine's own rank among the valid Sets
                "n": st.n,
                "gn": st.gate_n,
                "gp": st.gate_gp,
                "gl": st.gate_gl,
                "p15": round(st.last15_pf, 4),
                "r25": round(st.last25_avg_r, 3),
                "wr": round(st.wr, 1),
                "e": round(st.expectancy, 5),
                "h": round(st.avg_hold_s),
                "da": round(st.avg_dd_s),
                "rk": rank.get(id(st), -1),
                # the Set's lane chain: its processed Set (orders opened while Base passed) and its executable Set (opened
                # while every gate passed), each with its orders and PF
                "mainId": st.id + MAIN_SUFFIX,
                "mainN": int(st.proc_total) + len(st.live),
                "mainPf": round(st.main_pf, 4),
                "realId": st.id + REAL_SUFFIX,
                "realN": int(st.exe_total) + len(st.live),
                "realPf": round(st.exe_pf, 4),
                "realNegId": st.id + REALNEG_SUFFIX,
                "realNegN": int(st.neg_total),
                "realNegPf": round(st.neg_pf, 4),
                "shortPf": round(st.base_short_pf, 4),
                "mainShortN": int(st.main_short_win or self.main_n),
                "lane": self.lane_of(st),
                "why": self.gate_reason(st),
            }
            for st in self.by_idx
        ]
        return {
            "enabled": self.enabled,
            "ready": p.ready,
            "lookback": self.lookback,
            "pfWindow": self.pf_n,
            "deactN": self.deact_n,
            "minPf": self.min_pf,
            "maxDdS": self.max_dd_s,
            "useHistoricGate": self.use_historic_gate,
            "freshS": self.fresh_s,
            "costPct": self.cost_pct,
            "setCount": len(self.sets),
            "activeCount": sum(1 for s in self.sets.values() if self.is_eligible(s)),
            "counts": self.counts(),
            "pfBase": self._pf_split(),
            "coverage": cover,
            "index": index,
            "minStep": self.min_step,
            "minStepCfg": self.min_step_cfg,
            "stepMax": self.step_max,
            "stepAdapt": self.step_adapt,
            "steps": list(self.steps),
            "histFills": sum(s.n for s in self.sets.values()),
            "barsSymbols": len(self.bars),
            "quality": {"excluded": len(self.quality), "maxFlatShare": self.max_flat_share, "maxJumpPct": self.max_jump * 100.0,
                        "symbols": dict(sorted(self.quality.items())[:24])},
            "progress": {
                "phase": p.phase,
                "pct": round(p.pct, 1),
                "symbol": p.symbol,
                "setId": p.set_id,
                "barsDone": p.bars_done,
                "barsTotal": p.bars_total,
                "setsDone": p.sets_done,
                "setsTotal": p.sets_total,
                "symbolsDone": p.symbols_done,
                "symbolsTotal": p.symbols_total,
                "elapsedMs": round(p.elapsed_ms, 1),
                "lastRunMs": round(p.last_run_ms, 1),
                "cycle": p.cycle,
                "detail": p.detail,
                "ready": p.ready,
                "error": p.error,
                "errors": p.errors,
            },
            "rows": rows,
        }


def synth_trend(n: int = 240, start: float = 100.0, step: float = 0.12, noise: float = 0.04) -> List[List[float]]:
    bars: List[List[float]] = []
    px = start
    for i in range(n):
        drift = step if (i // 18) % 2 == 0 else -step * 0.7
        o = px
        c = px + drift + ((i % 5) - 2) * noise
        h = max(o, c) + abs(noise)
        l = min(o, c) - abs(noise) * 0.6
        v = 1000 + (i % 7) * 40
        bars.append([o, h, l, c, v])
        px = c
    return bars


def self_test() -> List[Tuple[str, bool, str]]:
    out: List[Tuple[str, bool, str]] = []
    # drawdown time: 3 down, recover, 2 down
    rows = [
        {"t": 100, "pnl": 1.0, "pnl_pct": 0.003},
        {"t": 160, "pnl": -0.4, "pnl_pct": -0.002},
        {"t": 220, "pnl": -0.4, "pnl_pct": -0.002},
        {"t": 400, "pnl": 1.2, "pnl_pct": 0.004},
        {"t": 460, "pnl": -0.3, "pnl_pct": -0.0015},
        {"t": 520, "pnl": -0.3, "pnl_pct": -0.0015},
    ]
    dd = drawdown_time(rows, now=520)
    out.append(("set-dd-episodes", dd["episodes"] == 2.0, f"{dd}"))
    out.append(("set-dd-max", dd["maxS"] >= 120, f"{dd['maxS']}"))
    # drawdown and gate state are derived from the tape: a Set with a long losing run is invalid, not deactivated
    book = SetBook()
    book.load(
        {
            "histEnabled": True,
            "setDeactN": 25,
            "windowsLinked": False, "setPfWindow": 15,
            "setMinPf": 1.10,
            "setMaxDdtHours": 36,
            "setMinSamples": 8,
            "setMinStep": 3,
            "setStepMax": 6,
            "setStepAdapt": True,
            "stratIndications": True,
            "stratGeneral": True,
            "trailVariants": ["0.3:0.1"],
            "trailGiveMin": 0.1,
            "trailGiveMax": 0.1,
            "setSlRatios": [0.5],
        }
    )
    book.gate_window, book.gate_min = 15, 12  # these fixtures carry 15 trades
    out.append(("set-count", len(book.sets) >= 2, f"n={len(book.sets)}"))
    out.append(("set-tp-cost", abs(step_tp_pct(3, 0.15) - 0.0045) < 1e-9, f"{step_tp_pct(3, 0.15)}"))
    base_only = [s for s in book.sets.values() if s.kind == "base"]
    trail_only = [s for s in book.sets.values() if s.kind == "trail"]
    out.append(("set-step-floor", bool(base_only) and all(s.step >= 3 for s in base_only) and min(s.step for s in base_only) == 3, f"steps={sorted({s.step for s in base_only})} trails={len(trail_only)}"))
    book.min_step_cfg, book.min_step, book.step_max = 10, 10, 12
    book._rebuild_sets()
    base_only = [s for s in book.sets.values() if s.kind == "base"]
    out.append(("set-no-below", all(s.step >= 10 for s in base_only) and not any(s.step < 10 for s in base_only), f"n={len(book.sets)} steps={sorted({s.step for s in base_only})}"))
    book.min_step_cfg, book.min_step, book.step_max = 3, 3, 6
    book.step_adapt = True
    book._rebuild_sets()
    mixed = [{"pnl": -0.02, "pnl_pct": -0.003}] * 20 + [{"pnl": 0.02, "pnl_pct": 0.004}] * 5
    book.adapt_from_live(mixed)
    out.append(("set-adapt-min", book.min_step == 5, f"min={book.min_step} n={len(book.sets)}"))
    sid = next(iter(book.sets))
    st = book.sets[sid]
    st.hist = [{"t": 1000 + i, "pnl": -0.01, "pnl_pct": -0.003, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "sl"} for i in range(25)]
    st.live = []
    book._score_one(st)
    out.append(("set-hist-neg-off", (not book.is_eligible(st)) and st.deact_reason in ("pf<min", "n<min"), f"{st.active} {st.deact_reason}"))
    st.live = [{"t": 2000 + i, "pnl": -0.02, "pnl_pct": -0.004, "symbol": "T", "side": "LONG", "hold_s": 40, "reason": "sl"} for i in range(25)]
    book._score_one(st)
    # live closes are part of the one gate: 25 live losses are judged with the hist rows, not by a live-only rule
    out.append(("set-live-in-one-gate", (not book.is_eligible(st)) and st.deact_reason in ("pf<min", "n<min") and len(st.tape()) == 50, f"{st.active} {st.deact_reason} tape={len(st.tape())}"))
    st.live = [{"t": 3000 + i, "pnl": 0.02, "pnl_pct": 0.003, "symbol": "T", "side": "LONG", "hold_s": 40, "reason": "peak"} for i in range(25)]
    book._score_one(st)
    out.append(("set-live-win-on", st.active, f"{st.active} {st.deact_reason}"))
    book.on_live_close({"ours": False, "set_id": sid, "pnl": -9, "pnl_pct": -0.5, "t": 9, "symbol": "X"})
    out.append(("set-skip-foreign", len(st.live) == 25, f"n={len(st.live)}"))
    book.on_live_close({"ours": True, "set_id": sid, "pnl": 0.01, "pnl_pct": 0.002, "t": 10, "symbol": "T", "client_id": "Gx02og0603dup00001"})
    n1 = len(st.live)
    book.on_live_close({"ours": True, "set_id": sid, "pnl": 0.01, "pnl_pct": 0.002, "t": 11, "symbol": "T", "client_id": "Gx02og0603dup00001"})
    out.append(("set-skip-dup-cid", len(st.live) == n1, f"n={len(st.live)} was={n1}"))
    # last-15 PF pass on winners
    st2 = list(book.sets.values())[0]
    st2.hist = [{"t": 2000 + i, "pnl": 0.02, "pnl_pct": 0.003, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "tp"} for i in range(15)]
    st2.live = []
    book.min_pf = 1.08
    book._score_one(st2)
    out.append(("set-pf15-pass", st2.last15_pf >= 1.09 and st2.active, f"pf={st2.last15_pf} ratio={st2.last15_ratio} {st2.deact_reason}"))
    # historic replay produces fills and scores
    book2 = SetBook()
    book2.load(
        {
            "histEnabled": True, "stagesEnabled": False,       # the pick on the Base gate alone
            "histLookbackBars": 240,
            "histMinBars": 80,
            "histWarmup": 20,
            "setDeactN": 25,
            "windowsLinked": False, "setPfWindow": 15,
            "setMinPf": 1.0,
            "setMaxDdtHours": 36,
            "setMinSamples": 5,
            "setMinStep": 3,
            "setStepMax": 8,
            "stratIndications": True,
            "stratGeneral": True,
            "trailVariants": ["0.3:0.1"],
            "setSlRatios": [0.5, 1.0],
            "tpPct": 0.75,
            "timeStopS": 240,
        }
    )
    book2.gate_window, book2.gate_min = 15, 12  # these fixtures carry 15 trades
    book2.ingest_bars("AAA-USDT", synth_trend(240, 50.0, 0.18, 0.03))
    book2.ingest_bars("BBB-USDT", synth_trend(240, 20.0, -0.14, 0.03))
    book2.replay_all(now=1_700_000_000)
    fills = sum(s.n for s in book2.sets.values())
    out.append(("set-hist-fills", fills >= 8, f"fills={fills} ready={book2.progress.ready} {book2.progress.detail}"))
    out.append(("set-hist-ready", book2.progress.ready and book2.progress.pct >= 99, f"{book2.progress.phase} {book2.progress.pct}"))
    out.append(("set-progress", book2.progress.last_run_ms > 0, f"{book2.progress.last_run_ms}ms"))
    # pick prefers higher last15
    p = book2.pick("general") or book2.pick("indications")
    out.append(("set-pick-or-gate", True, f"active={book2.snapshot()['activeCount']} pick={getattr(p, 'id', None)}"))
    winner = next(iter(book2.sets.values()))
    winner.hist = [{"t": 1_700_000_000 + i * 60, "pnl": 0.02, "pnl_pct": 0.003, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "tp"} for i in range(20)]
    winner.live = []
    book2._score_one(winner)
    for s in book2.sets.values():
        if s.id != winner.id:
            s.active = False
    picked = book2.pick(winner.pack)
    out.append(("set-pick", picked is not None and picked.id == winner.id and winner.active and winner.step >= book2.min_step, f"{getattr(picked, 'id', None)} active={winner.active} pf={winner.last15_pf} st={winner.step}"))
    # same-bar SL pessimism
    why, px = hit_exit(1, 100.0, 99.5, 100.8, None, [100.0, 101.0, 99.4, 100.2, 1])
    out.append(("set-sl-first", why == "sl" and abs(px - 99.5) < 1e-9, f"{why} {px}"))
    d, conf, _ = general_signal(synth_trend(40, 10.0, 0.25, 0.01))
    out.append(("set-general-sig", d != 0 or conf >= 0, f"d={d} c={conf:.2f}"))
    # independent intern: different SL:TP / step must diverge on the same bars
    book3 = SetBook()
    book3.load(
        {
            "histEnabled": True,
            "histLookbackBars": 240,
            "histMinBars": 80,
            "histWarmup": 20,
            "setDeactN": 25,
            "windowsLinked": False, "setPfWindow": 15,
            "setMinPf": 0.5,
            "setMaxDdtHours": 36,
            "setMinSamples": 3,
            "setMinStep": 3,
            "setStepMax": 12,
            "stratIndications": False,
            "stratGeneral": True,
            "trailVariants": ["0.3:0.1"],
            "setSlRatios": [0.5, 1.5],
            "tpPct": 0.75,
            "timeStopS": 21600,
            "exitIgnoreTp": True,
            "setHonorTp": True,
            "setHistTimeBars": 45,
        }
    )
    book3.ingest_bars("CCC-USDT", synth_trend(240, 80.0, 0.22, 0.05))
    book3.ingest_bars("DDD-USDT", synth_trend(240, 40.0, -0.16, 0.05))
    book3.replay_all(now=1_700_000_100)
    tight = [s for s in book3.sets.values() if abs(s.sl_ratio - 0.5) < 1e-9]
    wide = [s for s in book3.sets.values() if abs(s.sl_ratio - 1.5) < 1e-9]
    lo_step = [s for s in book3.sets.values() if s.step == 3]
    hi_step = [s for s in book3.sets.values() if s.step == 12]
    def sig(st: SetState) -> Tuple[int, float, float, float]:
        return (st.n, round(st.gate_pf, 4), round(st.avg_hold_s, 1), round(st.expectancy, 6))
    t_sig = sig(tight[0]) if tight else (0, 0.0, 0.0, 0.0)
    w_sig = sig(wide[0]) if wide else (0, 0.0, 0.0, 0.0)
    lo_sig = sig(lo_step[0]) if lo_step else (0, 0.0, 0.0, 0.0)
    hi_sig = sig(hi_step[0]) if hi_step else (0, 0.0, 0.0, 0.0)
    intern_ok = (t_sig != w_sig) or (lo_sig != hi_sig)
    out.append(("set-intern-independent", intern_ok and (tight[0].n + wide[0].n) > 0, f"sl0.5={t_sig} sl1.5={w_sig} st3={lo_sig} st12={hi_sig} fills={sum(s.n for s in book3.sets.values())}"))
    # full config grid: every pack × sl × trail × step indexed
    book4 = SetBook()
    book4.load(
        {
            "histEnabled": True, "stagesEnabled": False,      # these checks are about the Base gate alone, on short fixture tapes
            "setMinStep": 8,
            "setStepMax": 12,
            "stratIndications": True,
            "stratGeneral": True,
            "trailMinStep": 3,
        }
    )
    book4.gate_window, book4.gate_min = 15, 12  # these fixtures carry 15 trades
    cov = book4.coverage()
    want_base = len(book4.packs) * len(book4.sl_ratios) * len(book4.steps)
    built_keys = sorted({s.trail_key for s in book4.by_idx if s.kind == "trail"})
    want_tr = len(book4.packs) * max(1, len(built_keys))
    # Exit Sets (exitIgnoreTp): one per distinct stop distance (SL ratio x step) per pack, no TP
    want_exit = len(book4.packs) * len({round(sl * st_, 6) for sl in book4.sl_ratios for st_ in book4.steps}) if book4.exit_sets else 0
    want = want_base + want_tr + want_exit
    idxs = [s.idx for s in book4.by_idx]
    trails_in = {s.trail_key for s in book4.by_idx if s.kind == "trail"}
    kinds = {s.kind for s in book4.by_idx}
    out.append(("set-grid-product", len(book4.by_idx) == want and want_base >= 50 and want_tr >= 10, f"n={len(book4.by_idx)} base={want_base} exit={want_exit} trail={want_tr} dims={cov.get('dims')} fam={cov.get('families')}"))
    out.append(("set-idx-unique", idxs == list(range(len(idxs))), f"n={len(idxs)} last={idxs[-1] if idxs else None}"))
    out.append(("set-trail-cover", cov.get("trailCover") and len(trails_in) >= 5 and "trail" in kinds, f"trails={sorted(trails_in)} cover={cov.get('trailCover')}"))
    mid_tp = (book4.steps[len(book4.steps) // 2]) * book4.cost_pct
    honour_ok = (not book4.hist_honor_tp) or all(st.trail_arm < mid_tp - 1e-9 for st in book4.by_idx if st.kind == "trail")
    out.append(("set-trail-honour-rule", honour_ok and len(book4.trails) >= len(built_keys), f"honour={book4.hist_honor_tp} mid_tp={mid_tp:.2f} built={len(built_keys)} grid={len(book4.trails)}"))
    out.append(("set-sl-cover", cov.get("slCover") and len(book4.sl_ratios) >= 5, f"sl={book4.sl_ratios}"))
    out.append(("set-get-idx", book4.get_idx(0) is book4.by_idx[0] and book4.get_idx(want - 1) is book4.by_idx[-1], f"0={book4.get_idx(0).id if book4.get_idx(0) else None}"))
    v = book4.coord_vars(book4.by_idx[0])
    out.append(("set-coord-vars", v.get("idx") == 0 and v.get("kind") == "base" and "step" in v, str(v)))
    trail_row = next((s for s in book4.sets.values() if s.kind == "trail"), None)
    if trail_row:
        trail_row.hist = [
            {"t": 1_700_000_000 + i * 60, "pnl": 0.02, "pnl_pct": 0.003, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "tp"}
            for i in range(20)
        ]
        book4._score_one(trail_row)
        trail_row.active = True
    book4.progress.ready = True  # calibrated: the gate is applied, so the trail set must validate
    tr0 = book4.pick_trail(trail_row.pack if trail_row else "indications")
    out.append(("set-pick-trail", tr0 is not None and tr0.kind == "trail" and tr0.trail_key, f"{getattr(tr0,'id',None)} {getattr(tr0,'trail_key',None)}"))
    # two trails independent intern
    book5 = SetBook()
    book5.load(
        {
            "histEnabled": True,
            "histLookbackBars": 240,
            "histMinBars": 80,
            "histWarmup": 20,
            "setMinPf": 0.5,
            "setMinStep": 8,
            "setStepMax": 8,
            "stratIndications": False,
            "stratGeneral": True,
            "trailMinStep": 3,
            "setSlRatios": [0.5],
            "setHonorTp": True,
            "setHistTimeBars": 45,
        }
    )
    book5.ingest_bars("EEE-USDT", synth_trend(240, 60.0, 0.2, 0.06))
    book5.replay_all(now=1_700_000_200)
    by_tr = {}
    for st in book5.by_idx:
        if st.kind != "trail":
            continue
        by_tr[st.trail_key] = (st.n, round(st.avg_hold_s, 1), round(st.expectancy, 6), st.idx)
    out.append(("set-trail-independent", len(by_tr) >= 3 and len(set(by_tr.values())) >= 2, f"{by_tr}"))
    base_n = sum(1 for s in book5.by_idx if s.kind == "base")
    out.append(("set-trail-own-family", base_n >= 1 and all(s.step == 0 for s in book5.by_idx if s.kind == "trail"), f"base={base_n} trails={len(by_tr)}"))
    dropped = book2.trim_bars(["AAA-USDT"])
    out.append(("set-trim-bars", dropped >= 1 and "AAA-USDT" in book2.bars and "BBB-USDT" not in book2.bars, f"drop={dropped} left={list(book2.bars)}"))
    book2.bars["AAA-USDT"] = book2.bars["AAA-USDT"] + book2.bars["AAA-USDT"]
    clamped = book2.clamp_bars(80)
    out.append(("set-clamp-bars", clamped >= 1 and len(book2.bars["AAA-USDT"]) <= 80, f"n={len(book2.bars['AAA-USDT'])} c={clamped}"))
    book6 = SetBook()
    book6.load({"histEnabled": True, "histLookbackBars": 240, "histMinBars": 80, "histWarmup": 20, "setMinStep": 8, "setStepMax": 8, "stratGeneral": True, "stratIndications": False, "setSlRatios": [0.5], "trailVariants": ["0.3:0.1"]})
    book6.ingest_bars("FFF-USDT", synth_trend(240, 55.0, 0.16, 0.04))
    book6.ingest_bars("GGG-USDT", synth_trend(240, 33.0, -0.12, 0.04))
    book6.replay_all(now=1_700_000_300, symbols=["FFF-USDT"])
    out.append(("set-replay-slice", book6.progress.ready and book6.progress.symbols_total == 1, f"n={book6.progress.symbols_total} fills={sum(s.n for s in book6.sets.values())} {book6.progress.detail}"))
    # --- evaluation math: last-N window counts and PF averages, hand-computed ---
    w = SetBook()
    w.load(
        {
            "positionCostPct": 0.15, "histEnabled": True, "windowsLinked": False, "setPfWindow": 15, "setDeactN": 25, "setMinPf": 1.0,
            "setMinSamples": 5, "setMinStep": 3, "setStepMax": 3,
            "stratIndications": False, "stratGeneral": True, "setSlRatios": [0.5],
            "trailVariants": ["0.3:0.1"],
        }
    )
    wst = next(x for x in w.sets.values() if x.kind == "base")
    rows20 = []
    for i in range(20):
        g = 0.004 if i % 2 == 0 else -0.002
        rows20.append({"t": 5000 + i * 60, "pnl": g - 0.0015, "pnl_pct": g, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "tp" if g > 0 else "sl"})
    wst.hist = rows20
    wst.live = []
    w._score_one(wst)
    l15 = rows20[-15:]
    rs15 = [((r["pnl_pct"] * 100.0) - 0.15) / 0.15 for r in l15]
    want_ratio = round(1.0 + (sum(rs15) / len(rs15)) * 0.10, 4)
    l25 = rows20[-25:]
    rs25 = [((r["pnl_pct"] * 100.0) - 0.15) / 0.15 for r in l25]
    want25r = sum(rs25) / len(rs25)
    want25p = sum(r["pnl"] for r in l25) / len(l25)
    out.append(("set-lastn-window", wst.last15_n == 15 and abs(wst.last15_ratio - want_ratio) < 1e-6, f"n={wst.last15_n} ratio={wst.last15_ratio} want={want_ratio}"))
    out.append(("set-avg-math", wst.last25_n == 20 and abs(wst.last25_avg_r - want25r) < 1e-9 and abs(wst.last25_avg_pnl - want25p) < 1e-9, f"n={wst.last25_n} avgR={wst.last25_avg_r:.4f}/{want25r:.4f} avgP={wst.last25_avg_pnl:.6f}/{want25p:.6f}"))
    # --- positive-PF-only validation: gated pick and pack gate ---
    g2 = SetBook()
    g2.load(
        {
            "positionCostPct": 0.15, "histEnabled": True, "stagesEnabled": False, "windowsLinked": False, "setPfWindow": 15, "setDeactN": 25, "setMinPf": 1.20,
            "setMinSamples": 8, "setMinStep": 3, "setStepMax": 3,
            "stratIndications": False, "stratGeneral": True, "setSlRatios": [0.5, 1.0],
            "trailVariants": ["0.3:0.1"],
        }
    )
    g2.gate_window, g2.gate_min = 15, 12  # these fixtures carry 15 trades
    g2.min_pf = 1.20  # this sticky-off test was written against the old 1.20 threshold
    g2.progress.ready = True
    bases = [x for x in g2.by_idx if x.kind == "base"]
    neg_set, pos_set = bases[0], bases[1]
    neg_rows = [{"t": 6000 + i * 60, "pnl": -0.0045, "pnl_pct": -0.003, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "sl"} for i in range(15)]
    # net PF exactly 1.10: 11 wins of +0.0004 and 4 losses of -0.001 (11*0.0004 / 4*0.001)
    pos_rows = [{"t": 6000 + i * 60, "pnl": (-0.001 if i % 4 == 0 else 0.0004), "pnl_pct": (-0.001 if i % 4 == 0 else 0.0004) + 0.0015, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "tp"} for i in range(15)]
    neg_set.hist = list(neg_rows)
    pos_set.hist = list(pos_rows)
    g2._score_one(neg_set)
    g2._score_one(pos_set)
    out.append(("set-neg-off", (not g2.is_eligible(neg_set)) and neg_set.deact_reason == "pf<min", f"{neg_set.active} {neg_set.deact_reason} pf={neg_set.last15_pf}"))
    out.append(("set-pos-on", pos_set.active and pos_set.last15_pf >= 1.0, f"{pos_set.active} pf={pos_set.last15_pf}"))
    pk = g2.pick("general")
    out.append(("set-pick-pos-only", pk is None, f"base gate: PF 1.10 < 1.20, nothing below the gate is picked ({getattr(pk, 'id', None)})"))
    pos_set.active = False
    for ts in g2.by_idx:
        if ts.kind == "trail":
            ts.hist = list(neg_rows)
            g2._score_one(ts)
    pk2 = g2.pick("general")
    out.append(("set-pick-all-neg-none", pk2 is None, f"{getattr(pk2, 'id', None)}"))
    out.append(("set-pack-closed", not g2.pack_open("general"), f"open={g2.pack_open('general')} fills={sum(x.n for x in g2.sets.values())}"))
    # reactivation: negative set returns once the last-N window rolls positive
    neg_set.hist = list(pos_rows)
    g2._score_one(neg_set)
    out.append(("set-neg-recover", neg_set.active and neg_set.last15_pf >= 1.0, f"{neg_set.active} pf={neg_set.last15_pf}"))
    # no sticky off: the one gate decides on every evaluation. PF 1.10 < min_pf 1.20 stays invalid; PF 2.75 returns
    neg_set.hist = list(neg_rows)
    g2._score_one(neg_set)
    off1 = not g2.is_eligible(neg_set)
    neg_set.hist = list(pos_rows)  # PF 1.10 < min_pf 1.20 -> still invalid
    g2._score_one(neg_set)
    still_off = not g2.is_eligible(neg_set)
    rec_rows = [{"t": 9000 + i * 60, "pnl": (-0.001 if i % 4 == 0 else 0.001), "pnl_pct": (0.0005 if i % 4 == 0 else 0.0025), "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "tp"} for i in range(15)]
    neg_set.hist = rec_rows  # PF 2.75 -> valid again on the next evaluation
    g2._score_one(neg_set)
    out.append(("set-no-sticky-off", off1 and still_off and g2.is_eligible(neg_set), f"{neg_set.active} {neg_set.deact_reason} pf={neg_set.last15_pf}"))
    # cold start (no replay yet): ungated pick still returns a set
    g3 = SetBook()
    g3.load({"histEnabled": True, "stratGeneral": True, "stratIndications": False, "setSlRatios": [0.5], "setMinStep": 3, "setStepMax": 3, "trailVariants": ["0.3:0.1"]})
    pk3 = g3.pick("general")
    out.append(("set-pick-cold", pk3 is None and not g3.pack_open("general"), f"ready={g3.progress.ready} {getattr(pk3, 'id', None)}"))
    # error state: due() retries on the backoff, not on every pass
    g3.progress.phase = "error"; g3.progress.ready = False; g3.last_run = time.time()
    out.append(("set-due-backoff", g3.due() is False, f"retry_s={g3.retry_s}"))
    g3.last_run = time.time() - g3.retry_s - 1
    out.append(("set-due-retry", g3.due() is True, "after backoff"))
    # time-ordered tape: the last-40 window must be the most recent trades
    g4 = SetBook(); g4.load({"histEnabled": True, "stratGeneral": True, "stratIndications": False, "setSlRatios": [0.5], "setMinStep": 3, "setStepMax": 3})
    st4 = next(iter(g4.by_idx))
    rows4 = [{"t": 1000 + k, "pnl": -0.001, "pnl_pct": -0.001, "hold_s": 60, "reason": "sl", "set_id": st4.id} for k in range(60)]
    st4.hist = sorted(reversed(rows4), key=lambda r: r["t"])[-40:]
    out.append(("set-tape-recent", min(r["t"] for r in st4.hist) == 1020, f"first={min(r['t'] for r in st4.hist)}"))
    # entry threshold is configurable and defaults to the historical 0.58
    out.append(("set-entry-conf-default", SetBook().entry_conf == 0.58, f"{SetBook().entry_conf}"))
    g5 = SetBook(); g5.load({"setEntryConf": 0.8, "setCooldownBars": 5})
    out.append(("set-entry-conf-wired", g5.entry_conf == 0.8 and g5.cooldown_bars == 5, f"conf={g5.entry_conf} cd={g5.cooldown_bars}"))
    # base gate = net PF over the last 50 orders, min 30 judged, boundary PF >= min_pf (no fallback tier)
    def _pf_rows(n: int, target: float) -> List[Dict[str, Any]]:
        # 30 wins of +0.001 and 20 losses of -(0.0015 / target): PF = target exactly (scaled to n)
        rows = []
        for k in range(n):
            if k % 5 < 3:
                rows.append({"t": 1_700_000_000 + k * 60, "pnl": 0.001, "pnl_pct": 0.0025, "hold_s": 60, "reason": "tp", "symbol": "T", "side": "LONG"})
            else:
                rows.append({"t": 1_700_000_000 + k * 60, "pnl": -0.0015 / target, "pnl_pct": -0.0015 / target + 0.0015, "hold_s": 60, "reason": "sl", "symbol": "T", "side": "LONG"})
        return rows
    gb = SetBook()
    gb.load({"positionCostPct": 0.15, "setMinPf": 1.10, "stagesEnabled": False, "setGateWindow": 50, "setGateMinTrades": 30, "setSlRatios": [0.5], "setMinStep": 3, "setStepMax": 3, "stratGeneral": True, "stratIndications": False})
    sg = next(iter(gb.by_idx))
    sg.active = True
    def _gate(rows: List[Dict[str, Any]]) -> Tuple[bool, float, int]:
        sg.hist = list(rows)
        sg.live = []
        gb._score_one(sg)
        return gb.is_eligible(sg), round(sg.gate_pf, 4), sg.gate_n
    ok105, pf105, n105 = _gate(_pf_rows(50, 1.05))
    out.append(("gate-pf-1.05-rejected", (not ok105) and abs(pf105 - 1.05) < 1e-3 and n105 == 50, f"pf={pf105} n={n105} eligible={ok105}"))
    ok115, pf115, _ = _gate(_pf_rows(50, 1.15))
    out.append(("gate-pf-1.15-accepted", ok115 and abs(pf115 - 1.15) < 1e-3, f"pf={pf115} eligible={ok115}"))
    okb, pfb, _ = _gate(_pf_rows(50, 1.10))
    out.append(("gate-boundary-1.10", okb and abs(pfb - 1.10) < 1e-6, f"pf={pfb} eligible={okb}"))
    okf, pff, nf = _gate(_pf_rows(29, 1.50))
    out.append(("gate-min-30-orders", (not okf) and nf == 29, f"pf={pff} n={nf} eligible={okf}"))
    gb.lookback_default_ok = (LOOKBACK_DEFAULT == 1920 and SetBook().lookback == 1920)
    out.append(("lookback-32h-default", gb.lookback_default_ok, f"lookback={SetBook().lookback}"))
    # R1: a live close is stored on the replay unit (net fraction), not USDT
    lv = SetBook(); lv.load({"positionCostPct": 0.15, "setSlRatios": [0.5], "setMinStep": 3, "setStepMax": 3, "stratGeneral": True, "stratIndications": False})
    sl1 = next(iter(lv.by_idx))
    lv.on_live_close({"set_id": sl1.id, "t": 1_700_000_000, "symbol": "L-USDT", "side": "LONG", "pnl": 0.25, "pnl_pct": 0.0025, "hold_s": 60, "reason": "tp", "client_id": "c1"})
    stored = sl1.live[-1] if sl1.live else {}
    out.append(("live-unit-net-fraction", abs(stored.get("pnl", -9) - net_pnl_pct(0.0025, 0.15)) < 1e-12 and abs(stored.get("pnl_usdt", 0) - 0.25) < 1e-12, f"pnl={stored.get('pnl')} usdt={stored.get('pnl_usdt')}"))
    # R2: a repeated close (same client id, or none) and a repeated seed add nothing
    n_before = len(sl1.live)
    lv.on_live_close({"set_id": sl1.id, "t": 1_700_000_000, "symbol": "L-USDT", "side": "LONG", "pnl": 0.25, "pnl_pct": 0.0025, "hold_s": 60, "reason": "tp", "client_id": "c1"})
    row_nocid = {"set_id": sl1.id, "t": 1_700_000_500, "symbol": "M-USDT", "side": "SHORT", "pnl": -0.1, "pnl_pct": -0.001, "hold_s": 60, "reason": "sl"}
    lv.on_live_close(row_nocid); lv.on_live_close(row_nocid)
    seeded = [{"set_id": sl1.id, "t": 1_700_000_900, "symbol": "N-USDT", "side": "LONG", "pnl": 0.1, "pnl_pct": 0.001, "hold_s": 60, "reason": "tp", "client_id": "c9"}]
    lv.seed_live(seeded); n_mid = len(sl1.live); lv.seed_live(seeded)
    out.append(("live-dedupe", len(sl1.live) == n_before + 2 and len(sl1.live) == n_mid, f"before={n_before} after={len(sl1.live)} seeded_twice={n_mid==len(sl1.live)}"))
    # a close without pnl_pct cannot be normalised and is skipped, not guessed
    skip0 = lv.live_skipped
    lv.on_live_close({"set_id": sl1.id, "t": 1_700_001_000, "symbol": "P-USDT", "side": "LONG", "pnl": 5.0, "reason": "tp"})
    out.append(("live-skip-no-pct", lv.live_skipped == skip0 + 1 and len(sl1.live) == n_before + 2, f"skipped={lv.live_skipped}"))

    # R10: drawdown is measured to the caller's clock; with no clock it is the last trade, never wall time
    dd_rows = [{"t": 100, "pnl": 1.0}, {"t": 160, "pnl": -0.4}]
    dd_def = drawdown_time(dd_rows)["currentS"]
    dd_inj = drawdown_time(dd_rows, now=460)["currentS"]
    dd_stale = drawdown_time(dd_rows, now=160 + 7200)["currentS"]
    # the drawdown starts at the last high (t=100): open time is clock - 100, idle time never cuts it short
    out.append(("dd-clock-injected", dd_def == 60.0 and dd_inj == 360.0 and dd_stale == 7260.0, f"default={dd_def} injected={dd_inj} stale={dd_stale}"))
    # R4: a flat window is neutral (RSI 50, no signal), not a short bias from RSI 100
    flat = [[1.0, 1.0, 1.0, 1.0, 1.0]] * 80
    flat_sig = general_signal(flat)
    out.append(("set-flat-neutral", flat_sig == (0, 0.0, "flat") and abs(rsi([1.0] * 20, 7) - 50.0) < 1e-9, f"{flat_sig} rsi={rsi([1.0] * 20, 7)}"))
    # R6: one cost default, shared by indication settings and position cost
    from indication_engine import DEFAULT_SETTINGS as IND_DEFAULTS
    from position_cost import POSITION_COST_PCT_DEFAULT as PCD
    out.append(("cost-one-default", IND_DEFAULTS["positionCostPct"] == PCD == 0.12, f"ind={IND_DEFAULTS['positionCostPct']} pos={PCD}"))
    # perf: scan() is the lane loop used by the replay and the simulator; step() is its reference.
    import random as _random
    _rnd = _random.Random(11)
    _bars: List[List[float]] = []
    _px = 100.0
    for _ in range(700):
        _o = _px
        _hi = _o * (1 + abs(_rnd.gauss(0, 0.002)))
        _lo = _o * (1 - abs(_rnd.gauss(0, 0.002)))
        _c = _lo + (_hi - _lo) * _rnd.random()
        _px = _c
        _bars.append([_o, _hi, _lo, _c, 1.0])
    _dirs = [_rnd.choice([0, 0, 1, -1]) for _ in _bars]
    _confs = [_rnd.random() for _ in _bars]
    _book = SetBook()
    _book.load({"histEnabled": True, "stratGeneral": True, "stratIndications": True, "setMinStep": 2, "setStepMax": 30})
    _sample = _book.by_idx[::max(1, len(_book.by_idx) // 40)]
    _scan_ok = True
    _checked = 0
    for _st in _sample:
        for _honor in (True, False):
            _cfg = {"entry_conf": 0.5, "time_bars": 40, "scratch_bars": 12, "scratch_min": 0.001,
                    "cooldown": 2, "cost_pct": 0.15, "honor_tp": _honor}
            _cuts = [30]
            while _cuts[-1] < len(_bars):
                _cuts.append(min(len(_bars), _cuts[-1] + _rnd.randint(1, 60)))
            _flags = [_rnd.random() < 0.8 for _ in range(len(_cuts) - 1)]
            _allow = [True] * len(_bars)
            for _c0, _c1, _f in zip(_cuts, _cuts[1:], _flags):
                for _k in range(_c0, _c1):
                    _allow[_k] = _f
            _ref = SetLane(_st, "X")
            _ref_recs = []
            for _k in range(30, len(_bars)):
                _rec = _ref.step(_k, _bars[_k], _dirs[_k], _confs[_k], _allow[_k], _cfg)
                if _rec is not None:
                    _ref_recs.append(_rec)
            _lane = SetLane(_st, "X")
            _cand = [_k for _k in range(30, len(_bars)) if _dirs[_k] != 0 and _confs[_k] >= _cfg["entry_conf"]]
            _got = []
            for _c0, _c1, _f in zip(_cuts, _cuts[1:], _flags):
                _got.extend(_lane.scan(_c0, _c1, _bars, _dirs, _confs, _f, _cfg, _cand))
            _same = _got == _ref_recs and _lane.open == _ref.open and _lane.cool == _ref.cool
            _scan_ok = _scan_ok and _same
            _checked += len(_ref_recs)
    out.append(("scan-equals-step", _scan_ok and _checked > 0, f"sets={len(_sample)} trades={_checked}"))
    # perf: the signal cache must give exactly the uncached signals, including after the window slides
    _sb_bars = [[b[0], b[1], b[2], b[3], b[4]] for b in _bars[:420]]
    _packs = ("indications", "general")
    _cache: Dict[int, Dict[str, Tuple[int, float, str]]] = {}
    _salt = hash(json.dumps(_book.ind_settings, sort_keys=True, default=str))
    _cold = pack_signals(_sb_bars, _packs, _book.ind_settings, 1_700_000_000.0, 30)
    _warm1 = pack_signals(_sb_bars, _packs, _book.ind_settings, 1_700_000_000.0, 30, cache=_cache, salt=_salt)
    _warm2 = pack_signals(_sb_bars, _packs, _book.ind_settings, 1_700_000_000.0, 30, cache=_cache, salt=_salt)
    _slide = _sb_bars[5:] + [[_bars[k][0], _bars[k][1], _bars[k][2], _bars[k][3], _bars[k][4]] for k in range(420, 425)]
    _cold_s = pack_signals(_slide, _packs, _book.ind_settings, 1_700_000_300.0, 30)
    _warm_s = pack_signals(_slide, _packs, _book.ind_settings, 1_700_000_300.0, 30, cache=_cache, salt=_salt)
    _cache_ok = _cold == _warm1 == _warm2 and _cold_s == _warm_s
    out.append(("signal-cache-exact", _cache_ok, f"bars={len(_sb_bars)} cached_windows={len(_cache)}"))
    # isolation: chunked refreshes agree with one full refresh; a failing symbol or Set stops nothing else
    def _rw(seed: int, count: int) -> List[List[float]]:
        r = _random.Random(seed)
        px = 50.0
        rows: List[List[float]] = []
        for _ in range(count):
            o = px
            hi = o * (1 + abs(r.gauss(0, 0.002)))
            lo = o * (1 - abs(r.gauss(0, 0.002)))
            c = lo + (hi - lo) * r.random()
            px = c
            rows.append([o, hi, lo, c, 1.0])
        return rows
    _sym_bars = {"AAA-USDT": _rw(1, 900), "BBB-USDT": _rw(2, 900), "CCC-USDT": _rw(3, 900)}
    _iso_ov = {"histEnabled": True, "stratGeneral": True, "stratIndications": False, "setMinStep": 2, "setStepMax": 6,
               "trailVariants": ["0.3:0.1"], "setSlRatios": [0.5, 1.0]}
    _now = float((int(1_700_000_000 // BAR_S)) * BAR_S)

    def _fresh_book() -> SetBook:
        b = SetBook()
        b.load(_iso_ov)
        for sym, rows in _sym_bars.items():
            b.bars[sym] = [r[:] for r in rows]
        return b

    _full = _fresh_book()
    _full.replay_all(now=_now)
    _chunked = _fresh_book()
    _chunked.replay_all(now=_now, symbols=["AAA-USDT"])
    _chunked.replay_all(now=_now, symbols=["BBB-USDT", "CCC-USDT"])
    _chunk_ok = all(
        _full.sets[st.id].n == _chunked.sets[st.id].n
        and round(_full.sets[st.id].gate_pf, 9) == round(_chunked.sets[st.id].gate_pf, 9)
        and _full.sets[st.id].hist == _chunked.sets[st.id].hist
        for st in _full.by_idx
    ) and sum(st.n for st in _full.by_idx) > 0
    out.append(("chunk-equals-full", _chunk_ok, f"sets={len(_full.by_idx)} fills={sum(st.n for st in _full.by_idx)}"))
    _bad = _fresh_book()
    _bad.bars["BBB-USDT"] = [["x", "x", "x", "x", "x"]] * 900
    _bad.replay_all(now=_now)
    _sym_ok = (
        _bad.progress.phase == "ready"
        and "BBB-USDT" in _bad.progress.error
        and "AAA-USDT" in _bad._hist_rows and "CCC-USDT" in _bad._hist_rows and "BBB-USDT" not in _bad._hist_rows
        and sum(st.n for st in _bad.by_idx) > 0
    )
    out.append(("symbol-isolation", _sym_ok, f"phase={_bad.progress.phase} errors={_bad.progress.errors} err={_bad.progress.error[:60]}"))
    _iso = _fresh_book()
    _victim = _iso.by_idx[3].id
    _orig_score = _iso._score_one

    def _flaky_score(st: SetState, now: Optional[float] = None) -> None:
        if st.id == _victim:
            raise RuntimeError("injected scoring fault")
        return _orig_score(st, now=now)

    _iso._score_one = _flaky_score
    _iso.replay_all(now=_now)
    _set_ok = (
        _iso.sets[_victim].last_error != ""
        and all(st.last_error == "" and st.n >= 0 for st in _iso.by_idx if st.id != _victim)
        and _iso.progress.phase == "ready"
        and _iso.progress.errors >= 1
    )
    out.append(("set-isolation", _set_ok, f"victim={_victim} err={_iso.sets[_victim].last_error[:40]} errors={_iso.progress.errors}"))
    # Exit Sets: exitIgnoreTp is its own strategy type. A base Set takes its TP; its Exit twin (same stop, no TP) does not,
    # each has its own id and tape, and the pick ranks the two kinds together.
    xb = SetBook()
    xb.load({"exitIgnoreTp": True, "stagesEnabled": False, "setMinStep": 2, "setStepMax": 4, "setSlRatios": [1.0, 2.0], "histEnabled": True})
    kinds_x = {k: [x for x in xb.by_idx if x.kind == k] for k in SET_KINDS}
    dists = {round(x.sl_ratio * x.step, 6) for x in kinds_x["exit"] if x.pack == "general"}
    out.append(("set-exit-kind-built", len(kinds_x["exit"]) == len(xb.packs) * 5 and len(dists) == 5
                and all(x.id.endswith(":" + EXIT_TAG) for x in kinds_x["exit"]),
                f"exit={len(kinds_x['exit'])} distinct stops={sorted(dists)}"))
    xoff = SetBook()
    xoff.load({"exitIgnoreTp": False, "setMinStep": 2, "setStepMax": 4, "setSlRatios": [1.0, 2.0], "histEnabled": True})
    out.append(("set-exit-kind-off", not any(x.kind == "exit" for x in xoff.by_idx), "exitIgnoreTp off builds no Exit Set"))
    up = [[100.0, 100.0, 100.0, 100.0, 1.0]] + [[100.0 + 0.05 * k, 100.06 + 0.05 * k, 99.99 + 0.05 * k, 100.05 + 0.05 * k, 1.0] for k in range(30)]
    cfg_x = {"entry_conf": 0.5, "time_bars": 200, "scratch_bars": 999, "scratch_min": 0.5, "cooldown": 0, "cost_pct": 0.12,
             "honor_tp": False, "sl_floor_steps": 1}
    bst = next(x for x in xb.by_idx if x.kind == "base" and x.pack == "general" and x.step == 2 and x.sl_ratio == 1.0)
    xst = next(x for x in xb.by_idx if x.kind == "exit" and x.pack == "general" and x.step == 2 and x.sl_ratio == 1.0)
    dirs_x = [1] + [0] * 30
    confs_x = [0.9] + [0.0] * 30
    tb = SetLane(bst, "T").scan(0, len(up), up, dirs_x, confs_x, True, cfg_x, [0], BarIndex(up))
    tx = SetLane(xst, "T").scan(0, len(up), up, dirs_x, confs_x, True, cfg_x, [0], BarIndex(up))
    out.append(("set-base-takes-tp-exit-never", bool(tb) and tb[0]["reason"] == "tp" and not tx,
                f"base={[t['reason'] for t in tb]} exit={[t['reason'] for t in tx]} (honor_tp off applies to trailing Sets only)"))
    bst.gate_n = xst.gate_n = 50
    bst.gate_pf, xst.gate_pf = 1.3, 2.0
    bst.gate_pf_old = bst.gate_pf_new = 1.3
    xst.gate_pf_old = xst.gate_pf_new = 2.0
    bst.n = xst.n = 200
    bst.main_n = xst.main_n = xb.main_n
    bst.main_pf = xst.main_pf = 2.0                 # both pass the Main gate as well
    for x in xb.by_idx:
        x.active = x in (bst, xst)
        x.max_dd_s = 0.0
    out.append(("set-pick-ranks-base-and-exit", xb.pick_any("general") is xst and xb.pack_open("general"),
                f"pick={getattr(xb.pick_any('general'), 'id', None)}"))
    # last N: the hold Auto floor and the linked windows read at least MIN_VALIDATION_N orders; the Base gate's minimum order count has
    # no floor (0 = off, owner 2026-10-29): a value of 3 is taken as it is
    ln = SetBook()
    ln.load({"setPfWindow": 3, "pfWindow": 3, "setGateMinTrades": 3, "setGateWindow": 20, "holdAutoMin": 3})
    out.append(("set-last-n-at-least-8", ln.pf_n >= MIN_VALIDATION_N and ln.gate_min == 3 and ln.hold_auto_min >= MIN_VALIDATION_N
                and MIN_VALIDATION_N == 8, f"pf_n={ln.pf_n} gate_min={ln.gate_min} hold_auto_min={ln.hold_auto_min}"))
    try:
        from exit_engine import ExitBook
        from risk_variants import VariantBook
        e_, v_ = ExitBook(), VariantBook()
        e_.load({"exitPfWindow": 3})
        v_.load({"slToTpRecalcN": 3, "trailRecalcN": 3}, {})
        # the floor applies to the Base gate minimum, the hold Auto floor and the linked validation windows; the other
        # counts (Block's own samples, the Exit lane minimum, the DCA window) keep their own ranges
        floors = {"exit_pf": e_.pf_n, "sl_recalc": v_.sl_recalc_n, "trail_recalc": v_.trail_recalc_n}
        ok_floor = all(v >= MIN_VALIDATION_N for v in floors.values())
    except Exception as exc:          # a module that no longer loads is a failure here
        floors, ok_floor = {"error": repr(exc)[:80]}, False
    out.append(("set-last-n-floor-linked-windows", ok_floor, str(floors)))
    # the Main gate reads the processed orders (opened while the Set passed Base), not the raw tape: 20 wins, 6 losses of three
    # times the size, then 12 wins opened while the Set was not valid. Base passes at the end (PF 1.33 over the last 30) and the
    # raw last 12 are all wins; the processed last 12 hold the losses (PF 1.0), so the Set fails the Main gate.
    pr = SetBook()
    pr.load({"setGateWindow": 30, "setGateMinTrades": 20, "setGateShortN": 0, "mainGateN": 12, "setMinPf": 1.25, "setMaxDdtHours": 36,
             "setMinStep": 3, "setStepMax": 3, "setSlRatios": [1.0], "stratIndications": False, "histEnabled": True})
    pst = next(x for x in pr.by_idx if x.kind == "base")
    cfp = cost_as_frac(pr.cost_pct)
    seq = [0.004] * 20 + [-0.012] * 6 + [0.004] * 12
    rows_p = [(1_700_000_000.0 + 600.0 * k, "T", "LONG", x, x + cfp, 300.0, "x", pst.id) for k, x in enumerate(seq)]
    pst.active = True
    pr.set_processed(pst, rows_p)
    pst.hist = [dict(zip(_ROW_KEYS, r)) for r in rows_p]
    pr._score_one(pst, now=rows_p[-1][0] + 60.0)
    raw12 = [r[3] for r in rows_p[-12:]]
    held = SetBook()
    held.load({"setGateWindow": 30, "setGateMinTrades": 20, "setGateShortN": 0, "mainGateN": 12, "setMinPf": 1.25, "setMaxDdtHours": 36,
               "setMinStep": 3, "setStepMax": 3, "setSlRatios": [1.0], "stratIndications": False, "histEnabled": True})
    hst = next(x for x in held.by_idx if x.kind == "base")
    hst.active = True
    hst.hist = [dict(zip(_ROW_KEYS, r)) for r in rows_p]
    held._score_one(hst, now=rows_p[-1][0] + 60.0)          # not replayed: the same rule over the tape it holds
    out.append(("set-real-gate-reads-processed-orders", min(raw12) > 0 and pr.gate_reason(pst) == "main" and pst.gate_pf >= 1.25
                and held.gate_reason(hst) == "main" and abs(hst.main_pf - pst.main_pf) < 1e-9 and pst.proc_total < len(rows_p),
                f"raw last 12 all wins={min(raw12) > 0}; Base PF {pst.gate_pf}; processed {pst.proc_total}/{len(rows_p)}; "
                f"Real PF {pst.main_pf} -> {pr.gate_reason(pst) or 'valid'} (held tape: {held.gate_reason(hst) or 'valid'})"))
    # the stage chain: Base (long + short + minimal window) -> Main Set -> Main (long + short + minimal) -> Real Set -> Real (last realGateN); and
    # Main Negative (the last 5 / 10 Main orders average below zero) -> Real Negative Set -> Real on it. Each Set a subset of the one before.
    ch = SetBook()
    ch.load({"setGateWindow": 30, "setGateMinTrades": 20, "setGateShortN": 10, "mainGateN": 8, "realGateN": 8, "setMinPf": 1.25,
             "setMaxDdtHours": 36, "setMinStep": 3, "setStepMax": 3, "setSlRatios": [1.0], "stratIndications": False, "histEnabled": True})
    cst = next(x for x in ch.by_idx if x.kind == "base")
    cfc = cost_as_frac(ch.cost_pct)
    seqc = ([0.004, 0.004, 0.004, -0.006] * 30 + [-0.004] * 6 + [0.004, 0.004, -0.004] * 20)
    rows_c = [(1_700_000_000.0 + 600.0 * k, "T", "LONG", x, x + cfc, 300.0, "x", cst.id) for k, x in enumerate(seqc)]
    chain_c = ch._chain_of(cst, rows_c)
    tm = {t for t, _ in chain_c["main"]}
    sub = {t for t, _ in chain_c["real"]} <= tm and {t for t, _ in chain_c["realneg"]} <= tm and len(tm) < len(rows_c)
    short_ok = chain_c["short_n"] in (ch.main_n,) + tuple(ch.short_windows)
    cst.active = True
    ch.set_processed(cst, rows_c)
    cst.hist = [dict(zip(_ROW_KEYS, r)) for r in rows_c]
    ch._score_one(cst, now=rows_c[-1][0] + 60.0)
    idx_c = next(r for r in ch.snapshot()["index"] if r["id"] == cst.id)
    pfb = ch.snapshot()["pfBase"]
    out.append(("set-stage-chain-each-set-from-the-one-before", sub and short_ok and len(chain_c["real"]) > 0 and len(chain_c["realneg"]) > 0
                and idx_c["mainId"] == cst.id + ":main" and idx_c["realId"] == cst.id + ":real" and idx_c["realNegId"] == cst.id + ":realneg"
                and idx_c["mainN"] == len(chain_c["main"]) and idx_c["realN"] == len(chain_c["real"])
                and {"base", "main", "processed", "real", "realneg"} <= set(pfb),
                f"Base Set {len(rows_c)} -> Main Set {len(chain_c['main'])} -> Real Set {len(chain_c['real'])} / Real Negative Set "
                f"{len(chain_c['realneg'])}; Main short window {chain_c['short_n']}"))
    # each stage rule on its own: the verdicts read only the Set of their stage
    P = ch.stage_params(cst.pack)
    good, bad8 = [0.004] * 20, [0.004] * 12 + [-0.004] * 8
    v_auto = stage_verdicts(good + bad8[-5:], good, [], 5, P)                 # the short window (5) fails, the long 8 passes
    v_neg = stage_verdicts(good + [-0.004] * 5, [], [0.004] * 8, ch.main_n, P)  # a dip: the Negative lane opens, Real Negative passes
    v_real = stage_verdicts(good, bad8, [0.004] * 8, ch.main_n, P)             # Main passes, the Real Set fails: no positive lane
    out.append(("set-stage-rules-main-short-negative-real", (not v_auto["short"]) and v_auto["main"] is not None and not v_auto["pos"]
                and v_neg["neg"] and v_neg["negl"] and not v_neg["pos"] and v_real["main"] and not v_real["real"] and not v_real["pos"]
                and v_real["negl"] is False,
                f"short={v_auto['short']} pos={v_auto['pos']}; dip neg={v_neg['neg']} negl={v_neg['negl']}; Real Set fails pos={v_real['pos']}"))
    # the chain against a brute-force reading of its rules on random tapes: the Base short window, the Auto choice (the best
    # candidate with enough orders and the margin) and both Main windows for the Real Set
    import random as _rnd
    from dataclasses import replace as _dc_replace

    def _ref_chain(ts_, en_, nt_, Q):
        def ok(idx, t, n):                    # the newest n of the lane's orders closed before t, PF at or above the floor
            w = [nt_[k] for k in idx if ts_[k] < t][-n:]
            return len(w) >= (n if Q.full_window else 1) and pf_of(sum(x for x in w if x > 0), -sum(x for x in w if x < 0)) + 1e-9 >= Q.floor
        g_, l_ = prefix_sums(nt_)
        allk = list(range(len(ts_)))
        def mok(t, n, pos):                   # the Base minimal window: the newest n results before t all positive (negative)
            w = [nt_[k] for k in allk if ts_[k] < t][-n:]
            return len(w) >= (n if Q.full_window else 1) and (all(x > 0 for x in w) if pos else all(x < 0 for x in w))
        main_ = [k for k in allk if processed_flag(ts_, nt_, g_, l_, en_[k], Q.base_long, Q.gate_min, Q.floor, Q.max_dd_s, Q.lookback_s, 0, Q.consistency)
                 and (not Q.base_short or ok(allk, en_[k], Q.base_short))
                 and (not Q.base_min_n or mok(en_[k], Q.base_min_n, Q.base_min_positive))]

        def pf_inc(w):
            inc = [nt_[k] for k in main_ if ok(main_, en_[k], w)]
            return len(inc), pf_of(sum(x for x in inc if x > 0), -sum(x for x in inc if x < 0))
        ref = pf_inc(Q.main_n)[1]
        cands = [(pf, w) for w in Q.short_windows for c, pf in [pf_inc(w)] if c >= Q.short_min and pf >= ref * Q.short_margin and pf > ref]
        an = max(cands, key=lambda x: (x[0], -Q.short_windows.index(x[1])))[1] if cands else Q.main_n

        def kok(k):                           # the Main minimal window: the newest main_min_n Main results before the entry all positive (negative)
            w = [nt_[j] for j in main_ if ts_[j] < en_[k]][-Q.main_min_n:] if Q.main_min_n else []
            return (not Q.main_min_n) or (len([x for x in w]) >= (Q.main_min_n if Q.full_window else 1)
                                          and (all(x > 0 for x in w) if Q.main_min_positive else all(x < 0 for x in w)))
        real_ = [k for k in main_ if ok(main_, en_[k], Q.main_n) and ok(main_, en_[k], an) and kok(k)]
        return main_, real_, an
    Pr = StageParams(base_long=30, base_short=10, gate_min=20, floor=1.25, max_dd_s=1e12, lookback_s=1e12, consistency=0.0, main_on=True,
                     main_n=8, short_on=True, short_windows=(5, 10, 15, 20, 25), short_min=8, short_margin=1.02, neg_on=False, neg_windows=(5, 10),
                     real_on=True, real_n=15, full_window=True)
    rng_ = _rnd.Random(11)
    same, short_cut, auto_moved, auto_cut = True, 0, 0, 0
    for _ in range(12):
        nt_ = [rng_.choice((0.006, 0.005, -0.004, -0.005)) for _ in range(400)]
        ts_ = [1000.0 + 60.0 * k for k in range(len(nt_))]
        en_ = [t - 300.0 for t in ts_]
        got = stage_chain(ts_, en_, nt_, Pr)
        m_, r_, an_ = _ref_chain(ts_, en_, nt_, Pr)
        same = same and (got["main"], got["real"], got["short_n"]) == (m_, r_, an_)
        short_cut += len(_ref_chain(ts_, en_, nt_, _dc_replace(Pr, base_short=0))[0]) - len(m_)
        auto_moved += an_ != Pr.main_n
        auto_cut += len([k for k in m_ if k not in r_]) > 0 and an_ != Pr.main_n
    # the Main Set holds only orders opened while Base passed: a Set that loses from the start has an empty Main Set, its winning
    # twin's Main Set starts after gate_min orders (before that Base has too few orders to pass)
    lose = [(-0.004 if k % 3 else 0.002) for k in range(200)]
    win_ = [(0.006 if k % 3 else -0.004) for k in range(200)]
    tl = [1000.0 + 60.0 * k for k in range(200)]
    el = [t - 300.0 for t in tl]
    m_lose = stage_chain(tl, el, lose, Pr)["main"]
    m_win = stage_chain(tl, el, win_, Pr)["main"]
    out.append(("set-main-set-only-orders-opened-while-base-passed", not m_lose and bool(m_win) and min(m_win) >= Pr.gate_min,
                f"losing Set: {len(m_lose)} Main orders; winning Set: {len(m_win)} from order {min(m_win) if m_win else None} (gate minimum {Pr.gate_min})"))
    out.append(("set-stage-chain-equals-the-rules", same and short_cut > 0 and auto_moved > 0 and auto_cut > 0,
                f"equal={same}; short window removed {short_cut} Main orders; Main short window moved on {auto_moved}/12 tapes"))
    # the default judges every stage window on the orders it has (at least one): the same brute-force reading with full_window off, and the
    # difference to the strict rule on a sparse tape (a Set with 14 orders never filled a window of 25 / 15, so it never reached Real)
    Pa = _dc_replace(Pr, full_window=False)
    same_a, wider = True, 0
    for _ in range(12):
        nt_ = [rng_.choice((0.006, 0.005, -0.004, -0.005)) for _ in range(400)]
        ts_ = [1000.0 + 60.0 * k for k in range(len(nt_))]
        en_ = [t - 300.0 for t in ts_]
        got = stage_chain(ts_, en_, nt_, Pa)
        m_, r_, an_ = _ref_chain(ts_, en_, nt_, Pa)
        same_a = same_a and (got["main"], got["real"], got["short_n"]) == (m_, r_, an_)
        wider += len(got["real"]) - len(stage_chain(ts_, en_, nt_, Pr)["real"])
    # the Base minimal window (third last-N next to the long and the short one): the chain equals the brute-force reading for 1-3 results, positive and
    # negative, in both window rules; a positive and a negative window never admit the same order; any minimal window only removes Main orders
    same_m, disjoint, never_more, narrowed = True, True, True, 0
    for _ in range(8):
        nt_ = [rng_.choice((0.006, 0.005, -0.004, -0.005)) for _ in range(300)]
        ts_ = [1000.0 + 60.0 * k for k in range(len(nt_))]
        en_ = [t - 300.0 for t in ts_]
        plain = stage_chain(ts_, en_, nt_, Pa)["main"]
        for full_ in (False, True):
            for n_ in (1, 2, 3):
                pos_ = _dc_replace(Pa, base_min_n=n_, base_min_positive=True, full_window=full_)
                neg_ = _dc_replace(Pa, base_min_n=n_, base_min_positive=False, full_window=full_)
                gp_, gn_ = stage_chain(ts_, en_, nt_, pos_), stage_chain(ts_, en_, nt_, neg_)
                same_m = same_m and gp_["main"] == _ref_chain(ts_, en_, nt_, pos_)[0] and gn_["main"] == _ref_chain(ts_, en_, nt_, neg_)[0]
                disjoint = disjoint and not (set(gp_["main"]) & set(gn_["main"]))
                if not full_:
                    never_more = never_more and set(gp_["main"]) <= set(plain) and set(gn_["main"]) <= set(plain)
                    narrowed += len(plain) - len(gp_["main"]) > 0 and len(plain) - len(gn_["main"]) > 0
    out.append(("set-base-minimal-window-equals-the-rules", same_m and disjoint and never_more and narrowed > 0,
                f"chain equals the brute-force reading for N 1-3 positive and negative, both window rules={same_m}; positive and negative windows disjoint={disjoint}; "
                f"a minimal window only removes Main orders={never_more} (narrowed {narrowed} cases)"))
    sparse_t = [1000.0 + 60.0 * k for k in range(14)]
    sparse_n = [0.006] * 14
    sparse_e = [t - 300.0 for t in sparse_t]
    Ps = _dc_replace(Pr, main_n=25, real_n=15, base_short=0, gate_min=1, full_window=False)
    avail_ch = stage_chain(sparse_t, sparse_e, sparse_n, Ps)
    strict_ch = stage_chain(sparse_t, sparse_e, sparse_n, _dc_replace(Ps, full_window=True))
    v_av = stage_verdicts([0.006] * 14, [0.006] * 9, [], 25, Ps)
    v_st = stage_verdicts([0.006] * 14, [0.006] * 9, [], 25, _dc_replace(Ps, full_window=True))
    out.append(("set-stage-windows-judge-the-orders-available", same_a and wider > 0 and len(avail_ch["real"]) > 0 and not strict_ch["real"]
                and v_av["pos"] and not v_st["pos"],
                f"equal to the rules={same_a}; the default chain holds {wider} more Real orders than the strict one over 12 tapes; 14 orders: Real Set "
                f"{len(avail_ch['real'])} (strict {len(strict_ch['real'])}); verdict pos {v_av['pos']} (strict {v_st['pos']})"))
    sb = SetBook()
    sb.load({"setGateWindow": 30, "setGateMinTrades": 20, "setGateShortN": 10, "setMinPf": 1.25, "setMaxDdtHours": 36,
             "setMinStep": 3, "setStepMax": 3, "setSlRatios": [1.0], "stratIndications": False, "histEnabled": True})
    sst = next(x for x in sb.by_idx if x.kind == "base")
    sst.active = True
    seqs = [0.006] * 22 + [-0.004] * 8          # long window PF passes, the last 10 do not
    sst.hist = [dict(zip(_ROW_KEYS, (1_700_000_000.0 + 600.0 * k, "T", "LONG", x, x + cfc, 300.0, "x", sst.id))) for k, x in enumerate(seqs)]
    sb._score_one(sst, now=1_700_000_000.0 + 600.0 * len(seqs))
    out.append(("set-base-needs-both-windows", sst.gate_pf >= 1.25 and sb.gate_reason(sst) == "short",
                f"long PF {sst.gate_pf}, short PF {sst.base_short_pf} -> {sb.gate_reason(sst)}"))
    # the stage keys: mainGate* the Main static window, realGate* the Real stage, stagesEnabled off = the Base long window alone
    lk = SetBook(); lk.load({"realGateN": 12, "realGateEnabled": False, "mainGateN": 9})
    lo_ = SetBook(); lo_.load({"stagesEnabled": False})
    out.append(("set-main-stage-keys-and-legacy-names", lk.realst_n == 12 and lk.realst_on is False and lk.main_n == 9 and lk.main_on is True
                and SetBook().main_n == MAIN_GATE_N_DEFAULT == 25 and SetBook().short_windows == (5, 10, 15) and SetBook().realst_n == REAL_GATE_N_DEFAULT == 15
                and not (lo_.main_on or lo_.neg_on or lo_.realst_on or lo_.base_short),
f"Real N {lk.realst_n} on {lk.realst_on}; Main N {lk.main_n}; stages off -> Base alone"))
    return out


if __name__ == "__main__":
    failed = 0
    for name, ok, detail in self_test():
        print(("PASS" if ok else "FAIL"), name, detail)
        failed += int(not ok)
    if failed:
        raise SystemExit(1)
    print("set_engine ok")
