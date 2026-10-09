#!/usr/bin/env python3
"""Independent config Sets: 1m historic replay, base gate, max DD time, per-Set lifecycle.

A Set is one (pack × SL:TP ratio × trail) book. Historic walks 1-minute OHLC,
simulates entries/exits, then scores each Set on its own tape. Live closes
merge into the same book. One rule decides validity (gate_reason): PF over the
last setGateWindow orders at or above setMinPf, at least setGateMinTrades orders,
max drawdown time below setMaxDdtHours, and a fresh evaluation. An invalid Set
keeps being scored and becomes valid again on its own; only a lock is sticky.
"""
from __future__ import annotations

import bisect
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from config_num import num
from position_cost import (
    GATE_MIN_PF_DEFAULT,
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
    cost_as_frac,
    net_pnl_pct,
    snap_sl_ratio,
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
from risk_variants import parse_trail, trail_grid, trail_key

PACKS = ("indications", "general")
GENERAL_WINDOW_BARS = 60     # general pack reads the newest 60 one-minute bars
IND_WINDOW_BARS = 300        # indication pack: 15m lane needs 20 candles = 300 one-minute bars
DEACT_N_DEFAULT = 25
PF_N_DEFAULT = 15
LOOKBACK_DEFAULT = 1920          # 32 h of 1m bars (systemwide default; was 960 = 16 h)
# dedupe keys kept for live closes: a close re-delivered after this many newer closes is counted again
LIVE_SEEN_MAX = 20000
GATE_WINDOW_DEFAULT = 50        # gate PF over each Set's last N closed orders
GATE_MIN_DEFAULT = 30           # fewer orders than this: not judged, not eligible
WARMUP_DEFAULT = 30
BAR_S = 60.0
FEE_PCT = 0.001  # round-trip, matches live close_pos
STEP_MIN = TP_STEP_MIN
STEP_MAX = TP_STEP_MAX
HIST_CAP = 80
# trail Sets carry no SL grid: their stop is TRAIL_SL_RATIO x their mid-step TP (documented family constant)
TRAIL_SL_RATIO = 0.6


def _join_bars(old: Sequence[Sequence[float]], new: Sequence[Sequence[float]]) -> List[List[float]]:
    """Join a newer 1-minute window onto a stored series. Rows carry no timestamps, so the join finds where the new
    window's first row sits in the stored series and checks the overlap row by row. The stored history before that
    point is kept, and the new rows (including a still-forming last bar) replace the overlapped tail. With no overlap
    the new window stands, because a gap cannot be filled from rows that do not line up."""
    if not old:
        return [list(r) for r in new]
    head = list(new[0])
    for p in range(len(old) - 1, -1, -1):
        if list(old[p]) == head:
            span = min(len(new), len(old) - p)
            if all(list(old[p + i]) == list(new[i]) for i in range(span - 1)):
                return [list(r) for r in old[:p]] + [list(r) for r in new]
            break
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


def indication_signal(bars: Sequence[Sequence[float]], settings: Dict[str, Any], now: float) -> Tuple[int, float, str]:
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
    if not votes:
        return 0, 0.0, "flat"
    long_w = sum(c for d, c, _ in votes if d > 0)
    short_w = sum(c for d, c, _ in votes if d < 0)
    if long_w > short_w and long_w >= 0.6:
        return 1, min(1.0, long_w / max(1, len(votes))), "+".join(w for d, _, w in votes if d > 0)
    if short_w > long_w and short_w >= 0.6:
        return -1, min(1.0, short_w / max(1, len(votes))), "+".join(w for d, _, w in votes if d < 0)
    return 0, max(long_w, short_w), "split"


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
    for i in range(warmup, n):
        key = None
        if cache is not None:
            lo_w = max(0, i + 1 - IND_WINDOW_BARS)
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
        ts = base_ts + i * BAR_S
        row: Dict[str, Tuple[int, float, str]] = {}
        if "general" in packs:
            lo = i + 1 - GENERAL_WINDOW_BARS
            row["general"] = general_signal(bars[lo if lo > 0 else 0 : i + 1])
        if "indications" in packs:
            lo = i + 1 - IND_WINDOW_BARS
            row["indications"] = indication_signal(bars[lo if lo > 0 else 0 : i + 1], ind_settings, ts)
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


class SetLane:
    """One (Set, symbol) trade state machine. The only implementation of the bar loop: the engine
    replay (SetBook._replay_symbol) and the streaming simulator both call step()."""
    __slots__ = ("st", "symbol", "sid", "open", "cool", "use_trail", "arm", "give", "sl_base", "tp_frac")

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
        self.sl_base = st.tp_pct * (st.sl_ratio if st.kind == "base" else TRAIL_SL_RATIO)
        self.tp_frac = st.tp_pct

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
            why, px = hit_exit(side, entry, o["sl"], o["tp"], o.get("trail"), bar, ignore_tp=not cfg["honor_tp"])
            if why is None and held >= cfg["time_bars"]:
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
        sl_frac = max(cost_frac, self.sl_base)           # a stop is never tighter than one PositionCost
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
    ) -> List[Dict[str, Any]]:
        """Advance over bars[i0:i1]. Same rules as step(), applied to the whole range in one loop.

        cand: sorted bar indices where an entry is possible (dir != 0 and conf >= entry_conf). A flat lane
        jumps between them; the bars in between can only spend cooldown, which is applied in one step.
        While a position is open its state is held in locals and written back when it closes or the
        range ends. Returns the closed trades in exit order; open position and cooldown carry across calls.
        """
        thr = cfg["entry_conf"]
        if cand is None:
            cand = [i for i in range(i0, i1) if dirs[i] != 0 and confs[i] >= thr]
        time_bars = cfg["time_bars"]
        scratch_bars = cfg["scratch_bars"]
        scratch_min = cfg["scratch_min"]
        cooldown = cfg["cooldown"]
        cost_pct = cfg["cost_pct"]
        cost_frac = cost_as_frac(cost_pct)
        honor_tp = bool(cfg["honor_tp"])
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
                sl_frac = max(cost_frac, sl_base)
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

class SetBook:
    def __init__(self) -> None:
        self.enabled = True
        self.lookback = LOOKBACK_DEFAULT
        self.min_bars = 120
        self.warmup = WARMUP_DEFAULT
        self.refresh_s = 90.0
        self.pf_n = PF_N_DEFAULT
        self.deact_n = DEACT_N_DEFAULT
        self.min_pf = GATE_MIN_PF_DEFAULT
        self.gate_window = GATE_WINDOW_DEFAULT
        self.gate_min = GATE_MIN_DEFAULT
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
        # replayed trade rows per symbol: a refresh replaces only the symbols it replayed
        self._hist_rows: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        self.bars: Dict[str, List[List[float]]] = {}
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
        self.lookback = max(120, min(2880, int(ov.get("histLookbackBars") or LOOKBACK_DEFAULT)))
        self.min_bars = max(60, min(self.lookback, int(ov.get("histMinBars") or 120)))
        self.warmup = max(16, min(80, int(ov.get("histWarmup") or WARMUP_DEFAULT)))
        self.refresh_s = max(30.0, min(600.0, float(ov.get("histRefreshS") or 90)))
        self.pf_n = max(5, min(50, int(ov.get("setPfWindow") or ov.get("pfWindow") or PF_N_DEFAULT)))
        self.deact_n = max(10, min(80, int(ov.get("setDeactN") or DEACT_N_DEFAULT)))
        self.gate_window = max(10, min(200, int(ov.get("setGateWindow") or GATE_WINDOW_DEFAULT)))
        self.gate_min = max(5, min(self.gate_window, int(ov.get("setGateMinTrades") or GATE_MIN_DEFAULT)))
        self.min_pf = float(ov.get("setMinPf") if ov.get("setMinPf") is not None else GATE_MIN_PF_DEFAULT)
        # max drawdown time: the longest drawdown over the last setGateWindow positions must be LOWER than this many
        # hours. setMaxDdtHours is 2..36 in steps of 2, default 12. max_dd_s holds the threshold in seconds.
        hrs = ov.get("setMaxDdtHours")
        hrs = 12.0 if hrs is None else float(hrs)
        hrs = max(2.0, min(36.0, round(hrs / 2.0) * 2.0))
        self.max_ddt_h = hrs
        self.max_dd_s = hrs * 3600.0
        self.use_historic_gate = bool(ov.get("setUseHistoricGate", True))
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
        self.packs = packs or ["indications"]
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
        self.trail_min_step = max(TRAIL_STEP_SETTING_MIN, min(TRAIL_STEP_SETTING_MAX, int(float(ov.get("trailMinStep") or TRAIL_STEP_DEFAULT))))
        raw_tr = ov.get("trailVariants")
        if raw_tr:
            if isinstance(raw_tr, str):
                raw_tr = [p.strip() for p in raw_tr.split(",") if p.strip()]
            self.trails = [(trail_key(a, g), a, g) for a, g in (parse_trail(x) for x in raw_tr)]
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
        }
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
            st.idx = idx
            next_sets[st.id] = st
            by_idx.append(st)
            idx += 1
        for pack_i, pack in enumerate(self.packs):
            for sl_i, sl in enumerate(self.sl_ratios):
                for step_i, step in enumerate(self.steps):
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
            for tr_i, (tkey, arm, give) in enumerate(trails):
                sid = make_trail_id(pack, tkey)
                prev = keep.get(sid)
                mid_step = self.steps[len(self.steps) // 2] if self.steps else 8
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
        self.progress.sets_total = len(self.sets)

    def adapt_from_live(self, closed: Sequence[Any]) -> None:
        with self._lock:
            self._adapt_from_live(closed)

    def _adapt_from_live(self, closed: Sequence[Any]) -> None:
        """If live average is a loss, raise min step to # of positive/successful fills."""
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

    def ingest_bars(self, symbol: str, bars: Sequence[Sequence[float]]) -> None:
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
            self.bars[symbol] = _join_bars(self.bars.get(symbol) or [], cleaned)[-self.lookback :]

    def trim_bars(self, keep: Sequence[str]) -> int:
        want = set(keep)
        n = 0
        for s in list(self.bars):
            if s not in want:
                self.bars.pop(s, None)
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
                names = [s for s, b in self.bars.items() if len(b) >= self.min_bars]
            else:
                names = [s for s in symbols if len(self.bars.get(s) or []) >= self.min_bars]
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
                with self._lock:
                    self._hist_rows[symbol] = per_set
                self.progress.bars_done += len(self.bars[symbol])
                if on_step:
                    on_step()
            self.progress.phase = "score"
            self.progress.pct = 90.0
            cutoff = now - self.lookback * BAR_S
            with self._lock:
                live = {s for s, b in self.bars.items() if len(b) >= self.min_bars}
                for s in [s for s in self._hist_rows if s not in live]:
                    del self._hist_rows[s]          # symbols that left the universe drop their rows
                for st in self.by_idx:
                    try:
                        rows = [
                            r
                            for s, per_set in self._hist_rows.items()
                            for r in per_set.get(st.id, [])
                            if finite(r.get("t")) >= cutoff
                        ]
                        full = sorted(rows, key=lambda r: finite(r.get("t")))
                        st.hist = full[-max(40, self.gate_window):]
                        st.last_error = ""          # cleared before scoring: a clean score is the only way back to valid
                        self._score_one(st, now=now)
                        st.n = len(full)
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

    def _replay_symbol(self, symbol: str, hist: Dict[str, List[Dict[str, Any]]], now: float, on_step: Optional[Callable[[], None]] = None) -> None:
        bars = self.bars[symbol]
        n = len(bars)
        warmup = min(self.warmup, max(16, n // 5))
        base_ts = now - (n - 1) * BAR_S
        salt = hash(json.dumps(self.ind_settings, sort_keys=True, default=str))
        cache = self._sig_cache.setdefault(symbol, {})
        signals = pack_signals(bars, self.packs, self.ind_settings, base_ts, warmup, on_step, cache=cache, salt=salt)
        cfg = {
            "entry_conf": self.entry_conf,
            "time_bars": max(8, min(self.hist_time_bars, max(8, n - warmup - 1))),
            "scratch_bars": max(8, int(self.scratch_s / BAR_S)),
            "scratch_min": self.scratch_min,
            "cooldown": self.cooldown_bars,
            "cost_pct": self.cost_pct,
            "honor_tp": bool(getattr(self, "hist_honor_tp", True)),
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
        for st in self.by_idx:
            if st.pack not in self.packs:
                continue
            lane = SetLane(st, symbol)
            for done in lane.scan(warmup, n, bars, dirs[st.pack], confs[st.pack], True, cfg, cand[st.pack]):
                hist[st.id].append({"t": base_ts + done["exit_i"] * BAR_S, "symbol": symbol, "side": done["side"],
                                    "pnl": done["pnl"], "pnl_pct": done["pnl_pct"], "hold_s": done["hold_s"],
                                    "reason": done["reason"], "set_id": st.id})
            self.progress.set_id = st.id

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
        if st.gate_pf + 1e-9 < self.min_pf:
            return "pf<min"
        if st.max_dd_s >= self.max_dd_s:
            return "ddt"
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
        trails = [t[0] for t in (self.trails_built or self.trails or [])]
        trail_sets = [st for st in self.by_idx if st.kind == "trail"]
        base_sets = [st for st in self.by_idx if st.kind == "base"]
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
            "families": {"base": len(base_sets), "trail": len(trail_sets)},
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
            rows = [s for s in self.by_idx if s.pack == pack and s.kind == kind and self.is_eligible(s)]
            rows.sort(key=lambda s: (s.gate_pf, s.last25_avg_r, -s.max_dd_s, s.n), reverse=True)
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
        rows.sort(key=lambda s: (s.gate_pf, s.last25_avg_r, -s.max_dd_s, s.n), reverse=True)
        return rows[0]

    def pick_trail(self, pack: str) -> Optional[SetState]:
        return self.pick(pack, kind="trail")

    def pick_any(self, pack: str) -> Optional[SetState]:
        return self.pick(pack, "base") or self.pick(pack, "trail")

    def pack_open(self, pack: str) -> bool:
        if not self.enabled or not self.use_historic_gate:
            return True
        return bool(self.valid_for(pack, "base") or self.valid_for(pack, "trail"))

    def counts(self) -> Dict[str, Any]:
        """Every configured Set is in exactly one of valid, invalid, locked, error. The retired Sets are counted apart."""
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
        rows = []
        for st in sorted(self.sets.values(), key=lambda s: (not self.is_eligible(s), -s.gate_pf, s.max_dd_s)):
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
                    "state": self.state_of(st)[0],
                    "stateReason": self.state_of(st)[1],
                    "evaluatedAt": round(st.evaluated_at, 1),
                    "deactReason": st.deact_reason,
                    "locked": st.locked,
                }
            )
        rows = rows[:24]
        p = self.progress
        cover = self.coverage()
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
            "coverage": cover,
            "index": index,
            "minStep": self.min_step,
            "minStepCfg": self.min_step_cfg,
            "stepMax": self.step_max,
            "stepAdapt": self.step_adapt,
            "steps": list(self.steps),
            "histFills": sum(s.n for s in self.sets.values()),
            "barsSymbols": len(self.bars),
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
            "setPfWindow": 15,
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
            "histEnabled": True,
            "histLookbackBars": 240,
            "histMinBars": 80,
            "histWarmup": 20,
            "setDeactN": 25,
            "setPfWindow": 15,
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
            "setPfWindow": 15,
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
            "histEnabled": True,
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
    want = want_base + want_tr
    idxs = [s.idx for s in book4.by_idx]
    trails_in = {s.trail_key for s in book4.by_idx if s.kind == "trail"}
    kinds = {s.kind for s in book4.by_idx}
    out.append(("set-grid-product", len(book4.by_idx) == want and want_base >= 50 and want_tr >= 10, f"n={len(book4.by_idx)} base={want_base} trail={want_tr} dims={cov.get('dims')} fam={cov.get('families')}"))
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
            "histEnabled": True, "setPfWindow": 15, "setDeactN": 25, "setMinPf": 1.0,
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
            "histEnabled": True, "setPfWindow": 15, "setDeactN": 25, "setMinPf": 1.20,
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
    gb.load({"setMinPf": 1.10, "setGateWindow": 50, "setGateMinTrades": 30, "setSlRatios": [0.5], "setMinStep": 3, "setStepMax": 3, "stratGeneral": True, "stratIndications": False})
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
    out.append(("cost-one-default", IND_DEFAULTS["positionCostPct"] == PCD == 0.15, f"ind={IND_DEFAULTS['positionCostPct']} pos={PCD}"))
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
    return out


if __name__ == "__main__":
    failed = 0
    for name, ok, detail in self_test():
        print(("PASS" if ok else "FAIL"), name, detail)
        failed += int(not ok)
    if failed:
        raise SystemExit(1)
    print("set_engine ok")
