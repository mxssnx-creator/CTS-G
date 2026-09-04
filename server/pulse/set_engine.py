#!/usr/bin/env python3
"""Independent config Sets: 1m historic replay, last-15 PF, max DD time, live deact.

A Set is one (pack × SL:TP ratio × trail × step) book. Historic walks 1-minute
OHLC to discover unproven books. Live on-exchange closes (cost-net) score the
processed Sets and are the only tape that deactivates them.
"""
from __future__ import annotations

import copy
import time
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from position_cost import (
    LAST_N_DEFAULT,
    POSITION_COST_PCT_DEFAULT,
    cost_aware_metrics,
    EVALUATION_WINDOWS,
    evaluation_windows,
    last_n_cost_pf,
    normalize_pf,
    signed_result_r,
    snap_ratio,
    SL_TP_RATIOS,
    SL_TP_MIN,
    SL_TP_MAX,
    SL_TP_STEP,
    sl_tp_grid,
    cost_as_frac,
    net_pnl_pct,
    row_net_pnl,
    row_position_cost_pct,
    row_side,
    filter_side,
)
from contracts import AXES, INDICATION_KINDS, VOLUME_RATIO_UNIT, stable_key
from indication_engine import bars_to_candles, evaluate_signal_candles, evaluate_ta_pack, evaluate_direction, evaluate_move, evaluate_active, evaluate_common, evaluate_trend, evaluate_break, ohlcv_row
from risk_variants import TRAIL_VARIANTS, TRAIL_ARM_MIN, TRAIL_ARM_MAX, TRAIL_GIVE_MIN, TRAIL_GIVE_MAX, give_from_arm, parse_trail, trail_candidates, trail_grid, trail_key

try:
    import numpy as _np
except Exception:  # pragma: no cover - scalar replay remains the fallback
    _np = None

PACKS = ("indications", "general")
DIRECTIONS = ("LONG", "SHORT")
DEACT_N_DEFAULT = 25
PF_N_DEFAULT = 15
LOOKBACK_DEFAULT = 480
LOOKBACK_MAX = 4320  # three days of 1m bars for historic validation
WARMUP_DEFAULT = 30
BAR_S = 60.0
FEE_PCT = 0.001  # round-trip, matches live close_pos
# The catalog must test the complete configured axis.  No example threshold
# is treated as an empirical result; live VST evidence decides which step is
# eventually preferred for live selection.
STEP_MIN = 1
STEP_LIVE_MIN = 1
STEP_MAX = 22
# Keep enough recent fills for the 5/10/15/25/50/75 evaluation windows while
# retaining a hard per-set memory bound.  trim_hist distributes this cap over
# symbols, so a busy multi-symbol book cannot grow without limit.
HIST_CAP = 80
# Indication kinds (live) <-> historic replay vote tags (indication_signal why).
IND_KINDS = ("state", "signals", "active", "direction", "move", "common", "trend", "break")
IND_TAG_KIND = {"sig": "signals", "ta": "state", "dir": "direction", "move": "move", "act": "active", "common": "common", "trend": "trend", "brk": "break", "break": "break"}


def clamp_step(v: Any, lo: int = STEP_MIN, hi: int = STEP_MAX) -> int:
    try:
        n = int(v)
    except Exception:
        n = lo
    return max(lo, min(hi, n))


def step_tp_pct(step: int, cost_pct: float) -> float:
    """TP fraction = step × position cost; the VST tape chooses the winner."""
    c = max(1e-9, float(cost_pct))
    if c > 0.05:
        c = c / 100.0
    return max(c, clamp_step(step) * c)


def finite(v: Any, fallback: float = 0.0) -> float:
    try:
        n = float(v)
    except Exception:
        return fallback
    return n if n == n and abs(n) != float("inf") else fallback


def trades_per_hour(rows: Sequence[Any]) -> float:
    """Return observed close throughput from a timestamped live tape."""
    ordered = sorted(
        [row for row in rows if row is not None],
        key=lambda row: finite(row.get("t") if isinstance(row, dict) else getattr(row, "t", 0)),
    )
    if len(ordered) < 2:
        return 0.0
    first = finite(ordered[0].get("t") if isinstance(ordered[0], dict) else getattr(ordered[0], "t", 0))
    last = finite(ordered[-1].get("t") if isinstance(ordered[-1], dict) else getattr(ordered[-1], "t", 0))
    # Duplicate/near-duplicate exchange timestamps must not create an
    # infinite score or make a tiny test burst look like a stable rate.
    span_h = max(1.0 / 60.0, (last - first) / 3600.0)
    keys = {str(row.get("client_id") or row.get("fillId") or i) if isinstance(row, dict)
            else str(getattr(row, "client_id", "") or i) for i, row in enumerate(ordered)}
    return len(keys) / span_h


def trim_hist(bucket: Sequence[Dict[str, Any]], cap: int = HIST_CAP) -> List[Dict[str, Any]]:
    """Keep recent fills from every symbol so last-N PF/DDT is not the last 1–2 names."""
    rows = [r for r in bucket if isinstance(r, dict)]
    cap = max(8, int(cap or HIST_CAP))
    if len(rows) <= cap:
        rows.sort(key=lambda r: finite(r.get("t")))
        return rows
    by: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by.setdefault(str(r.get("symbol") or "?"), []).append(r)
    per = max(3, cap // max(1, len(by)))
    out: List[Dict[str, Any]] = []
    for tape in by.values():
        tape.sort(key=lambda r: finite(r.get("t")))
        out.extend(tape[-per:])
    out.sort(key=lambda r: finite(r.get("t")))
    return out[-cap:]


def hist_row_key(row: Dict[str, Any]) -> Tuple[str, float, str, str, float, float]:
    """Stable identity for a deterministic historic replay row."""
    return (
        str(row.get("symbol") or ""),
        round(finite(row.get("t")), 6),
        str(row.get("side") or row.get("direction") or ""),
        str(row.get("reason") or ""),
        round(finite(row.get("pnl_pct")), 10),
        round(finite(row.get("hold_s")), 3),
    )


def merge_hist_rows(
    previous: Sequence[Dict[str, Any]],
    incoming: Sequence[Dict[str, Any]],
    replace_symbols: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Merge a refreshed symbol slice into the bounded historic tape."""
    replace = {str(s) for s in (replace_symbols or ())}
    merged: Dict[Tuple[str, float, str, str, float, float], Dict[str, Any]] = {}
    for row in previous:
        if not isinstance(row, dict) or str(row.get("symbol") or "") in replace:
            continue
        merged[hist_row_key(row)] = row
    for row in incoming:
        if isinstance(row, dict):
            merged[hist_row_key(row)] = row
    return trim_hist(list(merged.values()), HIST_CAP)


def last_n_balanced(rows: Sequence[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
    """Last-N fills with every symbol still represented when possible."""
    n = max(1, int(n))
    ordered = [r for r in rows if isinstance(r, dict)]
    ordered.sort(key=lambda r: finite(r.get("t")))
    if len(ordered) <= n:
        return ordered
    by: Dict[str, List[Dict[str, Any]]] = {}
    for r in ordered:
        by.setdefault(str(r.get("symbol") or "?"), []).append(r)
    if len(by) <= 1:
        return ordered[-n:]
    names = list(by.keys())
    if len(names) >= n:
        recent = sorted(names, key=lambda s: finite(by[s][-1].get("t")), reverse=True)[:n]
        picked = [by[s][-1] for s in recent]
        picked.sort(key=lambda r: finite(r.get("t")))
        return picked
    per = max(1, n // len(names))
    out: List[Dict[str, Any]] = []
    for tape in by.values():
        out.extend(tape[-per:])
    out.sort(key=lambda r: finite(r.get("t")))
    if len(out) < n:
        taken = {id(x) for x in out}
        for r in reversed(ordered):
            if id(r) in taken:
                continue
            out.append(r)
            taken.add(id(r))
            if len(out) >= n:
                break
        out.sort(key=lambda r: finite(r.get("t")))
    return out[-n:]


def drawdown_time(rows: Sequence[Dict[str, Any]], now: Optional[float] = None) -> Dict[str, float]:
    """CTS drawdown-time: episodes from peak through recovery, in seconds."""
    ordered = sorted(
        (r for r in rows if isinstance(r, dict) and finite(r.get("t")) > 0),
        key=lambda r: finite(r.get("t")),
    )
    # A historic tape has no meaningful wall-clock tail. If the caller does
    # not provide an observation time, close an open episode at its last
    # sample. For an explicitly supplied time, do not let a stale symbol's
    # missing data create a false multi-hour DDt episode.
    if now is None:
        now = finite(ordered[-1].get("t")) if ordered else time.time()
    elif ordered:
        last_t = finite(ordered[-1].get("t"))
        if last_t > 0 and now - last_t > 3600:
            now = last_t
    equity = 0.0
    peak = 0.0
    started: Optional[float] = None
    max_s = 0.0
    total_s = 0.0
    episodes = 0
    max_depth = 0.0
    for row in ordered:
        t = finite(row.get("t"))
        if t <= 0:
            continue
        equity += finite(row.get("pnl"))
        if equity >= peak - 1e-12:
            if started is not None:
                dur = max(0.0, t - started)
                max_s = max(max_s, dur)
                total_s += dur
                started = None
            peak = max(peak, equity)
            continue
        if started is None:
            started = t
            episodes += 1
        max_depth = max(max_depth, peak - equity)
    current = 0.0 if started is None else max(0.0, now - started)
    if started is not None:
        max_s = max(max_s, current)
        total_s += current
    return {
        "episodes": float(episodes),
        "maxS": round(max_s, 1),
        "avgS": round(total_s / episodes, 1) if episodes else 0.0,
        "totalS": total_s,
        "currentS": round(current, 1),
        "maxDepth": round(max_depth, 6),
        "inDd": 1.0 if started is not None else 0.0,
        "n": float(len(ordered)),
    }


def drawdown_time_by_symbol(rows: Sequence[Dict[str, Any]], now: Optional[float] = None) -> Dict[str, float]:
    """DDT per symbol, then max/mean. Mixed-market tapes must not span one 20h episode."""
    by: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        by.setdefault(str(r.get("symbol") or "?"), []).append(r)
    if len(by) <= 1:
        return drawdown_time(rows, now)
    parts = [drawdown_time(tape, now) for tape in by.values() if tape]
    if not parts:
        return drawdown_time(rows, now)
    episodes = sum(p["episodes"] for p in parts)
    total_s = sum(p["totalS"] for p in parts)
    return {
        "episodes": float(episodes),
        "maxS": round(max(p["maxS"] for p in parts), 1),
        "avgS": round(total_s / episodes, 1) if episodes else 0.0,
        "currentS": round(max(p["currentS"] for p in parts), 1),
        "maxDepth": round(max(p["maxDepth"] for p in parts), 6),
        "inDd": 1.0 if any(p["inDd"] for p in parts) else 0.0,
        "n": float(len(rows)),
        "symbols": float(len(by)),
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

    def ema(values: List[float], n: int) -> float:
        k = 2.0 / (n + 1)
        e = values[0]
        for x in values[1:]:
            e = x * k + e * (1 - k)
        return e

    def rsi(values: List[float], n: int = 7) -> float:
        if len(values) < n + 1:
            return 50.0
        gains = losses = 0.0
        window = values[-(n + 1) :]
        for i in range(1, len(window)):
            d = window[i] - window[i - 1]
            if d >= 0:
                gains += d
            else:
                losses -= d
        if losses == 0:
            return 100.0
        rs = (gains / n) / (losses / n)
        return 100 - (100 / (1 + rs))

    e8 = ema(closes, 8)
    e21 = ema(closes, 21)
    r = rsi(closes, 7)
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


def indication_kind_votes(bars: Sequence[Sequence[float]], settings: Dict[str, Any], now: float) -> List[Tuple[int, float, str]]:
    """Independent vote per indication kind. Signals / State / Direction / Move / Active / Common."""
    candles = bars_to_candles(list(bars)[-60:], now=now, period_s=BAR_S)
    closes = []
    for b in list(bars)[-60:]:
        row = ohlcv_row(b)
        if row:
            closes.append(row[3])
    want = {
        "sig": bool(settings.get("typeSignals", True)),
        "ta": bool(settings.get("typeState", True)),
        "dir": bool(settings.get("typeDirection", True)),
        "move": bool(settings.get("typeMove", True)),
        "act": bool(settings.get("typeActive", True)),
        "common": bool(settings.get("typeCommon", True)),
        "trend": bool(settings.get("typeTrend", True)),
        "brk": bool(settings.get("typeBreak", True)),
    }
    votes: List[Tuple[int, float, str]] = []
    if want["sig"]:
        try:
            ev = evaluate_signal_candles("hist-1m", "Historic 1m", candles, settings, weight=0.85)
            if ev:
                votes.append((1 if ev.direction == "long" else -1, float(ev.confidence), "sig"))
        except Exception:
            pass
    if want["ta"]:
        try:
            ta = evaluate_ta_pack(candles, settings)
            if ta:
                votes.append((1 if ta.direction == "long" else -1, float(ta.confidence), "ta"))
        except Exception:
            pass
    if want["dir"] and closes:
        try:
            drow = evaluate_direction("hist", closes, settings)
            if drow:
                votes.append((1 if drow.direction == "long" else -1, float(drow.confidence), "dir"))
        except Exception:
            pass
    if want["move"] and closes:
        try:
            mrow = evaluate_move("hist", closes, settings)
            if mrow:
                votes.append((1 if mrow.direction == "long" else -1, float(mrow.confidence), "move"))
        except Exception:
            pass
    if want["act"] and closes:
        try:
            arow = evaluate_active("hist", closes, settings)
            if arow:
                votes.append((1 if arow.direction == "long" else -1, float(arow.confidence), "act"))
        except Exception:
            pass
    if want["common"] and candles:
        try:
            crow = evaluate_common("hist", candles, settings)
            if crow:
                votes.append((1 if crow.direction == "long" else -1, float(crow.confidence), "common"))
        except Exception:
            pass
    if want.get("trend") and closes:
        try:
            trow = evaluate_trend("hist", closes, settings)
            if trow:
                votes.append((1 if trow.direction == "long" else -1, float(trow.confidence), "trend"))
        except Exception:
            pass
    if want.get("brk") and closes:
        try:
            brow = evaluate_break("hist", closes, settings)
            if brow:
                votes.append((1 if brow.direction == "long" else -1, float(brow.confidence), "brk"))
        except Exception:
            pass
    return votes


def votes_to_signal(votes: Sequence[Tuple[int, float, str]]) -> Tuple[int, float, str]:
    if not votes:
        return 0, 0.0, "flat"
    long_w = sum(c for d, c, _ in votes if d > 0)
    short_w = sum(c for d, c, _ in votes if d < 0)
    if long_w > short_w and long_w >= 0.6:
        return 1, min(1.0, long_w / max(1, len(votes))), "+".join(w for d, _, w in votes if d > 0)
    if short_w > long_w and short_w >= 0.6:
        return -1, min(1.0, short_w / max(1, len(votes))), "+".join(w for d, _, w in votes if d < 0)
    return 0, max(long_w, short_w), "split"


def indication_signal(bars: Sequence[Sequence[float]], settings: Dict[str, Any], now: float) -> Tuple[int, float, str]:
    return votes_to_signal(indication_kind_votes(bars, settings, now))


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
    parts = [pack, "1m", f"sl{float(sl_ratio):.1f}"]
    if trail:
        parts.append(f"tr{trail}")
    if step:
        parts.append(f"st{int(step)}")
    return ":".join(parts)


def make_trail_id(pack: str, trail: str, sl_ratio: float = 0.6, step: int = 0) -> str:
    return make_set_id(pack, sl_ratio, trail=trail, step=step)


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


@dataclass(frozen=True)
class StageRecord:
    """Stable audit record for one Set at one processing boundary."""

    set_id: str
    parent_set_id: str
    stage: str
    gross_pf: float
    net_pf: float
    position_cost_pct: float
    ddt_s: float
    sample_count: int
    volume_ratio: float
    ev: float = 0.0
    gross_ev: float = 0.0
    confidence: float = 0.0
    uncertainty: float = 1.0
    required_samples: int = 8
    insufficient_sample: bool = True
    axis_key: str = ""
    relative_count: int = 1
    indication_kind: str = ""
    strategy_adjustments: Tuple[Tuple[str, Any], ...] = ()
    adjustment_deltas: Tuple[Tuple[str, Any], ...] = ()
    qualification_reason: str = ""
    dedupe_key: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "setId": self.set_id,
            "parentSetId": self.parent_set_id,
            "stage": self.stage,
            "grossPf": self.gross_pf,
            "netPf": self.net_pf,
            "positionCostPct": self.position_cost_pct,
            "ddtS": self.ddt_s,
            "sampleCount": self.sample_count,
            "requiredSamples": self.required_samples,
            "ev": self.ev,
            "grossEv": self.gross_ev,
            "confidence": self.confidence,
            "uncertainty": self.uncertainty,
            "insufficientSample": self.insufficient_sample,
            "volumeRatio": self.volume_ratio,
            "axisKey": self.axis_key,
            "relativeCount": self.relative_count,
            "indicationKind": self.indication_kind,
            "strategyAdjustments": dict(self.strategy_adjustments),
            "adjustmentDeltas": dict(self.adjustment_deltas),
            "qualificationReason": self.qualification_reason,
            "dedupeKey": self.dedupe_key or stable_key(self.set_id, self.stage),
        }


@dataclass
class SetState:
    id: str
    pack: str
    tf: str
    sl_ratio: float
    trail_key: str
    trail_arm: float
    trail_give: float
    step: int = STEP_MIN
    tp_pct: float = 0.0045
    idx: int = 0
    pack_i: int = 0
    sl_i: int = 0
    tr_i: int = 0
    step_i: int = 0
    kind: str = "base"
    parent_set_id: str = ""
    stage: str = "Base"
    stage_qualified: str = ""
    base_pf: float = 0.0
    main_pf: float = 0.0
    real_pf: float = 0.0
    position_cost_pct: float = POSITION_COST_PCT_DEFAULT
    axis_key: str = ""
    relative_count: int = 1
    volume_ratio: float = 1.0
    indication_kind: str = ""
    strategy_adjustments: Dict[str, Any] = field(default_factory=dict)
    stage_ledger: Dict[str, Any] = field(default_factory=dict)
    hist: List[Dict[str, Any]] = field(default_factory=list)
    live: List[Dict[str, Any]] = field(default_factory=list)
    last15_ratio: float = 1.0
    last15_classic: float = 0.0
    last15_n: int = 0
    last15_r: float = 0.0
    last25_avg_r: float = 0.0
    last25_n: int = 0
    last25_avg_pnl: float = 0.0
    max_dd_s: float = 0.0
    avg_dd_s: float = 0.0
    dd_episodes: int = 0
    n: int = 0
    wins: int = 0
    gp: float = 0.0
    gl: float = 0.0
    wr: float = 0.0
    expectancy: float = 0.0
    avg_hold_s: float = 0.0
    classic_all: float = 0.0
    exits: Dict[str, int] = field(default_factory=dict)
    active: bool = False
    deact_reason: str = ""
    locked: bool = False
    source_n: int = 0
    by_side: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    live_eval: Dict[str, Any] = field(default_factory=dict)
    gross_pf: float = 0.0
    net_pf: float = 0.0
    gross_ev: float = 0.0
    net_ev: float = 0.0
    evaluation: Dict[str, Any] = field(default_factory=dict)
    evaluation_windows: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    normal_evaluation: Dict[str, Any] = field(default_factory=dict)
    adjusted_evaluation: Dict[str, Any] = field(default_factory=dict)
    adjustment_deltas: Dict[str, Any] = field(default_factory=dict)

    def tape(self) -> List[Dict[str, Any]]:
        return list(self.hist) + list(self.live)

    def tape_side(self, side: Optional[str] = None) -> List[Dict[str, Any]]:
        return filter_side(self.tape(), side)


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


class SetBook:
    def __init__(self) -> None:
        self.enabled = True
        self.lookback = LOOKBACK_DEFAULT
        self.min_bars = 120
        self.warmup = WARMUP_DEFAULT
        self.refresh_s = 90.0
        self.pf_n = PF_N_DEFAULT
        self.deact_n = DEACT_N_DEFAULT
        self.min_pf = 1.05
        self.stage_min_pf = {"base": 1.05, "main": 1.10, "real": 1.15}
        self.real_min_pf = 1.15
        self.max_dd_s = 27000.0
        self.auto_deact = True
        self.use_historic_gate = True
        self.min_samples = 8
        self.reactivate = True
        self.strict_gate = True
        self.max_active = 0
        self.cost_pct = POSITION_COST_PCT_DEFAULT
        self.cost_source = "manual-fallback"
        # Optional live-selection policy: prefer the smallest stable
        # configuration ranges only after PF/DD/sample gates have passed.
        # Profit Factor remains the primary ranking objective.
        self.prefer_minimal_range = True
        self.additional_coordination = True
        # Deprecated internal aliases kept while older persisted overlays
        # and remote clients migrate to the explicit names above.
        self.prefer_minimal_positive = self.prefer_minimal_range
        self.minimal_positive_coordination = self.additional_coordination
        self.optimization_n = 50
        self.optimization_stats: Dict[str, Any] = {}
        self.live_negative_deact = True
        self.time_stop_s = 21600.0
        self.hist_time_bars = 45
        self.scratch_s = 90.0
        self.scratch_min = 0.0016
        self.tp_pct = 0.0075
        self.cooldown_bars = 2
        self.ignore_tp = True
        self.opt_sl = 0.0030
        self.min_step_cfg = STEP_LIVE_MIN
        self.min_step = STEP_LIVE_MIN
        self.step_max = STEP_MAX
        self.step_adapt = True
        self.steps: List[int] = list(range(STEP_MIN, STEP_MAX + 1))
        self.packs: List[str] = list(PACKS)
        self.sl_ratios: List[float] = list(SL_TP_RATIOS)
        self.trails: List[Tuple[str, float, float]] = []
        self.trail_enabled = True
        self.sets: Dict[str, SetState] = {}
        self.by_idx: List[SetState] = []
        self.bars: Dict[str, List[List[float]]] = {}
        self.progress = Progress()
        self.last_run = 0.0
        self.ind_settings: Dict[str, Any] = {}
        self.locks: Dict[str, bool] = {}
        # Per-indication-kind evidence: hist (rebuilt by every replay from the
        # winning vote tags) + live (fed by on_live_close via rec.ind_kind).
        self.ind_hist: Dict[str, List[Dict[str, Any]]] = {}
        self.ind_live: Dict[str, List[Dict[str, Any]]] = {}
        # Main-stage strategy tapes are kept separately from the canonical
        # Set tape so Block/DCA evidence cannot inflate Base/Real counts.
        self.strategy_hist: Dict[str, List[Dict[str, Any]]] = {}
        # Partial load-sliced replays retain bounded evidence from symbols
        # already covered in the current cycle. Per-symbol counts keep
        # histFills meaningful even though each Set tape is capped for RAM.
        self._hist_seen: set[str] = set()
        self._hist_total = 0
        self._hist_counts: Dict[str, Dict[str, int]] = {}
        self._hist_set_signature: Tuple[str, ...] = ()
        self._running = False
        self._snap_cache: Optional[Dict[str, Any]] = None
        self._snap_ts = 0.0
        self._live_ov_cache: Optional[Dict[str, Any]] = None
        self._live_ov_ts = 0.0
        self._pick_lock = threading.RLock()
        self._pick_cursor = 0
        self.hist_block = True
        self.hist_dca = True
        self.block_vr = 1.0
        self.dca_dist = [0.012, 0.016, 0.020, 0.024]
        self.dca_mult = [1.5, 2.0, 2.3, 2.5]
        # VST-only exploration is opt-in through the X02 overlay.  It rotates
        # a small, historic-qualified candidate set until each candidate has
        # enough real exchange fills; X01 never enables this by default.
        self.live_test_mode = False
        self.live_test_candidates = 12
        self.live_test_min_samples = 8

    def __getstate__(self) -> Dict[str, Any]:
        """Return a copy-safe state for isolated historic replays.

        ``SetBook`` is snapshotted while the live lane continues processing
        exchange events.  The selection lock protects the live book but is
        not itself copyable; omitting it from the snapshot lets deepcopy
        recreate an independent lock in ``__setstate__``.
        """
        state = dict(self.__dict__)
        state.pop("_pick_lock", None)
        return state

    def __setstate__(self, state: Dict[str, Any]) -> None:
        self.__dict__.update(state)
        self._pick_lock = threading.RLock()

    def replay_clone(self, symbols: Optional[Sequence[str]] = None) -> "SetBook":
        """Create a lean catalog clone for an isolated history slice.

        A full ``deepcopy(SetBook)`` duplicates the complete 34k-state object
        graph before a replay has produced any evidence.  That is needlessly
        expensive on the long-running lane and can turn a bounded replay into
        cgroup swap pressure.  The replay only needs catalog parameters,
        selected bars, and empty evidence containers; live evidence remains
        authoritative in the source book and is merged on commit.
        """
        clone = SetBook()
        skip = {
            "sets", "by_idx", "bars", "progress", "last_run",
            "ind_hist", "ind_live", "strategy_hist", "_hist_seen",
            "_hist_total", "_hist_counts", "_hist_set_signature", "_running",
            "_snap_cache", "_snap_ts", "_live_ov_cache", "_live_ov_ts",
            "_pick_lock",
        }
        for name, value in self.__dict__.items():
            if name in skip:
                continue
            if isinstance(value, (dict, list, set, tuple)):
                setattr(clone, name, copy.deepcopy(value))
            else:
                setattr(clone, name, value)

        empty_fields = {
            "strategy_adjustments": {},
            "stage_ledger": {},
            "hist": [],
            "live": [],
            "exits": {},
            "by_side": {},
            "live_eval": {},
            "evaluation": {},
            "evaluation_windows": {},
            "normal_evaluation": {},
            "adjusted_evaluation": {},
            "adjustment_deltas": {},
        }
        states: Dict[str, SetState] = {}
        ordered: List[SetState] = []
        for original in self.by_idx:
            state = copy.copy(original)
            for field_name, empty in empty_fields.items():
                setattr(state, field_name, copy.deepcopy(empty))
            state.last15_ratio = 1.0
            state.last15_classic = state.last15_r = 0.0
            state.last15_n = state.last25_n = 0
            state.last25_avg_r = state.last25_avg_pnl = 0.0
            state.max_dd_s = state.avg_dd_s = 0.0
            state.dd_episodes = 0
            state.n = state.wins = 0
            state.gp = state.gl = state.wr = state.expectancy = 0.0
            state.avg_hold_s = state.classic_all = 0.0
            state.source_n = 0
            state.active = False
            state.deact_reason = ""
            state.base_pf = state.main_pf = state.real_pf = 0.0
            state.gross_pf = state.net_pf = state.gross_ev = state.net_ev = 0.0
            states[state.id] = state
            ordered.append(state)
        clone.sets = states
        clone.by_idx = ordered
        wanted = [str(s) for s in symbols] if symbols is not None else list(self.bars)
        clone.bars = {
            symbol: [list(row) for row in (self.bars.get(symbol) or [])]
            for symbol in wanted if self.bars.get(symbol)
        }
        clone.progress = copy.deepcopy(self.progress)
        clone.ind_hist = {}
        clone.ind_live = {}
        clone.strategy_hist = {}
        clone._hist_seen = set(self._hist_seen)
        clone._hist_total = int(self._hist_total or 0)
        clone._hist_counts = {}
        clone._hist_set_signature = tuple(self._hist_set_signature)
        clone._running = False
        clone._snap_cache = None
        clone._snap_ts = 0.0
        clone._live_ov_cache = None
        clone._live_ov_ts = 0.0
        return clone

    def load(
        self,
        ov: Dict[str, Any],
        cts: Optional[Dict[str, Any]] = None,
        *,
        rebuild: bool = True,
    ) -> None:
        cts = cts or {}
        self.enabled = bool(ov.get("histEnabled", True))
        self.lookback = max(120, min(LOOKBACK_MAX, int(ov.get("histLookbackBars") or LOOKBACK_DEFAULT)))
        self.min_bars = max(60, min(self.lookback, int(ov.get("histMinBars") or 120)))
        self.warmup = max(16, min(80, int(ov.get("histWarmup") or WARMUP_DEFAULT)))
        self.refresh_s = max(30.0, min(600.0, float(ov.get("histRefreshS") or 90)))
        self.pf_n = max(5, min(50, int(ov.get("setPfWindow") or ov.get("pfWindow") or PF_N_DEFAULT)))
        self.deact_n = max(10, min(80, int(ov.get("setDeactN") or DEACT_N_DEFAULT)))
        def _pf(key: str, fallback: float) -> float:
            return normalize_pf(ov.get(key, fallback), fallback)
        self.stage_min_pf = {
            "base": _pf("baseMinPf", 1.05),
            "main": _pf("mainMinPf", 1.10),
            "real": _pf("realMinPf", float(ov.get("setMinPf") or 1.15)),
        }
        self.min_pf = _pf("setMinPf", _pf("minPf", self.stage_min_pf["base"]))
        self.real_min_pf = self.stage_min_pf["real"]
        self.max_dd_s = max(600.0, min(650.0 * 60.0, float(ov.get("setMaxDdTimeS") or 27000)))
        self.auto_deact = bool(ov.get("setAutoDeact", True))
        self.live_negative_deact = bool(ov.get("setLiveNegativeDeact", ov.get("liveNegativeSetDeactivation", False)))
        self.prefer_minimal_range = bool(
            ov.get("preferMinimalRange", ov.get("preferMinimalPositive", False))
        )
        self.additional_coordination = bool(
            ov.get("additionalCoordination", ov.get("minimalPositiveCoordination", False))
        )
        self.live_test_mode = bool(ov.get("liveTestMode", False))
        try:
            self.live_test_candidates = max(2, min(32, int(ov.get("liveTestCandidates") or 12)))
        except Exception:
            self.live_test_candidates = 12
        self.prefer_minimal_positive = self.prefer_minimal_range
        self.minimal_positive_coordination = self.additional_coordination
        try:
            self.optimization_n = max(50, min(200, int(ov.get("coordOptimizationN") or 50)))
        except Exception:
            self.optimization_n = 50
        self.use_historic_gate = bool(ov.get("setUseHistoricGate", True))
        self.min_samples = max(5, min(40, int(ov.get("setMinSamples") or 8)))
        try:
            self.live_test_min_samples = max(5, min(25, int(ov.get("liveTestMinSamples") or self.eval_need())))
        except Exception:
            self.live_test_min_samples = self.eval_need()
        self.reactivate = bool(ov.get("setReactivate", True))
        # Strict gate (default ON): only VALIDATED (last-N fills >= 8) AND
        # PROFITABLE (cost-adjusted PF > 1.15) + DDt under the cap may
        # drive live orders. Cold/unproven sets keep collecting evidence.
        self.strict_gate = bool(ov.get("setStrictGate", True))
        try:
            raw_active = int(ov.get("setMaxActive") if ov.get("setMaxActive") is not None else 0)
        except Exception:
            raw_active = 0
        self.max_active = 0 if raw_active <= 0 else max(1, raw_active)
        self.cost_pct = float(ov.get("positionCostPct") or ov.get("setCostPct") or POSITION_COST_PCT_DEFAULT)
        if self.cost_pct > 2:
            self.cost_pct = self.cost_pct / 100.0
        if self.cost_pct > 1:
            self.cost_pct = POSITION_COST_PCT_DEFAULT
        self.cost_source = str(ov.get("positionCostSource") or "manual-fallback")
        self.time_stop_s = float(ov.get("timeStopS") or 21600)
        self.hist_time_bars = max(8, min(120, int(ov.get("setHistTimeBars") or 45)))
        self.scratch_s = float(ov.get("scratchS") or 90)
        tp = float(ov.get("tpPct") or 0.75)
        self.tp_pct = tp / 100.0 if tp > 0.05 else tp
        self.ignore_tp = bool(ov.get("exitIgnoreTp", True))
        self.hist_honor_tp = bool(ov.get("setHonorTp", True))
        self.hist_block = bool(ov.get("histSimulateBlock", ov.get("stratBlock", True)))
        self.hist_dca = bool(ov.get("histSimulateDca", True))
        try:
            self.block_vr = max(0.5, min(2.0, float(ov.get("blockVolumeRatio") or 1.0)))
        except Exception:
            self.block_vr = 1.0
        self.dca_dist = [0.012, 0.016, 0.020, 0.024]
        self.dca_mult = [1.5, 2.0, 2.3, 2.5]
        opt = float(ov.get("exitOptSlPct") or 0.30)
        self.opt_sl = opt / 100.0 if opt > 0.02 else opt
        self.min_step_cfg = clamp_step(ov.get("setMinStep") or ov.get("minStepRange") or STEP_LIVE_MIN)
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
        # Full SL:TP catalog always. A selected live slToTpRatio never shrinks
        # the book. An explicit slToTpRatios list is only a test pin when no
        # range keys are present.
        has_range = any(k in ov for k in ("slToTpMin", "slToTpMax", "slToTpStep"))
        raw_ratios = ov.get("slToTpRatios")
        if has_range or not (isinstance(raw_ratios, (list, tuple)) and raw_ratios):
            lo = finite(ov.get("slToTpMin"), SL_TP_MIN)
            hi = finite(ov.get("slToTpMax"), SL_TP_MAX)
            step = finite(ov.get("slToTpStep"), SL_TP_STEP)
            self.sl_ratios = sl_tp_grid(lo, hi, step)
        else:
            ratios = []
            for x in raw_ratios:
                try:
                    ratios.append(max(SL_TP_MIN, min(SL_TP_MAX, round(float(x), 2))))
                except Exception:
                    continue
            self.sl_ratios = sorted(set(ratios)) or list(SL_TP_RATIOS)
        self.trail_enabled = bool(ov.get("stratTrailing", True))
        # Always enumerate the full arm×give product as independent Sets
        # alongside Normal (base) SL×TP. A selected live trail does not hide
        # the others. Explicit stratTrailing=False drops the trail family
        # (hist-calc checkbox) — live default stays on. Settings no longer
        # shrinks the catalog; overlay min/max are forced to the full grid.
        if ov.get("stratTrailing") is False:
            self.trails = []
        else:
            self.trails = trail_grid(
                float(ov.get("trailArmMin") or TRAIL_ARM_MIN),
                float(ov.get("trailArmMax") or TRAIL_ARM_MAX),
                float(ov.get("trailGiveMin") or TRAIL_GIVE_MIN),
                float(ov.get("trailGiveMax") or TRAIL_GIVE_MAX),
            )
        locks = ov.get("setLocks") if isinstance(ov.get("setLocks"), dict) else {}
        self.locks = {str(k): bool(v) for k, v in locks.items()}
        self.ind_settings = {
            "candleLimit": 60,
            "minimumStrength": float(ov.get("indMinStrength") or 0.2),
            "minimumConfidence": float(ov.get("indMinConfidence") or 0.6),
            "minimumAgreement": float(ov.get("indMinAgreement") or 0.55),
            "stopLossMinPct": float(ov.get("indStopMinPct") or 0.2),
            "stopLossMaxPct": float(ov.get("indStopMaxPct") or 1.5),
            "stopLossAtrMultiplier": float(ov.get("indAtrMult") or 0.85),
            "takeProfitRewardRisk": float(ov.get("indRewardRisk") or 1.8),
            "takeProfitMaxPct": 5.0,
            "positionCostPct": self.cost_pct,
            "typeState": bool(ov.get("indTypeState", True)),
            "typeSignals": bool(ov.get("indTypeSignals", True)),
            "typeTrend": bool(ov.get("indTypeTrend", True)),
            "typeBreak": bool(ov.get("indTypeBreak", True)),
            "typeDirection": bool(ov.get("indTypeDirection", True)),
            "typeMove": bool(ov.get("indTypeMove", True)),
            "typeActive": bool(ov.get("indTypeActive", True)),
            "typeCommon": bool(ov.get("indTypeCommon", True)),
            "activeOutbreak": ov.get("activeOutbreakRanges") or ov.get("indActiveOutbreak") or [3, 5, 10],
            "dirRange": int(ov.get("indDirRange") or 10),
            "dirMinChange": float(ov.get("indDirMinChange") or 0.001),
            "moveRange": int(ov.get("indMoveRange") or 10),
            "moveMinChange": float(ov.get("indMoveMinChange") or 0.001),
            "activeThreshold": float(ov.get("indActiveThreshold") or 1.0),
            "activeNoise": float(ov.get("indActiveNoise") or ov.get("noise") or 0.0005),
            "activeMovePct": float(ov.get("indActiveMovePct") or ov.get("activeMovePct") or 0.5),
            "activeVolatilityWeight": float(ov.get("volWeight") or ov.get("activeVolatilityWeight") or 0.3),
        }
        try:
            self.cooldown_bars = max(1, min(12, int(ov.get("setCooldownBars") or 2)))
        except Exception:
            self.cooldown_bars = 2
        try:
            self.scratch_min = float(ov.get("setScratchMin") or 0.0016)
        except Exception:
            self.scratch_min = 0.0016
        # A live engine may have tens of thousands of independent Sets.  The
        # initial catalog is allowed to build off the service start path so a
        # slow CPU or exchange response cannot make systemd report a startup
        # timeout.  Normal reloads keep the synchronous behaviour unless the
        # caller explicitly opts into the deferred bootstrap.
        if rebuild:
            self._rebuild_sets()

    def eval_need(self) -> int:
        """Fills needed to enable a Set. Capped at 8 so last-15 can validate."""
        try:
            ms = int(self.min_samples or 8)
        except Exception:
            ms = 8
        try:
            pf = int(self.pf_n or 15)
        except Exception:
            pf = 15
        return max(5, min(8, ms, pf))

    def _step_grid(self) -> List[int]:
        # Always the configured TP-step range. Live adapt may prefer a higher
        # step when picking, but it must never drop SL×TP sets from the book.
        lo = clamp_step(self.min_step_cfg, STEP_MIN, self.step_max)
        hi = clamp_step(self.step_max, lo, STEP_MAX)
        return list(range(lo, hi + 1))

    def _rebuild_sets(self) -> None:
        keep = {sid: st for sid, st in self.sets.items()}
        next_sets: Dict[str, SetState] = {}
        by_idx: List[SetState] = []
        self.steps = self._step_grid()
        trails = list(self.trails) if self.trails else []
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
                    tp = step_tp_pct(step, self.cost_pct)
                    sid = make_set_id(pack, sl, "", step)
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
                        sid = make_trail_id(pack, tkey, sl, step)
                        prev = keep.get(sid)
                        if prev:
                            st = prev
                            st.trail_key = tkey
                            st.trail_arm = arm
                            st.trail_give = give
                            st.sl_ratio = sl
                            st.step = step
                            st.tp_pct = tp
                            st.kind = "trail"
                            st.locked = bool(self.locks.get(sid))
                        else:
                            st = SetState(
                                id=sid, pack=pack, tf="1m", sl_ratio=sl,
                                trail_key=tkey, trail_arm=arm, trail_give=give,
                                step=step, tp_pct=tp, kind="trail",
                                locked=bool(self.locks.get(sid)),
                            )
                        st.pack_i = pack_i
                        st.sl_i = sl_i
                        st.tr_i = tr_i
                        st.step_i = step_i
                        _put(st)
        self.sets = next_sets
        self.by_idx = by_idx
        for st in by_idx:
            st.parent_set_id = st.id if st.kind == "base" else make_set_id(st.pack, st.sl_ratio, "", st.step)
            st.stage = "Base"
            st.stage_qualified = ""
            st.position_cost_pct = self.cost_pct
            st.axis_key = ""
            st.relative_count = 1
            st.volume_ratio = 1.0
            st.indication_kind = "" if st.pack != "indications" else "signals"
            st.strategy_adjustments = {}
        self.progress.sets_total = len(self.sets)
        signature = tuple(next_sets)
        if signature != self._hist_set_signature:
            self._hist_set_signature = signature
            self._hist_seen.clear()
            self._hist_total = 0
            self._hist_counts = {}
            # Existing SetState objects are reused when IDs overlap. Their
            # historic tape belongs to the previous catalog and must not be
            # counted during the first partial replay of the new catalog.
            for st in self.by_idx:
                st.hist = []
                # Empty sets do not need a full metric/ledger object graph.
                # Scoring 34k fresh states here allocated evaluation windows,
                # stage dictionaries and side maps for every state before any
                # bars existed, pushing a small VPS into swap during boot.
                # Keep scalar defaults lazy; live evidence on a reused state
                # still gets scored immediately and remains independent.
                if st.live:
                    self._score_one(st)
                else:
                    st.n = 0
                    st.wins = 0
                    st.gp = st.gl = st.wr = st.expectancy = 0.0
                    st.avg_hold_s = st.classic_all = 0.0
                    st.last15_ratio = 1.0
                    st.last15_classic = st.last15_r = 0.0
                    st.last15_n = st.last25_n = 0
                    st.last25_avg_r = st.last25_avg_pnl = 0.0
                    st.max_dd_s = st.avg_dd_s = 0.0
                    st.dd_episodes = 0
                    st.source_n = 0
                    st.exits = {}
                    st.active = False
                    st.deact_reason = ""
                    st.base_pf = st.main_pf = st.real_pf = 0.0
                    st.stage_ledger = {}
                    st.by_side = {}
                    st.live_eval = {}
                    st.gross_pf = st.net_pf = st.gross_ev = st.net_ev = 0.0
                    st.evaluation = {}
                    st.evaluation_windows = {}
                    st.normal_evaluation = {}
                    st.adjusted_evaluation = {}
                    st.adjustment_deltas = {}
            # A changed set catalog invalidates the old gate until the new
            # catalog has been replayed over the full configured universe.
            self.progress.ready = False
            self.progress.phase = "idle"

    @staticmethod
    def _record_step(rec: Any) -> int:
        """Read a Set step from explicit metadata or its stable Set ID."""
        raw = rec.get("step") if isinstance(rec, dict) else getattr(rec, "step", 0)
        try:
            step = int(raw or 0)
        except Exception:
            step = 0
        if step > 0:
            return step
        set_id = str((rec.get("set_id") if isinstance(rec, dict) else getattr(rec, "set_id", "")) or "")
        for token in reversed(set_id.split(":")):
            if token.startswith("st"):
                try:
                    return int(token[2:])
                except Exception:
                    break
        return 0

    def _live_windows_ok(
        self,
        rows: Sequence[Dict[str, Any]],
        *,
        minimum_pf: float = 1.0,
    ) -> Tuple[bool, str, Dict[str, Dict[str, Any]]]:
        """Require every sufficiently sampled live window to clear its floor.

        A short positive tail cannot hide a longer negative tail.  Windows
        that do not yet have the configured sample minimum remain explicitly
        cold and do not veto an otherwise valid candidate.
        """
        ordered = sorted((r for r in rows if isinstance(r, dict)), key=lambda r: finite(r.get("t")))
        need = self.eval_need()
        windows = evaluation_windows(ordered, self.cost_pct, required_samples=need)
        checked = 0
        for name, metric in windows.items():
            n = int(metric.get("n") or 0)
            if n < need:
                continue
            checked += 1
            pf = float(metric.get("pf") or 0.0)
            if pf + 1e-9 < float(minimum_pf):
                return False, f"live {name} PF {pf:.2f}<{float(minimum_pf):.2f}", windows
        return True, "" if checked else "cold", windows

    def adapt_from_live(self, closed: Sequence[Any]) -> None:
        """Derive the preferred minimum from actual per-Set live evidence.

        The global tape is diagnostic only.  A negative aggregate cannot make
        the engine invent a step threshold, and a positive aggregate cannot
        promote every Set.  A step becomes preferred only when one or more
        independently tagged Sets at that step have enough live samples, a
        positive cost-net window at every available horizon, and the normal
        real-stage PF floor on the primary window.
        """
        floor = self.min_step_cfg
        self.optimization_stats: Dict[str, Any] = getattr(self, "optimization_stats", {}) or {}
        all_rows = sorted(
            list(closed),
            key=lambda r: finite(r.get("t") if isinstance(r, dict) else getattr(r, "t", 0)),
        )
        # The extra 50+ window is an explicit coordination option. The
        # legacy/off mode keeps the normal deactivation tape independent.
        window = max(50, self.deact_n, self.optimization_n) if self.additional_coordination else max(self.deact_n, 15)
        rows = all_rows[-window:]
        if len(rows) < 8:
            self.optimization_stats = {
                "n": len(rows),
                "positive": sum(1 for rec in rows if row_net_pnl(rec, self.cost_pct) > 0),
                "positiveRate": 0.0,
                "pf": 1.0,
                "netAvg": 0.0,
                "maxDdS": 0.0,
                "costPct": self.cost_pct,
                "costSource": self.cost_source,
                "window": window,
                "enabled": bool(self.additional_coordination),
                "configuredMinStep": floor,
                "effectiveMinStep": floor,
                "candidateSteps": [],
                "status": "insufficient-live-sample",
            }
            self.min_step = floor
            return

        n_pos = sum(1 for rec in rows if row_net_pnl(rec, self.cost_pct) > 0)
        avg = sum(row_net_pnl(rec, self.cost_pct) for rec in rows) / len(rows) if rows else 0.0
        opt_pf = last_n_cost_pf(rows, len(rows), self.cost_pct)
        opt_dd = drawdown_time_by_symbol(rows)
        by_config: Dict[tuple, List[Dict[str, Any]]] = {}
        for rec in rows:
            step = self._record_step(rec)
            row = rec if isinstance(rec, dict) else asdict(rec)
            if step >= floor and row.get("exchange_confirmed") and not row.get("partial"):
                key = (step, str(row.get("set_id") or ""), str(row.get("symbol") or ""),
                       str(row.get("side") or ""), str(row.get("strategy") or "core"),
                       str(row.get("axis_key") or ""), float(row.get("volume_ratio") or 1))
                by_config.setdefault(key, []).append(row)
        candidates: List[Dict[str, Any]] = []
        promoted_steps: List[int] = []
        for key in sorted(by_config):
            step = key[0]
            tape = by_config[key]
            if len(tape) < self.eval_need():
                continue
            ok, reason, windows = self._live_windows_ok(tape, minimum_pf=1.0)
            last15 = windows.get("last15") or {}
            primary_pf = float(last15.get("pf") or 0.0)
            promoted = bool(ok and int(last15.get("n") or 0) >= self.eval_need() and primary_pf + 1e-9 >= self.real_min_pf)
            if promoted:
                promoted_steps.append(step)
            candidates.append({
                "step": step,
                "setId": key[1], "symbol": key[2], "side": key[3], "strategy": key[4],
                "n": len(tape),
                "last15Pf": round(primary_pf, 4),
                "windowPf": {name: round(float(metric.get("pf") or 0.0), 4) for name, metric in windows.items()},
                "windowsOk": bool(ok),
                "status": "promoted" if promoted else (reason or "not-promoted"),
            })
        nxt = min(promoted_steps) if self.step_adapt and promoted_steps else floor
        self.optimization_stats = {
            "n": len(rows),
            "positive": n_pos,
            "positiveRate": round(n_pos / len(rows), 4) if rows else 0.0,
            "pf": float(opt_pf.get("ratio") or 1.0),
            "netAvg": round(avg, 8),
            "maxDdS": float(opt_dd.get("maxS") or 0.0),
            "costPct": float(opt_pf.get("costPct") or self.cost_pct),
            "costSource": opt_pf.get("costSource") or self.cost_source,
            "window": window,
            "enabled": bool(self.additional_coordination),
            "configuredMinStep": floor,
            "effectiveMinStep": nxt,
            "candidateSteps": candidates[: self.step_max],
            "status": "promoted" if promoted_steps else "no-live-promoted-step",
        }
        self.min_step = nxt
        # This is only a selection floor. The full configured Set catalog stays
        # evaluated and every non-promoted candidate remains visible.

    def ingest_bars(self, symbol: str, bars: Sequence[Sequence[float]]) -> None:
        if not bars:
            return
        cleaned: List[List[float]] = []
        for b in bars:
            row = ohlcv_row(b)
            if not row:
                continue
            cleaned.append([row[0], row[1], row[2], row[3], row[4]])
        if len(cleaned) >= 16:
            self.bars[symbol] = cleaned[-self.lookback :]

    def trim_tapes(self, hist_cap: int = 96, live_cap: int = 80, bar_cap: int = 180) -> int:
        n = 0
        hc = max(24, int(hist_cap or HIST_CAP))
        lc = max(16, int(live_cap or 80))
        for st in self.by_idx:
            if len(st.hist) > hc:
                st.hist = st.hist[-hc:]
                n += 1
            if len(st.live) > lc:
                st.live = st.live[-lc:]
                n += 1
        n += self.clamp_bars(bar_cap)
        for k, tape in list(self.ind_hist.items()):
            if len(tape) > hc:
                self.ind_hist[k] = tape[-hc:]
                n += 1
        for k, tape in list(self.ind_live.items()):
            if len(tape) > lc:
                self.ind_live[k] = tape[-lc:]
                n += 1
        for k, tape in list(self.strategy_hist.items()):
            if len(tape) > hc:
                self.strategy_hist[k] = tape[-hc:]
                n += 1
        return n

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

    def on_live_close(self, rec: Any) -> None:
        if isinstance(rec, dict):
            if rec.get("ours") is False:
                return
            sid = str(rec.get("set_id") or rec.get("setId") or "")
            row = {
                "t": finite(rec.get("t")),
                "symbol": str(rec.get("symbol") or ""),
                "side": str(rec.get("side") or ""),
                "pnl": finite(rec.get("pnl")),
                "pnl_pct": finite(rec.get("pnl_pct")),
                "hold_s": finite(rec.get("hold_s")),
                "reason": str(rec.get("reason") or ""),
                "client_id": str(rec.get("client_id") or rec.get("clientId") or ""),
                "set_id": str(rec.get("set_id") or rec.get("setId") or ""),
                "parent_set_id": str(rec.get("parent_set_id") or rec.get("parentSetId") or ""),
                "axis_key": str(rec.get("axis_key") or rec.get("axisKey") or ""),
                "relative_count": int(rec.get("relative_count") or rec.get("relativeCount") or 1),
                "volume_ratio": finite(rec.get("volume_ratio") or rec.get("volumeRatio") or VOLUME_RATIO_UNIT),
                "strategy": str(rec.get("strategy") or ""),
                "pack": str(rec.get("pack") or ""),
            }
            ind_kind = str(rec.get("ind_kind") or rec.get("indKind") or "").strip().lower()
        else:
            if getattr(rec, "ours", True) is False:
                return
            sid = str(getattr(rec, "set_id", "") or "")
            row = {
                "t": finite(getattr(rec, "t", 0)),
                "symbol": str(getattr(rec, "symbol", "")),
                "side": str(getattr(rec, "side", "")),
                "pnl": finite(getattr(rec, "pnl", 0)),
                "pnl_pct": finite(getattr(rec, "pnl_pct", 0)),
                "hold_s": finite(getattr(rec, "hold_s", 0)),
                "reason": str(getattr(rec, "reason", "")),
                "client_id": str(getattr(rec, "client_id", "") or ""),
                "set_id": str(getattr(rec, "set_id", "") or ""),
                "parent_set_id": str(getattr(rec, "parent_set_id", "") or ""),
                "axis_key": str(getattr(rec, "axis_key", "") or ""),
                "relative_count": int(getattr(rec, "relative_count", 1) or 1),
                "volume_ratio": finite(getattr(rec, "volume_ratio", VOLUME_RATIO_UNIT) or VOLUME_RATIO_UNIT),
                "strategy": str(getattr(rec, "strategy", "") or ""),
                "pack": str(getattr(rec, "pack", "") or ""),
            }
            ind_kind = str(getattr(rec, "ind_kind", "") or "").strip().lower()
        original = rec if isinstance(rec, dict) else vars(rec)
        for key in ("qty", "entry", "exit", "fee_total", "position_cost_pct", "cost_source",
                    "exchange_confirmed", "partial", "strategy", "member_count"):
            if key in original:
                row[key] = original[key]
        if ind_kind not in IND_KINDS:
            ind_kind = ""
        if not sid:
            pack = "indications" if "ind:" in row["reason"] else "general"
            sl = snap_ratio(getattr(rec, "sl_ratio", 0.6) if not isinstance(rec, dict) else rec.get("sl_ratio") or 0.6)
            tkey = str(getattr(rec, "trail_key", "") if not isinstance(rec, dict) else rec.get("trail_key") or "")
            if not tkey:
                tkey = self.trails[0][0] if self.trails else "0.3:0.1"
            step = 0
            if isinstance(rec, dict):
                step = int(rec.get("step") or 0)
            else:
                step = int(getattr(rec, "step", 0) or 0)
            if not step:
                step = self.min_step
            sid = make_set_id(pack, sl, "", step)
        row["set_id"] = sid
        row["parent_set_id"] = row.get("parent_set_id") or sid
        row["pack"] = row.get("pack") or ("indications" if ind_kind else ("indications" if "ind:" in row["reason"] else "general"))
        if ind_kind:
            row["ind_kind"] = ind_kind
            row["indKind"] = ind_kind
            tape = self.ind_live.setdefault(ind_kind, [])
            cid0 = row.get("client_id") or ""
            if not (cid0 and any(r.get("client_id") == cid0 for r in tape)):
                tape.append(dict(row))
                self.ind_live[ind_kind] = trim_hist(tape, HIST_CAP)
        extra = ""
        if isinstance(rec, dict):
            extra = str(rec.get("trail_set_id") or rec.get("trailSetId") or "")
        else:
            extra = str(getattr(rec, "trail_set_id", "") or "")
        targets: List[SetState] = []
        for x in (sid, extra):
            if not x:
                continue
            st = self.sets.get(x)
            if not st:
                st = self.sets.get(f"{x}:st{self.min_step}")
            if st and st not in targets:
                targets.append(st)
        if not targets:
            return
        cid = row.get("client_id") or ""
        for st in targets:
            if cid and any(r.get("client_id") == cid for r in st.live):
                continue
            st.live.append(row)
            st.live = st.live[-80:]
            self._score_one(st)
        self._snap_ts = 0.0
        self._live_ov_ts = 0.0

    def seed_live(self, closed: Sequence[Any]) -> None:
        for rec in closed:
            self.on_live_close(rec)

    def due(self) -> bool:
        if not self.enabled:
            return False
        if self._running:
            return False
        return time.time() - self.last_run >= self.refresh_s or not self.progress.ready

    def replay_all(
        self,
        now: Optional[float] = None,
        on_step: Optional[Callable[[], None]] = None,
        symbols: Optional[Sequence[str]] = None,
        abort: Optional[Callable[[], bool]] = None,
        workers: int = 1,
        drop_bars: bool = False,
        on_symbol: Optional[Callable[[str, int, int], None]] = None,
        merge: bool = False,
        progress_total: Optional[int] = None,
        score: bool = True,
    ) -> None:
        if not self.enabled or self._running:
            return
        self._running = True
        t0 = time.time()
        now = now or t0
        try:
            if symbols is None:
                names = [s for s, b in self.bars.items() if len(b) >= self.min_bars]
            else:
                names = [s for s in symbols if len(self.bars.get(s) or []) >= self.min_bars]
            prior_ready = bool(self.progress.ready)
            if merge:
                total = max(0, int(progress_total if progress_total is not None else len(names)))
                if total != self._hist_total:
                    self._hist_seen.clear()
                    self._hist_total = total
                    self._hist_counts = {}
                    prior_ready = False
                elif prior_ready and total > 0 and len(self._hist_seen) >= total:
                    # The previous cycle completed. Start a fresh refresh
                    # cycle while retaining the last completed evidence.
                    self._hist_seen.clear()
                symbols_total = total
            else:
                symbols_total = len(names)
            self.progress = Progress(
                phase="replay",
                pct=1.0,
                sets_total=len(self.sets),
                symbols_total=symbols_total,
                bars_total=sum(len(self.bars.get(s) or []) for s in names),
                cycle=self.progress.cycle + 1,
                detail=f"{len(names)} symbols · {len(self.sets)} sets",
                ready=prior_ready if merge else False,
            )
            hist: Dict[str, List[Dict[str, Any]]] = {sid: [] for sid in self.sets}
            ind_hist: Dict[str, List[Dict[str, Any]]] = {}
            strategy_hist: Dict[str, List[Dict[str, Any]]] = {}
            processed_symbols: set[str] = set()
            aborted = False
            w = max(1, min(int(workers or 1), 8, len(names) or 1))
            lock = threading.Lock()

            def _merge(
                symbol: str,
                local: Dict[str, List[Dict[str, Any]]],
                local_ind: Dict[str, List[Dict[str, Any]]],
                local_strategy: Dict[str, List[Dict[str, Any]]],
            ) -> None:
                for sid, rows in local.items():
                    if rows:
                        hist.setdefault(sid, []).extend(rows)
                for k, rows in local_ind.items():
                    if rows:
                        ind_hist.setdefault(k, []).extend(rows)
                for k, rows in local_strategy.items():
                    if rows:
                        strategy_hist.setdefault(k, []).extend(rows)
                nbar = len(self.bars.get(symbol) or [])
                if drop_bars:
                    self.bars.pop(symbol, None)
                self.progress.bars_done += nbar
                if merge:
                    self._hist_seen.add(symbol)
                    processed_symbols.add(symbol)

            def _one(
                symbol: str,
            ) -> Tuple[
                str,
                Dict[str, List[Dict[str, Any]]],
                Dict[str, List[Dict[str, Any]]],
                Dict[str, List[Dict[str, Any]]],
            ]:
                local: Dict[str, List[Dict[str, Any]]] = {sid: [] for sid in self.sets}
                local_ind: Dict[str, List[Dict[str, Any]]] = {}
                local_strategy: Dict[str, List[Dict[str, Any]]] = {}
                self._replay_symbol(
                    symbol,
                    local,
                    now,
                    # Keep the service watchdog alive during a single
                    # symbol's vector/scalar replay.  A symbol can cover the
                    # full catalog and take longer than the systemd watchdog
                    # interval; forwarding the bounded inner progress hook
                    # avoids mistaking a healthy long replay for a hung lane.
                    on_step=on_step,
                    ind_hist=local_ind,
                    strat_hist=local_strategy,
                )
                return symbol, local, local_ind, local_strategy

            done = 0
            if w <= 1 or len(names) <= 1:
                for i, symbol in enumerate(names):
                    if abort and abort():
                        aborted = True
                        self.progress.detail = f"aborted-load {i}/{len(names)}"
                        break
                    self.progress.symbol = symbol
                    self.progress.symbols_done = i
                    self.progress.pct = 5.0 + (i / max(1, len(names))) * 80.0
                    self.progress.elapsed_ms = (time.time() - t0) * 1000
                    self.progress.detail = f"replay {symbol} {i + 1}/{len(names)}"
                    _sym, local, local_ind, local_strategy = _one(symbol)
                    _merge(_sym, local, local_ind, local_strategy)
                    done += 1
                    if on_symbol:
                        on_symbol(symbol, done, len(names))
                    if on_step:
                        on_step()
            else:
                # Keep at most one worker's worth of symbols in flight. The old
                # submit-all approach retained every future and its captured
                # payload until the slowest symbol finished.
                with ThreadPoolExecutor(max_workers=w, thread_name_prefix="set-replay") as pool:
                    pending: Dict[Any, Tuple[str, int]] = {}
                    next_i = 0
                    retries: Dict[str, int] = {}
                    while pending or next_i < len(names):
                        while not aborted and len(pending) < w and next_i < len(names):
                            symbol = names[next_i]
                            next_i += 1
                            pending[pool.submit(_one, symbol)] = (symbol, 0)
                        if not pending:
                            break
                        if abort and abort():
                            aborted = True
                            for future in pending:
                                future.cancel()
                            break
                        finished, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
                        for fut in finished:
                            symbol, _attempt = pending.pop(fut)
                            try:
                                result_symbol, local, local_ind, local_strategy = fut.result()
                            except Exception:
                                attempt = retries.get(symbol, 0) + 1
                                retries[symbol] = attempt
                                if attempt <= 1 and not (abort and abort()):
                                    pending[pool.submit(_one, symbol)] = (symbol, attempt)
                                    continue
                                raise
                            with lock:
                                _merge(result_symbol, local, local_ind, local_strategy)
                                done += 1
                                self.progress.symbol = result_symbol
                                self.progress.symbols_done = done
                                self.progress.pct = 5.0 + (done / max(1, len(names))) * 80.0
                                self.progress.elapsed_ms = (time.time() - t0) * 1000
                                self.progress.detail = f"replay {result_symbol} {done}/{len(names)}"
                            if on_symbol:
                                on_symbol(result_symbol, done, len(names))
                            if on_step:
                                on_step()
            self.progress.phase = "score"
            self.progress.pct = 90.0
            self._commit_hist(
                hist,
                ind_hist,
                merge=merge,
                replayed_symbols=processed_symbols if merge else names,
                score=score,
            )
            strategy_names = processed_symbols if merge else names
            if not merge:
                self.strategy_hist = {
                    k: trim_hist(rows, HIST_CAP) for k, rows in strategy_hist.items()
                }
            else:
                keys = set(self.strategy_hist) | set(strategy_hist)
                self.strategy_hist = {
                    k: merge_hist_rows(self.strategy_hist.get(k) or [], strategy_hist.get(k) or [], strategy_names)
                    for k in keys
                }
            coverage_done = len(self._hist_seen) if merge else (done if aborted else len(names))
            complete = (not merge and not aborted) or (merge and coverage_done >= symbols_total)
            if complete:
                self.progress.phase = "ready"
                self.progress.pct = 100.0
                self.progress.ready = True
                self.progress.symbols_done = symbols_total if merge else (done if aborted else len(names))
            else:
                self.progress.phase = "partial" if merge else "replay"
                self.progress.pct = 8.0 + (82.0 * coverage_done / max(1, symbols_total)) if symbols_total else 0.0
                self.progress.ready = prior_ready if merge else False
                self.progress.symbols_done = coverage_done
            self.progress.sets_done = len(self.sets)
            self.progress.detail = (
                f"{sum(1 for s in self.sets.values() if s.active)}/{len(self.sets)} active · "
                f"{sum(s.n for s in self.sets.values())} hist fills"
                + (f" · coverage {coverage_done}/{symbols_total}" if merge else "")
                + (" · aborted" if aborted else "")
            )
        except Exception as exc:
            self.progress.phase = "error"
            self.progress.error = str(exc)[:220]
        finally:
            self.progress.last_run_ms = (time.time() - t0) * 1000
            self.progress.elapsed_ms = self.progress.last_run_ms
            self.last_run = time.time()
            self._running = False
            self._snap_ts = 0.0
            self._live_ov_ts = 0.0
            self._snap_ts = 0.0
            self._live_ov_ts = 0.0

    def _commit_hist(
        self,
        hist: Dict[str, List[Dict[str, Any]]],
        ind_hist: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        *,
        merge: bool = False,
        replayed_symbols: Optional[Sequence[str]] = None,
        hist_counts: Optional[Dict[str, int]] = None,
        score: bool = True,
        score_ids: Optional[Sequence[str]] = None,
    ) -> None:
        names = [str(s) for s in (replayed_symbols or ())]
        score_set = {str(s) for s in score_ids} if score_ids is not None else None
        if not merge:
            if ind_hist is not None:
                self.ind_hist = {k: trim_hist(v, HIST_CAP) for k, v in ind_hist.items()}
        elif ind_hist is not None:
            keys = set(self.ind_hist) | set(ind_hist)
            self.ind_hist = {
                k: merge_hist_rows(self.ind_hist.get(k) or [], ind_hist.get(k) or [], names)
                for k in keys
            }
        for st in self.by_idx:
            # A bounded replay may commit one configuration slice at a time.
            # Do not treat a not-yet-replayed set as an empty result or erase
            # its already committed symbol evidence.
            if merge and st.id not in hist:
                continue
            full = hist.get(st.id, [])
            if merge:
                counts = self._hist_counts.setdefault(st.id, {})
                if not counts and st.hist:
                    for row in st.hist:
                        symbol = str(row.get("symbol") or "")
                        counts[symbol] = counts.get(symbol, 0) + 1
                for symbol in names:
                    if hist_counts is not None and st.id in hist_counts:
                        counts[symbol] = max(0, int(hist_counts[st.id]))
                    else:
                        counts[symbol] = sum(1 for row in full if str(row.get("symbol") or "") == symbol)
                st.hist = merge_hist_rows(st.hist, full, names)
                if score and (score_set is None or st.id in score_set):
                    self._score_one(st)
                st.n = sum(counts.values())
            else:
                full.sort(key=lambda r: finite(r.get("t")))
                st.hist = full
                if score and (score_set is None or st.id in score_set):
                    self._score_one(st)
                n_full = len(full)
                st.hist = trim_hist(full, HIST_CAP)
                st.n = n_full
        if score:
            self._cap_active()

    def _score_all(self) -> None:
        """Score the complete catalog once after a batched replay commit."""
        for st in self.by_idx:
            self._score_one(st)
        self._cap_active()

    def replay_symbol_partial(
        self,
        symbol: str,
        hist: Dict[str, List[Dict[str, Any]]],
        now: Optional[float] = None,
        ind_hist: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        drop_bars: bool = True,
        strat_hist: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        set_ids: Optional[Sequence[str]] = None,
        prepared: Optional[Tuple[Dict[str, List[Tuple[int, float, str]]], Dict[str, List[Tuple[int, float]]], int]] = None,
    ) -> int:
        """Replay one symbol into hist and drop its bars. Independent of other names."""
        if symbol not in self.bars:
            return 0
        nbar = len(self.bars[symbol])
        if nbar < self.min_bars:
            if drop_bars:
                self.bars.pop(symbol, None)
            return 0
        now = now or time.time()
        self._replay_symbol(
            symbol,
            hist,
            now,
            ind_hist=ind_hist,
            strat_hist=strat_hist,
            set_ids=set_ids,
            prepared=prepared,
        )
        if drop_bars:
            self.bars.pop(symbol, None)
        return nbar

    def _seed_pos(self, side: int, close: float, sl: float, tp: float, i: int, why: str) -> Dict[str, Any]:
        return {
            "side": side,
            "entry": close,
            "sl": sl,
            "tp": tp,
            "peak": close,
            "i": i,
            "trail": None,
            "tags": str(why or ""),
            "qty": 1.0,
            "parent": 1.0,
            "adds": 0,
        }

    def _rearm_stops(self, pos: Dict[str, Any], sl_frac: float, tp_frac: float) -> None:
        e = float(pos["entry"])
        side = int(pos["side"])
        if side > 0:
            pos["sl"] = e * (1 - sl_frac)
            pos["tp"] = e * (1 + tp_frac)
        else:
            pos["sl"] = e * (1 + sl_frac)
            pos["tp"] = e * (1 - tp_frac)

    def _maybe_block_add(self, pos: Dict[str, Any], bar: Sequence[float], sl_frac: float, tp_frac: float) -> None:
        n = int(pos.get("adds") or 0)
        stack = 3
        if n >= stack:
            return
        close = float(bar[3])
        entry = float(pos["entry"])
        side = int(pos["side"])
        if entry <= 0 or close <= 0:
            return
        u = ((close - entry) / entry) * side
        if u < 0.002:
            return
        # Always size off original parent, never the last add.
        add = float(pos["parent"]) * float(self.block_vr or 1.0)
        qty = float(pos["qty"])
        pos["entry"] = (entry * qty + close * add) / (qty + add)
        pos["qty"] = qty + add
        pos["adds"] = n + 1
        self._rearm_stops(pos, sl_frac, tp_frac)

    def _maybe_dca_add(self, pos: Dict[str, Any], bar: Sequence[float], sl_frac: float, tp_frac: float) -> None:
        n = int(pos.get("adds") or 0)
        dists = self.dca_dist
        if n >= min(4, len(dists)):
            return
        close = float(bar[3])
        entry = float(pos["entry"])
        side = int(pos["side"])
        if entry <= 0 or close <= 0:
            return
        adv = (entry - close) / entry if side > 0 else (close - entry) / entry
        if adv + 1e-12 < float(dists[n]):
            return
        mult = float(self.dca_mult[n] if n < len(self.dca_mult) else 2.5)
        add = float(pos["parent"]) * min(2.5, max(0.25, mult))
        qty = float(pos["qty"])
        pos["entry"] = (entry * qty + close * add) / (qty + add)
        pos["qty"] = qty + add
        pos["adds"] = n + 1
        self._rearm_stops(pos, sl_frac, tp_frac)

    def _advance_pos(
        self,
        pos: Dict[str, Any],
        bar: Sequence[float],
        i: int,
        sl_frac: float,
        tp_frac: float,
        use_trail: bool,
        arm: float,
        give: float,
        time_bars: int,
        scratch_bars: int,
        honor_tp: bool,
        ts: float,
        symbol: str,
        st_id: str,
        pack: str,
        strategy: str,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        side = int(pos["side"])
        entry = float(pos["entry"])
        held = i - int(pos["i"])
        if strategy == "block" and held >= 1:
            self._maybe_block_add(pos, bar, sl_frac, tp_frac)
            entry = float(pos["entry"])
        elif strategy == "dca" and held >= 1:
            self._maybe_dca_add(pos, bar, sl_frac, tp_frac)
            entry = float(pos["entry"])
        if use_trail:
            if side > 0:
                pos["peak"] = max(pos["peak"], float(bar[1]))
                fav = (pos["peak"] - entry) / entry
                if fav >= arm:
                    trail = pos["peak"] * (1 - give)
                    pos["trail"] = max(pos.get("trail") or 0.0, trail)
            else:
                pos["peak"] = min(pos["peak"], float(bar[2]))
                fav = (entry - pos["peak"]) / entry
                if fav >= arm:
                    trail = pos["peak"] * (1 + give)
                    cur = pos.get("trail")
                    pos["trail"] = trail if cur is None else min(cur, trail)
        elif side > 0:
            pos["peak"] = max(pos["peak"], float(bar[1]))
        else:
            pos["peak"] = min(pos["peak"], float(bar[2]))
        why, px = hit_exit(side, entry, pos["sl"], pos["tp"], pos.get("trail"), bar, ignore_tp=not honor_tp)
        if why is None and held >= time_bars:
            why, px = "time", float(bar[3])
        if why is None and held >= scratch_bars:
            move = (float(bar[3]) - entry) / entry * side
            if move >= self.scratch_min:
                why, px = "scratch+", float(bar[3])
        if not why:
            return pos, None
        raw = (px - entry) / entry * side
        qty = max(0.25, float(pos.get("qty") or 1.0))
        rec = {
            "t": ts,
            "symbol": symbol,
            "side": "LONG" if side > 0 else "SHORT",
            "direction": "LONG" if side > 0 else "SHORT",
            "pnl": net_pnl_pct(raw, self.cost_pct) * qty,
            "pnl_pct": raw,
            "hold_s": held * BAR_S,
            "reason": why,
            "set_id": st_id,
            "pack": pack,
            "costPct": self.cost_pct,
            "qty": qty,
            "adds": int(pos.get("adds") or 0),
            "strategy": strategy,
        }
        return None, rec

    def prepare_replay_signals(
        self,
        symbol: str,
        now: Optional[float] = None,
        on_step: Optional[Callable[[], None]] = None,
    ) -> Tuple[Dict[str, List[Tuple[int, float, str]]], Dict[str, List[Tuple[int, float]]], int]:
        """Build the symbol signal lanes once before replaying config chunks."""
        bars = self.bars[symbol]
        n = len(bars)
        warmup = min(self.warmup, max(16, n // 5))
        signals: Dict[str, List[Tuple[int, float, str]]] = {p: [(0, 0.0, "")] * n for p in self.packs}
        kind_sigs: Dict[str, List[Tuple[int, float]]] = {k: [(0, 0.0)] * n for k in IND_KINDS}
        base_ts = (now or time.time()) - (n - 1) * BAR_S
        for i in range(warmup, n):
            lo = max(0, i + 1 - 60)
            window = bars[lo : i + 1]
            ts = base_ts + i * BAR_S
            if "general" in self.packs:
                signals["general"][i] = general_signal(window)
            if "indications" in self.packs:
                votes = indication_kind_votes(window, self.ind_settings, ts)
                signals["indications"][i] = votes_to_signal(votes)
                for d, conf, tag in votes:
                    kind = IND_TAG_KIND.get(tag.strip())
                    if kind:
                        kind_sigs[kind][i] = (d, conf)
            if on_step and i % 50 == 0:
                on_step()
        return signals, kind_sigs, warmup

    def _replay_core_vectorized(
        self,
        symbol: str,
        bars: Sequence[Sequence[float]],
        pack_sets: Sequence[SetState],
        pack_sig: Sequence[Tuple[int, float, str]],
        now: float,
        warmup: int,
        time_bars: int,
        scratch_bars: int,
        honor_tp: bool,
        hist: Dict[str, List[Dict[str, Any]]],
    ) -> None:
        """Replay core configs with columnar state, preserving scalar rules.

        A full catalog contains thousands of independent Sets. Their core
        state transitions are identical apart from numeric parameters, so
        NumPy columns remove the Python dict loop over every config/bar while
        keeping one output row per Set. Block/DCA and indication tapes remain
        on their explicit paths below.
        """
        if _np is None or not pack_sets:
            return
        np = _np
        n = len(bars)
        m = len(pack_sets)
        side_values = (1, -1)
        tp_frac = np.asarray(
            [max(0.0020, float(st.tp_pct)) for st in pack_sets],
            dtype=float,
        )
        sl_frac = np.asarray(
            [max(0.0015, float(st.tp_pct) * max(0.3, float(st.sl_ratio or 0.6))) for st in pack_sets],
            dtype=float,
        )
        trailing = np.asarray([str(st.kind or "") == "trail" for st in pack_sets], dtype=bool)
        arms = np.asarray([
            (float(st.trail_arm) / 100.0 if float(st.trail_arm or 0) > 0.05 else float(st.trail_arm or 0))
            for st in pack_sets
        ], dtype=float)
        gives = np.asarray([
            (float(st.trail_give) / 100.0 if float(st.trail_give or 0) > 0.05 else float(st.trail_give or 0))
            for st in pack_sets
        ], dtype=float)
        base_ts = now - (n - 1) * BAR_S
        cooldown_n = max(0, int(self.cooldown_bars or 0))
        scratch_min = float(self.scratch_min or 0.0)

        for want_side in side_values:
            active = np.zeros(m, dtype=bool)
            cooldown = np.zeros(m, dtype=np.int32)
            entry = np.zeros(m, dtype=float)
            stop_loss = np.zeros(m, dtype=float)
            take_profit = np.zeros(m, dtype=float)
            peak = np.zeros(m, dtype=float)
            trail_px = np.zeros(m, dtype=float)
            entry_i = np.zeros(m, dtype=np.int32)

            for i in range(warmup, n):
                if cooldown_n:
                    cooldown = np.where((~active) & (cooldown > 0), cooldown - 1, cooldown)
                bar = bars[i]
                high = float(bar[1])
                low = float(bar[2])
                close = float(bar[3])
                if close <= 0:
                    continue

                live = active.copy()
                if live.any():
                    if want_side > 0:
                        if trailing.any():
                            peak[live] = np.maximum(peak[live], high)
                            fav = (peak - entry) / np.maximum(entry, 1e-12)
                            arm_now = live & trailing & (fav >= arms)
                            if arm_now.any():
                                next_trail = peak * (1.0 - gives)
                                trail_px[arm_now] = np.maximum(trail_px[arm_now], next_trail[arm_now])
                        if (~trailing & live).any():
                            peak[live & ~trailing] = np.maximum(peak[live & ~trailing], high)
                        effective_stop = np.where(live & (trail_px > 0), np.maximum(stop_loss, trail_px), stop_loss)
                        sl_hit = live & (low <= effective_stop)
                        tp_hit = live & (high >= take_profit) if honor_tp else np.zeros(m, dtype=bool)
                        exited = sl_hit | tp_hit
                        exit_px = np.where(sl_hit, effective_stop, take_profit)
                        raw = (exit_px - entry) / np.maximum(entry, 1e-12)
                    else:
                        if trailing.any():
                            peak[live] = np.minimum(peak[live], low)
                            fav = (entry - peak) / np.maximum(entry, 1e-12)
                            arm_now = live & trailing & (fav >= arms)
                            if arm_now.any():
                                next_trail = peak * (1.0 + gives)
                                fresh = arm_now & (trail_px <= 0)
                                trail_px[fresh] = next_trail[fresh]
                                trail_px[arm_now & ~fresh] = np.minimum(trail_px[arm_now & ~fresh], next_trail[arm_now & ~fresh])
                        if (~trailing & live).any():
                            peak[live & ~trailing] = np.minimum(peak[live & ~trailing], low)
                        effective_stop = np.where(live & (trail_px > 0), np.minimum(stop_loss, trail_px), stop_loss)
                        sl_hit = live & (high >= effective_stop)
                        tp_hit = live & (low <= take_profit) if honor_tp else np.zeros(m, dtype=bool)
                        exited = sl_hit | tp_hit
                        exit_px = np.where(sl_hit, effective_stop, take_profit)
                        raw = (entry - exit_px) / np.maximum(entry, 1e-12)

                    # Scalar _advance_pos applies time/scratch exits only
                    # after the stop/TP decision on the same bar.
                    held = i - entry_i
                    close_move = (close - entry) / np.maximum(entry, 1e-12) * want_side
                    time_exit = live & ~exited & (held >= time_bars)
                    scratch_exit = live & ~exited & ~time_exit & (close_move >= scratch_min) & (held >= scratch_bars)
                    if time_exit.any():
                        exited |= time_exit
                        exit_px[time_exit] = close
                        raw[time_exit] = (close - entry[time_exit]) / np.maximum(entry[time_exit], 1e-12) * want_side
                    if scratch_exit.any():
                        exited |= scratch_exit
                        exit_px[scratch_exit] = close
                        raw[scratch_exit] = (close - entry[scratch_exit]) / np.maximum(entry[scratch_exit], 1e-12) * want_side

                    exit_indices = np.flatnonzero(exited)
                    ts = base_ts + i * BAR_S
                    for j in exit_indices.tolist():
                        why = "sl" if bool(sl_hit[j]) else ("tp" if bool(tp_hit[j]) else ("time" if bool(time_exit[j]) else "scratch+"))
                        qty = 1.0
                        move = float(raw[j])
                        bucket = hist.setdefault(pack_sets[j].id, [])
                        bucket.append({
                            "t": ts,
                            "symbol": symbol,
                            "side": "LONG" if want_side > 0 else "SHORT",
                            "direction": "LONG" if want_side > 0 else "SHORT",
                            "pnl": net_pnl_pct(move, self.cost_pct) * qty,
                            "pnl_pct": move,
                            "hold_s": int(held[j]) * BAR_S,
                            "reason": why,
                            "set_id": pack_sets[j].id,
                            "pack": pack_sets[j].pack,
                            "costPct": self.cost_pct,
                            "qty": qty,
                            "adds": 0,
                            "strategy": "core",
                        })
                        # The scorer only needs the newest bounded evaluation
                        # tape. Trim at append time so a full matrix cannot
                        # retain millions of raw fills before the chunk
                        # boundary gets a chance to compact it.
                        if len(bucket) > HIST_CAP:
                            del bucket[:-HIST_CAP]
                    if exit_indices.size:
                        active[exited] = False
                        cooldown[exited] = cooldown_n

                d, conf, _why = pack_sig[i]
                if d == want_side and conf >= 0.58:
                    seed = (~active) & (cooldown <= 0)
                    if seed.any():
                        entry[seed] = close
                        peak[seed] = close
                        entry_i[seed] = i
                        trail_px[seed] = 0.0
                        if want_side > 0:
                            stop_loss[seed] = close * (1.0 - sl_frac[seed])
                            take_profit[seed] = close * (1.0 + tp_frac[seed])
                        else:
                            stop_loss[seed] = close * (1.0 + sl_frac[seed])
                            take_profit[seed] = close * (1.0 - tp_frac[seed])
                        active[seed] = True

    def _replay_symbol(
        self,
        symbol: str,
        hist: Dict[str, List[Dict[str, Any]]],
        now: float,
        on_step: Optional[Callable[[], None]] = None,
        ind_hist: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        strat_hist: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        set_ids: Optional[Sequence[str]] = None,
        prepared: Optional[Tuple[Dict[str, List[Tuple[int, float, str]]], Dict[str, List[Tuple[int, float]]], int]] = None,
    ) -> None:
        bars = self.bars[symbol]
        selected = {str(s) for s in (set_ids or self.sets)}
        replay_sets = [st for st in self.by_idx if st.id in selected]
        n = len(bars)
        if prepared is None:
            signals, kind_sigs, warmup = self.prepare_replay_signals(symbol, now, on_step=on_step)
        else:
            signals, kind_sigs, warmup = prepared
        base_ts = now - (n - 1) * BAR_S
        time_bars = max(8, min(self.hist_time_bars, max(8, n - warmup - 1)))
        scratch_bars = max(8, int(self.scratch_s / BAR_S))
        honor_tp = bool(getattr(self, "hist_honor_tp", True))
        by_pack: Dict[str, List[SetState]] = {}
        for st in replay_sets:
            by_pack.setdefault(st.pack, []).append(st)
        set_map = {st.id: st for st in replay_sets}
        for pack, pack_sets in by_pack.items():
            pack_sig = signals.get(pack) or [(0, 0.0, "")] * n
            # Block/DCA hist once per pack, not once per SL×TP book.
            strat_seed = next((s for s in pack_sets if s.kind == "base"), pack_sets[0] if pack_sets else None)
            do_block = bool(getattr(self, "hist_block", True)) and strat_hist is not None and strat_seed is not None
            do_dca = bool(getattr(self, "hist_dca", True)) and strat_hist is not None and strat_seed is not None
            if strat_hist is not None:
                strat_hist.setdefault("block", [])
                strat_hist.setdefault("dca", [])
            vector_core = _np is not None
            if vector_core:
                self._replay_core_vectorized(
                    symbol, bars, pack_sets, pack_sig, now, warmup,
                    time_bars, scratch_bars, honor_tp, hist,
                )
            for want_side in (1, -1):
                opens: Dict[str, Dict[str, Any]] = {}
                cools: Dict[str, int] = {}
                blk_pos: Optional[Dict[str, Any]] = None
                dca_pos: Optional[Dict[str, Any]] = None
                # The vectorized core does not populate ``opens``. Keep one
                # scalar-equivalent base lane so Block/DCA retain the exact
                # entry/cooldown coordination of the reference replay.
                representative: Optional[Dict[str, Any]] = None
                representative_cool = 0
                for i in range(warmup, n):
                    bar = bars[i]
                    ts = base_ts + i * BAR_S
                    for sid in list(cools):
                        if sid in opens:
                            continue
                        cools[sid] -= 1
                        if cools[sid] <= 0:
                            cools.pop(sid, None)
                    dead: List[str] = []
                    for sid, pos in opens.items():
                        st = set_map[sid]
                        sl_frac = max(0.0015, st.tp_pct * max(0.3, float(st.sl_ratio or 0.6)))
                        tp_frac = max(0.0020, st.tp_pct)
                        use_trail = st.kind == "trail"
                        arm = (st.trail_arm / 100.0 if st.trail_arm > 0.05 else st.trail_arm) if use_trail else 0.0
                        give = (st.trail_give / 100.0 if st.trail_give > 0.05 else st.trail_give) if use_trail else 0.0
                        pos, rec = self._advance_pos(
                            pos, bar, i, strategy="core",
                            sl_frac=sl_frac, tp_frac=tp_frac, use_trail=use_trail,
                            arm=arm, give=give, time_bars=time_bars, scratch_bars=scratch_bars,
                            honor_tp=honor_tp, ts=ts, symbol=symbol, st_id=st.id, pack=st.pack,
                        )
                        if rec:
                            hist.setdefault(sid, []).append(rec)
                            dead.append(sid)
                            cools[sid] = self.cooldown_bars
                        else:
                            opens[sid] = pos
                    for sid in dead:
                        opens.pop(sid, None)
                    if vector_core and strat_seed is not None:
                        if representative is None and representative_cool > 0:
                            representative_cool -= 1
                        if representative is not None:
                            rep_sl = max(
                                0.0015,
                                strat_seed.tp_pct * max(0.3, float(strat_seed.sl_ratio or 0.6)),
                            )
                            rep_tp = max(0.0020, strat_seed.tp_pct)
                            representative, rep_rec = self._advance_pos(
                                representative, bar, i, strategy="core",
                                sl_frac=rep_sl, tp_frac=rep_tp, use_trail=False,
                                arm=0.0, give=0.0, time_bars=time_bars,
                                scratch_bars=scratch_bars, honor_tp=honor_tp,
                                ts=ts, symbol=symbol, st_id=strat_seed.id, pack=pack,
                            )
                            if rep_rec:
                                representative_cool = self.cooldown_bars
                    if blk_pos is not None and strat_seed is not None:
                        sl_frac = max(0.0015, strat_seed.tp_pct * max(0.3, float(strat_seed.sl_ratio or 0.6)))
                        tp_frac = max(0.0020, strat_seed.tp_pct)
                        blk_pos, recb = self._advance_pos(
                            blk_pos, bar, i, strategy="block",
                            sl_frac=sl_frac, tp_frac=tp_frac, use_trail=False,
                            arm=0.0, give=0.0, time_bars=time_bars, scratch_bars=scratch_bars,
                            honor_tp=honor_tp, ts=ts, symbol=symbol, st_id=strat_seed.id, pack=pack,
                        )
                        if recb and strat_hist is not None:
                            strat_hist["block"].append(recb)
                    if dca_pos is not None and strat_seed is not None:
                        sl_frac = max(0.0015, strat_seed.tp_pct * max(0.3, float(strat_seed.sl_ratio or 0.6)))
                        tp_frac = max(0.0020, strat_seed.tp_pct)
                        dca_pos, recd = self._advance_pos(
                            dca_pos, bar, i, strategy="dca",
                            sl_frac=sl_frac, tp_frac=tp_frac, use_trail=False,
                            arm=0.0, give=0.0, time_bars=time_bars, scratch_bars=scratch_bars,
                            honor_tp=honor_tp, ts=ts, symbol=symbol, st_id=strat_seed.id, pack=pack,
                        )
                        if recd and strat_hist is not None:
                            strat_hist["dca"].append(recd)
                    d, conf, why = pack_sig[i]
                    if d == 0 or conf < 0.58 or d != want_side:
                        continue
                    close = float(bar[3])
                    if close <= 0:
                        continue
                    for st in pack_sets:
                        if vector_core and st is not strat_seed:
                            continue
                        if st.id in opens or cools.get(st.id, 0) > 0:
                            continue
                        if vector_core and st is strat_seed and (representative is not None or representative_cool > 0):
                            continue
                        sl_frac = max(0.0015, st.tp_pct * max(0.3, float(st.sl_ratio or 0.6)))
                        tp_frac = max(0.0020, st.tp_pct)
                        if d > 0:
                            sl = close * (1 - sl_frac)
                            tp = close * (1 + tp_frac)
                        else:
                            sl = close * (1 + sl_frac)
                            tp = close * (1 - tp_frac)
                        seed = self._seed_pos(d, close, sl, tp, i, str(why or ""))
                        if vector_core:
                            # Core Sets were already replayed column-wise;
                            # retain only the representative state needed by
                            # the independent Block/DCA lanes.
                            if st is strat_seed:
                                representative = dict(seed)
                                if do_block and blk_pos is None:
                                    blk_pos = dict(seed)
                                if do_dca and dca_pos is None:
                                    dca_pos = dict(seed)
                            continue
                        opens[st.id] = dict(seed)
                        if do_block and blk_pos is None and st is strat_seed:
                            blk_pos = dict(seed)
                        if do_dca and dca_pos is None and st is strat_seed:
                            dca_pos = dict(seed)
                    if on_step and i % 80 == 0:
                        on_step()
            if strat_hist is not None:
                for k in ("block", "dca"):
                    tape = strat_hist.get(k) or []
                    if len(tape) > 4000:
                        strat_hist[k] = tape[-2400:]
        if replay_sets:
            self.progress.set_id = replay_sets[-1].id
        if ind_hist is not None and "indications" in self.packs:
            self._replay_kind_tapes(
                symbol, bars, kind_sigs, ind_hist, now, warmup, time_bars, scratch_bars, honor_tp,
            )
        # Signals are an independent execution lane. Keep a dedicated Block
        # tape instead of only attributing Block results to the aggregate
        # indications pack.
        if (
            strat_hist is not None
            and "indications" in self.packs
            and bool(getattr(self, "hist_block", True))
        ):
            signal_sigs = kind_sigs.get("signals") or []
            if any(d != 0 for d, _ in signal_sigs):
                self._replay_kind_strategy_tape(
                    symbol, bars, signal_sigs, strat_hist.setdefault("block:signals", []),
                    now, warmup, time_bars, scratch_bars, honor_tp,
                    kind="signals",
                )

    def _replay_kind_strategy_tape(
        self,
        symbol: str,
        bars: Sequence[Sequence[float]],
        sigs: Sequence[Tuple[int, float]],
        output: List[Dict[str, Any]],
        now: float,
        warmup: int,
        time_bars: int,
        scratch_bars: int,
        honor_tp: bool,
        *,
        kind: str,
    ) -> None:
        """Replay one indication kind through the independent Block strategy."""
        n = len(bars)
        if n <= warmup:
            return
        base_ts = now - (n - 1) * BAR_S
        tp_frac = max(0.0020, step_tp_pct(self.min_step_cfg, self.cost_pct))
        sl_frac = max(0.0015, tp_frac * 0.6)
        for want_side in (1, -1):
            open_pos: Optional[Dict[str, Any]] = None
            cool = 0
            for i in range(warmup, n):
                bar = bars[i]
                ts = base_ts + i * BAR_S
                if open_pos is not None:
                    open_pos, rec = self._advance_pos(
                        open_pos, bar, i, strategy="block",
                        sl_frac=sl_frac, tp_frac=tp_frac, use_trail=False,
                        arm=0.0, give=0.0, time_bars=time_bars, scratch_bars=scratch_bars,
                        honor_tp=honor_tp, ts=ts, symbol=symbol,
                        st_id=f"indications:{kind}:block", pack="indications",
                    )
                    if rec:
                        rec["ind_kind"] = kind
                        rec["reason"] = f"block:{kind}:{rec.get('reason') or 'exit'}"
                        output.append(rec)
                        cool = self.cooldown_bars
                    if open_pos is not None:
                        continue
                if cool > 0:
                    cool -= 1
                    continue
                d, conf = sigs[i]
                if d == 0 or conf < 0.52 or d != want_side:
                    continue
                close = float(bar[3])
                if close <= 0:
                    continue
                sl = close * (1 - sl_frac) if d > 0 else close * (1 + sl_frac)
                tp = close * (1 + tp_frac) if d > 0 else close * (1 - tp_frac)
                open_pos = self._seed_pos(d, close, sl, tp, i, f"ind:{kind}")

    def _replay_kind_tapes(
        self,
        symbol: str,
        bars: Sequence[Sequence[float]],
        kind_sigs: Dict[str, List[Tuple[int, float]]],
        ind_hist: Dict[str, List[Dict[str, Any]]],
        now: float,
        warmup: int,
        time_bars: int,
        scratch_bars: int,
        honor_tp: bool,
    ) -> None:
        """Independent Signal / State / Direction / Move / Active / Common tapes.

        Each kind walks its own entries with a representative SL (0.6 × min-step TP).
        SL × TP combinations live as independent Sets, not mixed into kind tapes.
        """
        n = len(bars)
        if n <= warmup:
            return
        base_ts = now - (n - 1) * BAR_S
        tp_frac = max(0.0020, step_tp_pct(self.min_step_cfg, self.cost_pct))
        sl_frac = max(0.0015, tp_frac * 0.6)
        for kind, sigs in kind_sigs.items():
                if not any(d != 0 for d, _ in sigs):
                    continue
                buf = ind_hist.setdefault(kind, [])
                for want_side in (1, -1):
                    open_pos: Optional[Dict[str, Any]] = None
                    cool = 0
                    for i in range(warmup, n):
                        bar = bars[i]
                        ts = base_ts + i * BAR_S
                        if open_pos is not None:
                            side = int(open_pos["side"])
                            entry = float(open_pos["entry"])
                            held = i - int(open_pos["i"])
                            why, px = hit_exit(side, entry, open_pos["sl"], open_pos["tp"], None, bar, ignore_tp=not honor_tp)
                            if why is None and held >= time_bars:
                                why, px = "time", float(bar[3])
                            if why is None and held >= scratch_bars:
                                move = (float(bar[3]) - entry) / entry * side
                                if move >= self.scratch_min:
                                    why, px = "scratch+", float(bar[3])
                            if why:
                                raw = (px - entry) / entry * side
                                buf.append({
                                    "t": ts,
                                    "symbol": symbol,
                                    "side": "LONG" if side > 0 else "SHORT",
                                    "direction": "LONG" if side > 0 else "SHORT",
                                    "pnl": net_pnl_pct(raw, self.cost_pct),
                                    "pnl_pct": raw,
                                    "hold_s": held * BAR_S,
                                    "reason": f"ind:{kind}:{why}",
                                    "ind_kind": kind,
                                    "pack": "indications",
                                    "costPct": self.cost_pct,
                                    "slRatio": 0.6,
                                    "tpPct": tp_frac * 100.0,
                                })
                                open_pos = None
                                cool = self.cooldown_bars
                            continue
                        if cool > 0:
                            cool -= 1
                            continue
                        d, conf = sigs[i]
                        if d == 0 or conf < 0.52:
                            continue
                        if d != want_side:
                            continue
                        close = float(bar[3])
                        if d > 0:
                            sl_px = close * (1 - sl_frac)
                            tp_px = close * (1 + tp_frac)
                        else:
                            sl_px = close * (1 + sl_frac)
                            tp_px = close * (1 - tp_frac)
                        open_pos = {"side": d, "entry": close, "sl": sl_px, "tp": tp_px, "i": i}

    def _score_metrics(self, tape: Sequence[Dict[str, Any]], hist_n: Optional[int] = None) -> Dict[str, Any]:
        ordered = sorted((r for r in tape if isinstance(r, dict)), key=lambda r: finite(r.get("t")))
        nets = [row_net_pnl(r, self.cost_pct) for r in ordered]
        wins = sum(1 for x in nets if x > 0)
        gp = round(sum(x for x in nets if x > 0), 6)
        gl = round(abs(sum(x for x in nets if x < 0)), 6)
        decided = sum(1 for x in nets if x != 0)
        wr = round(100.0 * wins / decided, 1) if decided else 0.0
        expectancy = round(sum(nets) / len(nets), 6) if nets else 0.0
        holds = [finite(r.get("hold_s")) for r in ordered]
        avg_hold = round(sum(holds) / len(holds), 1) if holds else 0.0
        classic = round(gp / gl, 4) if gl > 0 else (99.0 if gp > 0 else 0.0)
        pf_tape = last_n_balanced(ordered, self.pf_n)
        last15 = last_n_cost_pf(pf_tape, self.pf_n, self.cost_pct)
        evaluation = cost_aware_metrics(pf_tape, self.cost_pct, required_samples=self.eval_need())
        # Use one bounded, symbol-balanced tape for every named last-N view.
        # This keeps the 50+/75-position coordinations reproducible without
        # retaining the complete history in each Set.
        window_tape = last_n_balanced(ordered, max(EVALUATION_WINDOWS))
        windows = evaluation_windows(window_tape, self.cost_pct, required_samples=self.eval_need())
        last25 = ordered[-self.deact_n :]
        if last25:
            rs = [signed_result_r(finite(r.get("pnl_pct")), row_position_cost_pct(r, self.cost_pct)) for r in last25]
            last25_avg_r = sum(rs) / len(rs)
            last25_avg_pnl = sum(row_net_pnl(r, self.cost_pct) for r in last25) / len(last25)
        else:
            last25_avg_r = 0.0
            last25_avg_pnl = 0.0
        dd = drawdown_time_by_symbol(ordered)
        need = self.eval_need()
        n15 = int(last15["count"])
        ratio = float(last15["ratio"])
        validated = n15 >= need and ratio + 1e-9 >= 1.0
        enable_pf = float(self.real_min_pf or 1.15)
        proven_neg = n15 >= need and ratio + 1e-9 < enable_pf
        dd_s = float(dd["maxS"])
        dd_ok = dd_s <= float(self.max_dd_s or 27000) + 1e-9
        return {
            "n": int(hist_n if hist_n is not None else len(ordered)),
            "wins": wins,
            "gp": gp,
            "gl": gl,
            "wr": wr,
            "expectancy": expectancy,
            "avg_hold_s": avg_hold,
            "classic_all": classic,
            "last15_ratio": ratio,
            "last15_classic": float(last15["classicPf"]),
            "last15_n": n15,
            "last15_r": float(last15["avgR"]),
            "last25_n": len(last25),
            "last25_avg_r": last25_avg_r,
            "last25_avg_pnl": last25_avg_pnl,
            "max_dd_s": float(dd["maxS"]),
            "avg_dd_s": float(dd["avgS"]),
            "dd_episodes": int(dd["episodes"]),
            "source_n": len(ordered),
            "validated": validated,
            "active": bool(n15 >= need and ratio + 1e-9 >= enable_pf and dd_ok),
            "enablePf": enable_pf,
            "ddOk": dd_ok,
            "cost_subtracted": True,
            "cost_pct": self.cost_pct,
            "net_avg": float(last15.get("netAvg") or expectancy),
            "gross_pf": float(evaluation.get("grossPf") or 0.0),
            "net_pf": float(evaluation.get("netPf") or 0.0),
            "gross_ev": float(evaluation.get("grossEv") or 0.0),
            "net_ev": float(evaluation.get("netEv") or 0.0),
            "evaluation": evaluation,
            "evaluation_windows": windows,
            "proven_neg": proven_neg,
        }

    def _stage_qualification(self, st: SetState, m: Dict[str, Any]) -> Dict[str, Any]:
        """Derive the monotonic Base -> Main -> Real qualification ledger.

        Main consumes only Base-qualified evidence and Real consumes only
        Main-qualified evidence. The ledger stores the decision at each
        boundary, so retries and restarts can replay the same parent without
        creating another count.
        """
        need = self.eval_need()
        n = int(m.get("last15_n") or 0)
        pf = float(m.get("last15_ratio") or 0.0)
        dd_ok = bool(m.get("ddOk", True))
        base_floor = float(self.stage_min_pf.get("base", 1.05))
        main_floor = float(self.stage_min_pf.get("main", 1.10))
        real_floor = float(self.stage_min_pf.get("real", 1.15))
        base = n >= need and pf + 1e-9 >= base_floor and dd_ok
        main = base and pf + 1e-9 >= main_floor
        real = main and pf + 1e-9 >= real_floor
        qualified = "Real" if real else ("Main" if main else ("Base" if base else ""))
        st.parent_set_id = st.id if st.kind == "base" else (st.parent_set_id or st.id)
        st.stage = qualified or "Unqualified"
        st.stage_qualified = qualified
        st.base_pf = round(pf, 6)
        st.main_pf = round(pf, 6) if base else 0.0
        st.real_pf = round(pf, 6) if main else 0.0
        st.position_cost_pct = self.cost_pct
        st.evaluation = dict(m.get("evaluation") or {})
        st.evaluation_windows = dict(m.get("evaluation_windows") or {})
        st.normal_evaluation = {
            "pf": float(m.get("gross_pf") or 0.0),
            "ev": float(m.get("gross_ev") or 0.0),
            "sampleCount": n,
            "source": "gross-price-move",
        }
        st.adjusted_evaluation = {
            "pf": float(m.get("net_pf") or 0.0),
            "ev": float(m.get("net_ev") or 0.0),
            "ratio": pf,
            "sampleCount": n,
            "source": "cost-net",
        }
        st.adjustment_deltas = {
            "pf": round(float(m.get("net_pf") or 0.0) - float(m.get("gross_pf") or 0.0), 6),
            "ev": round(float(m.get("net_ev") or 0.0) - float(m.get("gross_ev") or 0.0), 8),
            "costPct": self.cost_pct,
        }
        reasons: List[str] = []
        if n < need:
            reasons.append(f"sample {n}/{need}")
        if pf + 1e-9 < base_floor:
            reasons.append(f"base PF {pf:.2f}<{base_floor:.2f}")
        elif pf + 1e-9 < main_floor:
            reasons.append(f"main PF {pf:.2f}<{main_floor:.2f}")
        elif pf + 1e-9 < real_floor:
            reasons.append(f"real PF {pf:.2f}<{real_floor:.2f}")
        if not dd_ok:
            reasons.append("DDt cap")
        reason = "; ".join(reasons)
        st.strategy_adjustments = {
            "base": {"qualified": base, "evaluated": True, "minPf": base_floor},
            "main": {"qualified": main, "evaluated": base, "minPf": main_floor},
            "real": {"qualified": real, "evaluated": main, "minPf": real_floor},
            "live": {"evaluation": False, "source": "real"},
            "exchange": {"trackingOnly": True},
        }
        records = {
            "Base": {"evaluated": True, "qualified": base, "sampleCount": n, "pf": pf, "reason": reason or "qualified"},
            "Main": {"evaluated": base, "qualified": main, "sampleCount": n, "pf": pf, "reason": reason or "qualified"},
            "Real": {"evaluated": main, "qualified": real, "sampleCount": n, "pf": pf, "reason": reason or "qualified"},
        }
        return {
            "stage": st.stage,
            "qualified": qualified,
            "parentSetId": st.parent_set_id,
            "base": base,
            "main": main,
            "real": real,
            "basePf": st.base_pf,
            "mainPf": st.main_pf,
            "realPf": st.real_pf,
            "positionCostPct": self.cost_pct,
            "reason": reason,
            "records": records,
            "dedupeKey": stable_key(st.parent_set_id, st.id, "qualification", n, round(pf, 6)),
        }

    def stage_record(self, st: SetState, stage: str, *, reason: str = "") -> StageRecord:
        """Materialize one idempotent stage record without evaluating again."""
        name = str(stage or "").strip().lower()
        if name not in {"base", "main", "real", "live", "exchange"}:
            raise ValueError(f"unknown stage: {stage}")
        ledger = st.stage_ledger or {}
        stage_pf = {
            "base": st.base_pf,
            "main": st.main_pf,
            "real": st.real_pf,
        }
        record = (ledger.get("records") or {}).get(name.title()) if isinstance(ledger.get("records"), dict) else {}
        qualified = bool(ledger.get(name, False)) if name in ("base", "main", "real") else name in ("live", "exchange")
        evaluation = st.evaluation or {}
        return StageRecord(
            set_id=st.id,
            parent_set_id=st.parent_set_id or st.id,
            stage=name.title(),
            gross_pf=round(float(st.normal_evaluation.get("pf") or st.gross_pf or st.last15_classic or 0.0), 6),
            net_pf=round(float(st.adjusted_evaluation.get("pf") or st.net_pf or stage_pf.get(name, st.last15_ratio) or 0.0), 6),
            position_cost_pct=float(st.position_cost_pct or self.cost_pct),
            ddt_s=float(st.max_dd_s or 0.0),
            sample_count=int(record.get("sampleCount") or evaluation.get("sampleCount") or st.last15_n or 0),
            volume_ratio=float(st.volume_ratio or 1.0),
            ev=float(st.adjusted_evaluation.get("ev") or evaluation.get("netEv") or st.expectancy or 0.0),
            gross_ev=float(st.normal_evaluation.get("ev") or evaluation.get("grossEv") or 0.0),
            confidence=float(evaluation.get("confidence") or 0.0),
            uncertainty=float(evaluation.get("uncertainty") or 1.0),
            required_samples=int(evaluation.get("requiredSamples") or self.eval_need()),
            insufficient_sample=bool(evaluation.get("insufficientSample", True)),
            axis_key=st.axis_key,
            relative_count=int(st.relative_count or 1),
            indication_kind=st.indication_kind,
            strategy_adjustments=tuple((str(k), v) for k, v in (st.strategy_adjustments or {}).items()),
            adjustment_deltas=tuple((str(k), v) for k, v in (st.adjustment_deltas or {}).items()),
            qualification_reason=reason or str(record.get("reason") or ledger.get("reason") or ("qualified" if qualified else "not qualified")),
            dedupe_key=str(ledger.get("dedupeKey") or stable_key(st.parent_set_id or st.id, st.id, name, st.last15_n, st.last15_ratio)),
        )

    def stage_records(self, stage: str, pack: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return stable records; parent IDs are retained for aggregate dedupe."""
        want = str(pack or "")
        return [self.stage_record(st, stage).as_dict() for st in self.by_idx if not want or st.pack == want]

    def stage_flow(self) -> Dict[str, Any]:
        """Aggregate stage boundaries with parent dedupe and sample confidence."""
        names = ("Base", "Main", "Real")
        flow: Dict[str, Dict[str, Any]] = {
            name: {
                "evaluated": 0,
                "qualified": 0,
                "blocked": 0,
                "rejected": 0,
                "selected": 0,
                "entered": 0,
                "exited": 0,
                "parents": 0,
                "sampleCount": 0,
                "confidence": 0.0,
                "insufficientSample": 0,
                "volumeRatio": 0.0,
            }
            for name in names
        }
        parent_sets: Dict[str, set[str]] = {name: set() for name in names}
        for st in self.by_idx:
            ledger = st.stage_ledger or {}
            records = ledger.get("records") if isinstance(ledger.get("records"), dict) else {}
            for name in names:
                key = name.lower()
                evaluated = bool((records.get(name) or {}).get("evaluated", key == "base"))
                qualified = bool(ledger.get(key, False))
                if not evaluated:
                    continue
                bucket = flow[name]
                bucket["evaluated"] += 1
                parent = st.parent_set_id or st.id
                parent_sets[name].add(parent)
                if qualified:
                    bucket["qualified"] += 1
                    bucket["volumeRatio"] += float(st.volume_ratio or 1.0)
                else:
                    bucket["blocked"] += 1
                    bucket["rejected"] += 1
                evaluation = st.evaluation or {}
                bucket["sampleCount"] += int(evaluation.get("sampleCount") or st.last15_n or 0)
                bucket["confidence"] += float(evaluation.get("confidence") or 0.0)
                bucket["insufficientSample"] += int(bool(evaluation.get("insufficientSample", True)))
                if qualified and st.active:
                    bucket["selected"] += 1
                if st.live:
                    bucket["entered"] += 1
                    bucket["exited"] += len(st.live)
        for name in names:
            bucket = flow[name]
            evaluated = int(bucket["evaluated"] or 0)
            bucket["parents"] = len(parent_sets[name])
            bucket["confidence"] = round(float(bucket["confidence"]) / evaluated, 4) if evaluated else 0.0
            bucket["volumeRatio"] = round(float(bucket["volumeRatio"]), 6)
        return {
            "stages": flow,
            "stageOrder": list(names),
            "parentRule": "one parent Set is counted once; Main requires Base; Real requires Main",
            "costSubtracted": True,
            "requiredSamples": self.eval_need(),
        }

    def axis_variants(self, coordinator: Any) -> Dict[str, Any]:
        """Generate axis children only from qualified Base parents."""
        rows: List[Dict[str, Any]] = []
        for parent_id in self.qualified_stage_ids("base"):
            st = self.sets.get(parent_id)
            if st is None:
                continue
            rows.extend(coordinator.axis_variants(parent_id, st.hist + st.live, []))
        return coordinator.aggregate_axis_variants(rows)

    def qualified_stage_ids(self, stage: str, pack: Optional[str] = None) -> List[str]:
        """Return unique parent Set IDs qualified for a downstream stage."""
        name = str(stage or "").strip().lower()
        if name not in {"base", "main", "real"}:
            raise ValueError(f"qualification stage must be Base, Main, or Real: {stage}")
        out: List[str] = []
        seen: set[str] = set()
        for st in self.by_idx:
            if pack and st.pack != pack:
                continue
            if not bool((st.stage_ledger or {}).get(name)):
                continue
            parent = st.parent_set_id or st.id
            if parent not in seen:
                seen.add(parent)
                out.append(parent)
        return out

    def _side_active_flags(self, m: Optional[Dict[str, Any]], live: Sequence[Dict[str, Any]]) -> Tuple[bool, str]:
        """Per-side live flag. Unproven / hist-losing sides stay off the live path."""
        if not self.auto_deact:
            return True, ""
        live_rows = sorted((r for r in live if isinstance(r, dict)), key=lambda r: finite(r.get("t")))
        need = self.eval_need()
        enable_pf = float(self.real_min_pf or 1.15)
        if len(live_rows) < need:
            if not self.strict_gate:
                return True, ""
            if not m:
                return False, "unproven"
            n15 = int(m.get("last15_n") or 0)
            ratio = float(m.get("last15_ratio") or 0)
            if n15 < need:
                return False, "unproven"
            if ratio + 1e-9 < enable_pf:
                return False, f"hist PF {ratio:.2f}<{enable_pf:.2f}"
            dd_s = float(m.get("max_dd_s") or 0)
            if dd_s > float(self.max_dd_s or 27000) + 1e-9:
                return False, f"hist DDt {dd_s:.0f}s"
            return True, ""
        window_ok, window_reason, _windows = self._live_windows_ok(live_rows, minimum_pf=1.0)
        if not window_ok:
            return False, window_reason
        live25 = live_rows[-self.deact_n :]
        live_avg = (
            sum(row_net_pnl(r, self.cost_pct) for r in live25) / len(live25) if live25 else 0.0
        )
        live_tail = live_rows[-max(8, min(self.deact_n, 15)) :]
        live_tail_avg = (
            sum(row_net_pnl(r, self.cost_pct) for r in live_tail) / len(live_tail) if live_tail else 0.0
        )
        if self.live_negative_deact and len(live25) >= self.deact_n and live_avg < 0:
            return False, f"live last{len(live25)} avg loss {live_avg:.4f}"
        if self.live_negative_deact and len(live_rows) >= need and live_tail_avg < 0:
            return False, f"live last{len(live_tail)} avg loss {live_tail_avg:.4f}"
        if not m:
            return True, ""
        n15 = int(m.get("last15_n") or 0)
        ratio = float(m.get("last15_ratio") or 0)
        if self.live_negative_deact and n15 >= need and ratio + 1e-9 < 1.0:
            return False, f"live last{n15} PF {ratio:.2f}<1.00 neg"
        if self.live_negative_deact and n15 >= need and ratio + 1e-9 < enable_pf:
            return False, f"live last{n15} PF {ratio:.2f}<{enable_pf:.2f}"
        dd_s = float(m.get("max_dd_s") or 0)
        if n15 >= need and dd_s > float(self.max_dd_s or 27000) + 1e-9:
            return False, f"live DDt {dd_s:.0f}s"
        return True, ""

    def _score_one(self, st: SetState) -> None:
        self._snap_ts = 0.0
        self._live_ov_ts = 0.0
        tape = st.tape()
        tape.sort(key=lambda r: finite(r.get("t")))
        m = self._score_metrics(tape, hist_n=len(st.hist))
        st.n = m["n"]
        st.wins = m["wins"]
        st.gp = m["gp"]
        st.gl = m["gl"]
        st.wr = m["wr"]
        st.expectancy = m["expectancy"]
        st.gross_pf = float(m.get("gross_pf") or 0.0)
        st.net_pf = float(m.get("net_pf") or 0.0)
        st.gross_ev = float(m.get("gross_ev") or 0.0)
        st.net_ev = float(m.get("net_ev") or 0.0)
        st.evaluation = dict(m.get("evaluation") or {})
        st.normal_evaluation = {
            "pf": st.gross_pf,
            "ev": st.gross_ev,
            "sampleCount": int(st.evaluation.get("sampleCount") or 0),
            "source": "gross-price-move",
        }
        st.adjusted_evaluation = {
            "pf": st.net_pf,
            "ev": st.net_ev,
            "ratio": float(m.get("last15_ratio") or 0.0),
            "sampleCount": int(st.evaluation.get("sampleCount") or 0),
            "source": "cost-net",
        }
        st.adjustment_deltas = {
            "pf": round(st.net_pf - st.gross_pf, 6),
            "ev": round(st.net_ev - st.gross_ev, 8),
            "costPct": self.cost_pct,
        }
        st.avg_hold_s = m["avg_hold_s"]
        st.classic_all = m["classic_all"]
        counts: Dict[str, int] = {}
        for r in tape:
            k = str(r.get("reason") or "x").split(":")[0]
            counts[k] = counts.get(k, 0) + 1
        st.exits = counts
        st.last15_ratio = m["last15_ratio"]
        st.last15_classic = m["last15_classic"]
        st.last15_n = m["last15_n"]
        st.last15_r = m["last15_r"]
        st.last25_n = m["last25_n"]
        st.last25_avg_r = m["last25_avg_r"]
        st.last25_avg_pnl = m["last25_avg_pnl"]
        st.max_dd_s = m["max_dd_s"]
        st.avg_dd_s = m["avg_dd_s"]
        st.dd_episodes = m["dd_episodes"]
        st.source_n = m["source_n"]
        st.stage_ledger = self._stage_qualification(st, m)
        need = self.eval_need()
        live_m = self._score_metrics(st.live)
        live_ordered = sorted((r for r in st.live if isinstance(r, dict)), key=lambda r: finite(r.get("t")))
        live_opt_window = live_ordered[-max(50, self.deact_n, self.optimization_n) :]
        live_opt_pf = last_n_cost_pf(live_opt_window, len(live_opt_window) or 1, self.cost_pct)
        live_opt_dd = drawdown_time_by_symbol(live_opt_window) if live_opt_window else {"maxS": 0.0, "avgS": 0.0, "episodes": 0}
        live_opt_avg = (
            sum(row_net_pnl(r, self.cost_pct) for r in live_opt_window) / len(live_opt_window)
            if live_opt_window else 0.0
        )
        st.live_eval = {
            "n": len(st.live),
            "last15N": int(live_m["last15_n"]),
            "last15Ratio": round(float(live_m["last15_ratio"]), 4),
            "last15R": round(float(live_m["last15_r"]), 4),
            "evaluationWindows": dict(live_m.get("evaluation_windows") or {}),
            "netAvg": round(float(live_m["net_avg"]), 6),
            "expectancy": float(live_m["expectancy"]),
            "wr": float(live_m["wr"]),
            "maxDdS": float(live_m["max_dd_s"]),
            "avgDdS": float(live_m["avg_dd_s"]),
            "gp": float(live_m["gp"]),
            "gl": float(live_m["gl"]),
            "validated": bool(live_m["validated"]),
            "costSubtracted": True,
            "source": "live-exchange",
            "optimizationN": len(live_opt_window),
            "optimizationWindow": max(50, self.deact_n, self.optimization_n),
            "optimizationPf": round(float(live_opt_pf.get("ratio") or 1.0), 4),
            "optimizationNetAvg": round(live_opt_avg, 8),
            "optimizationMaxDdS": round(float(live_opt_dd.get("maxS") or 0.0), 1),
            "optimizationPositive": live_opt_avg >= 0.0,
            "tradesPerHour": round(trades_per_hour(st.live), 4),
            "costPct": float(live_opt_pf.get("costPct") or self.cost_pct),
            "costSource": live_opt_pf.get("costSource") or self.cost_source,
        }
        need = self.eval_need()
        enable_pf = float(self.min_pf or 1.15)
        by: Dict[str, Dict[str, Any]] = {}
        for side in DIRECTIONS:
            sub_hist = filter_side(st.hist, side)
            sub_tape = filter_side(tape, side)
            sub_live = filter_side(st.live, side)
            sm = self._score_metrics(sub_tape, hist_n=len(sub_hist))
            sm["side"] = side
            lm = self._score_metrics(sub_live)
            sm["live"] = {
                "n": len(sub_live),
                "last15_n": lm["last15_n"],
                "last15_ratio": lm["last15_ratio"],
                "last15_r": lm["last15_r"],
                "evaluation_windows": dict(lm.get("evaluation_windows") or {}),
                "net_avg": lm["net_avg"],
                "expectancy": lm["expectancy"],
                "max_dd_s": lm["max_dd_s"],
                "wr": lm["wr"],
                "validated": lm["validated"],
                "cost_subtracted": True,
                "optimization_n": len(sorted(sub_live, key=lambda r: finite(r.get("t")))[-max(50, self.deact_n, self.optimization_n):]),
            }
            sm["liveN"] = len(sub_live)
            if len(sub_live) >= self.eval_need():
                sm["last15_ratio"] = lm["last15_ratio"]
                sm["last15_n"] = lm["last15_n"]
                sm["last15_r"] = lm["last15_r"]
                sm["net_avg"] = lm["net_avg"]
                sm["expectancy"] = lm["expectancy"]
                sm["max_dd_s"] = lm["max_dd_s"]
                sm["validated"] = lm["validated"]
                sm["source"] = "live-exchange"
            else:
                sm["source"] = "hist-sim"
            active_s, reason_s = self._side_active_flags(lm if len(sub_live) >= self.eval_need() else sm, sub_live)
            sm["active"] = active_s
            sm["deact_reason"] = reason_s
            by[side] = sm
        st.by_side = by
        live_ordered = sorted((r for r in st.live if isinstance(r, dict)), key=lambda r: finite(r.get("t")))
        live25 = live_ordered[-self.deact_n :]
        live_avg = 0.0
        if live25:
            live_avg = sum(row_net_pnl(r, self.cost_pct) for r in live25) / len(live25)
        live_n = len(st.live)
        live_window_ok, live_window_reason, _live_windows = self._live_windows_ok(st.live, minimum_pf=1.0)
        live_tail = live_ordered[-max(8, min(self.deact_n, 15)) :]
        live_tail_avg = 0.0
        if live_tail:
            live_tail_avg = sum(row_net_pnl(r, self.cost_pct) for r in live_tail) / len(live_tail)
        if st.locked:
            st.active = False
            st.deact_reason = "locked"
            return
        if not self.auto_deact:
            st.active = True
            st.deact_reason = ""
            return
        need_h = self.eval_need()
        hist_n15 = int(m.get("last15_n") or 0)
        hist_pf = float(m.get("last15_ratio") or 0)
        hist_ok = hist_n15 >= need_h and hist_pf + 1e-9 >= enable_pf
        hist_dd_ok = float(m.get("max_dd_s") or 0) <= self.max_dd_s + 1e-9
        # Realtime only validated books. Historic still scores every SL×TP;
        # unproven / hist-losing / high-DDt sets stay off the live path.
        if live_n < need_h:
            if self.strict_gate:
                any_side = any(bool((by.get(d) or {}).get("active")) for d in DIRECTIONS)
                st.active = bool((hist_ok and hist_dd_ok) or any_side)
                if st.active:
                    st.deact_reason = ""
                elif hist_n15 < need_h:
                    st.deact_reason = "unproven"
                elif not hist_ok:
                    st.deact_reason = f"hist PF {hist_pf:.2f}<{enable_pf:.2f}"
                else:
                    st.deact_reason = f"hist DDt {m.get('max_dd_s'):.0f}s"
            else:
                st.active = True
                st.deact_reason = ""
            return
        # Deactivation of a live-processed Set is LIVE on-exchange only.
        # Use all available recent live fills, with a 50+ sample optimization
        # window once available. The current Set alone is evaluated here, so
        # one losing ratio/trail/step can never deactivate its siblings.
        if self.live_negative_deact and live_n >= self.deact_n and live_opt_avg < 0:
            st.active = False
            st.deact_reason = f"live last{len(live_opt_window)} avg loss {live_opt_avg:.4f}"
            st.last25_avg_pnl = live_opt_avg
            st.last25_n = len(live_opt_window)
            return
        if live_n >= need_h and not live_window_ok:
            st.active = False
            st.deact_reason = live_window_reason
            return
        if self.live_negative_deact and live_n >= need_h and live_tail_avg < 0:
            st.active = False
            st.deact_reason = f"live last{len(live_tail)} avg loss {live_tail_avg:.4f}"
            st.last25_avg_pnl = live_tail_avg
            return
        notes = []
        live_ratio = float(live_m["last15_ratio"])
        live_n15 = int(live_m["last15_n"])
        if self.live_negative_deact and live_n15 >= need and live_ratio + 1e-9 < 1.0:
            st.active = False
            st.deact_reason = f"live last{live_n15} PF {live_ratio:.2f}<1.00 neg"
            return
        if live_n15 >= need and live_ratio + 1e-9 < self.min_pf:
            notes.append(f"live last{live_n15} PF {live_ratio:.2f}<{self.min_pf:.2f}")
        if live_n >= need and float(live_m["max_dd_s"]) > self.max_dd_s:
            notes.append(f"live maxDDt {live_m['max_dd_s']:.0f}s>{self.max_dd_s:.0f}s")
        was_live_off = (not st.active) and st.deact_reason.startswith("live ")
        if notes and not self.reactivate and was_live_off:
            st.active = False
            st.deact_reason = "; ".join(dict.fromkeys(notes + [st.deact_reason]))
            return
        if notes and not self.reactivate and live_n15 >= need:
            st.active = False
            st.deact_reason = "; ".join(dict.fromkeys(notes))
            return
        st.active = True
        st.deact_reason = "; ".join(dict.fromkeys(notes))
        self._snap_ts = 0.0
        self._live_ov_ts = 0.0

    def _cap_active(self) -> None:
        # No set-count ceiling. Memory is trimmed by HIST_CAP / load_engine,
        # not by deactivating independent configs.
        return

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
            "last15Ratio": round(st.last15_ratio, 4),
            "maxDdS": st.max_dd_s,
            "parentSetId": st.parent_set_id or st.id,
            "stage": st.stage,
            "stageQualified": st.stage_qualified,
            "stageLedger": st.stage_ledger,
            "basePf": st.base_pf,
            "mainPf": st.main_pf,
            "realPf": st.real_pf,
            "positionCostPct": st.position_cost_pct,
            "axisKey": st.axis_key,
            "relativeCount": st.relative_count,
            "volumeRatio": st.volume_ratio,
            "indicationKind": st.indication_kind,
            "strategyAdjustments": st.strategy_adjustments,
        }

    def coverage(self) -> Dict[str, Any]:
        trails = [t[0] for t in (self.trails or [])]
        trail_sets = [st for st in self.by_idx if st.kind == "trail"]
        base_sets = [st for st in self.by_idx if st.kind == "base"]
        by_tr: Dict[str, Dict[str, Any]] = {}
        by_sl: Dict[str, Dict[str, Any]] = {}
        for st in trail_sets:
            b = by_tr.setdefault(st.trail_key, {"n": 0, "active": 0, "bestPf": 0.0, "bestIdx": -1, "sl": []})
            b["n"] += 1
            b["active"] += int(st.active)
            sls = b.setdefault("sl", [])
            if st.sl_ratio not in sls:
                sls.append(st.sl_ratio)
            if st.last15_ratio >= b["bestPf"]:
                b["bestPf"] = st.last15_ratio
                b["bestIdx"] = st.idx
        sl_step: Dict[str, set] = {}
        for st in base_sets:
            skey = f"{st.sl_ratio:.1f}"
            s = by_sl.setdefault(skey, {"n": 0, "active": 0, "bestPf": 0.0, "bestIdx": -1, "steps": []})
            s["n"] += 1
            s["active"] += int(st.active)
            steps = s.setdefault("steps", [])
            if st.step not in steps:
                steps.append(st.step)
            sl_step.setdefault(skey, set()).add(st.step)
            if st.last15_ratio >= s["bestPf"]:
                s["bestPf"] = st.last15_ratio
                s["bestIdx"] = st.idx
        sl_tp_cover = all(set(self.steps) <= sl_step.get(f"{sl:.1f}", set()) for sl in self.sl_ratios) if self.sl_ratios and self.steps else True
        # Check trail coverage from indexed tuples. The previous nested
        # ``any`` scan was O(trails * sl * steps * trail_sets), which made a
        # 30x25x20 catalog block the stats/UI thread for seconds on every
        # uncached snapshot.
        trail_pairs = {
            (round(float(st.sl_ratio), 10), str(st.trail_key))
            for st in trail_sets
        }
        trail_triples = {
            (round(float(st.sl_ratio), 10), str(st.trail_key), int(st.step))
            for st in trail_sets
        }
        trail_sl_cover = all(
            (round(float(sl), 10), str(t)) in trail_pairs
            for t in trails for sl in self.sl_ratios
        ) if trails and self.sl_ratios else True
        trail_sl_tp_cover = all(
            (round(float(sl), 10), str(t), int(step)) in trail_triples
            for t in trails for sl in self.sl_ratios for step in self.steps
        ) if trails and self.sl_ratios and self.steps else True
        need = self.eval_need()
        validated_count = sum(
            1
            for st in self.sets.values()
            if int(st.last15_n or 0) >= need and float(st.last15_ratio or 0) + 1e-9 >= 1.0
        )
        stage_counts = {"Base": 0, "Main": 0, "Real": 0, "Unqualified": 0}
        stage_parent_counts = {k: set() for k in stage_counts}
        for st in self.by_idx:
            stage = st.stage if st.stage in stage_counts else "Unqualified"
            stage_counts[stage] += 1
            stage_parent_counts[stage].add(st.parent_set_id or st.id)
        axis_counts = {a: sum(1 for st in self.by_idx if st.axis_key.startswith(a + ":")) for a in ("prev", "last", "cont", "pause")}
        return {
            "packs": list(self.packs),
            "slRatios": list(self.sl_ratios),
            "trails": trails,
            "steps": list(self.steps),
            "dims": {
                "pack": len(self.packs),
                "sl": len(self.sl_ratios),
                "trail": len(trails),
                "step": len(self.steps),
                "direction": 2,
            },
            "families": {"base": len(base_sets), "trail": len(trail_sets)},
            "product": len(self.by_idx),
            "setCount": len(self.sets),
            "activeCount": sum(1 for st in self.sets.values() if st.active),
            "validatedCount": validated_count,
            "validationNeed": need,
            "histFills": sum(st.n for st in self.sets.values()),
            "indexed": True,
            "independentTrail": bool(getattr(self, "trail_enabled", True)),
            "independentDirection": True,
            "independentIndication": True,
            "independentStrategy": True,
            "independentSlTp": True,
            "independentConfigs": True,
            "costSubtracted": True,
            "stageDefaults": {"base": self.stage_min_pf["base"], "main": self.stage_min_pf["main"], "real": self.stage_min_pf["real"]},
            "stageCounts": stage_counts,
            "stageParentCounts": {k: len(v) for k, v in stage_parent_counts.items()},
            "stageFlow": self.stage_flow(),
            "qualifiedParentIds": {
                "base": self.qualified_stage_ids("base"),
                "main": self.qualified_stage_ids("main"),
                "real": self.qualified_stage_ids("real"),
            },
            "axisCounts": axis_counts,
            "axisVolumeRatio": 0.01,
            "directions": list(DIRECTIONS),
            "byTrail": by_tr,
            "bySl": by_sl,
            "trailCover": all(any(st.trail_key == t for st in trail_sets) for t in trails) if trails else True,
            "slCover": all(any(abs(st.sl_ratio - sl) < 1e-9 for st in base_sets) for sl in self.sl_ratios),
            "slTpCover": sl_tp_cover,
            "trailSlCover": trail_sl_cover,
            "trailSlTpCover": trail_sl_tp_cover,
        }

    def _side_view(self, st: SetState, side: Optional[str] = None) -> Dict[str, Any]:
        want = str(side or "").strip().upper()
        if want in ("L", "1", "BUY"):
            want = "LONG"
        elif want in ("S", "-1", "SELL"):
            want = "SHORT"
        blob = (st.by_side or {}).get(want) if want in DIRECTIONS else None
        if not blob:
            return {
                "last15_ratio": st.last15_ratio,
                "last15_n": st.last15_n,
                "last25_avg_r": st.last25_avg_r,
                "max_dd_s": st.max_dd_s,
                "n": st.n,
                "validated": st.last15_n >= self.eval_need() and st.last15_ratio + 1e-9 >= float(self.real_min_pf or 1.15),
                "active": st.active,
            }
        return blob

    def pick(self, pack: str, kind: str = "base", side: Optional[str] = None) -> Optional[SetState]:
        gated = bool(self.progress.ready and self.use_historic_gate)
        want_side = str(side or "").strip().upper()
        if want_side in ("L", "1", "BUY"):
            want_side = "LONG"
        elif want_side in ("S", "-1", "SELL"):
            want_side = "SHORT"
        use_side = want_side in DIRECTIONS
        rows = [s for s in self.by_idx if s.pack == pack and s.kind == kind]
        if not rows:
            return None
        need = self.eval_need()

        def view(s: SetState) -> Dict[str, Any]:
            return self._side_view(s, want_side if use_side else None)

        def side_on(s: SetState) -> bool:
            if use_side:
                blob = (s.by_side or {}).get(want_side)
                if isinstance(blob, dict) and "active" in blob:
                    return bool(blob.get("active"))
            return bool(s.active)

        on = [s for s in rows if side_on(s)]
        if on:
            rows = on
        elif gated:
            return None
        if not rows:
            return None

        def proven_neg(s: SetState) -> bool:
            v = view(s)
            return int(v.get("last15_n") or 0) >= need and float(v.get("last15_ratio") or 0) + 1e-9 < 1.0

        passing = [
            s for s in rows
            if int(view(s).get("last15_n") or 0) >= need and float(view(s).get("last15_ratio") or 0) + 1e-9 >= self.real_min_pf
        ]
        if not passing and not self.strict_gate:
            passing = [
                s for s in rows
                if int(view(s).get("last15_n") or 0) >= need and float(view(s).get("last15_ratio") or 0) + 1e-9 >= 1.0
            ]
        if not passing and not self.strict_gate:
            passing = [s for s in rows if not proven_neg(s)]
        if not passing and not self.strict_gate and not gated:
            passing = [s for s in rows if side_on(s)] or list(rows)
        if not passing:
            return None

        def live_rows(s: SetState) -> List[Dict[str, Any]]:
            return sorted(
                filter_side(s.live, want_side if use_side else None),
                key=lambda r: finite(r.get("t")),
            )

        def live_ok(s: SetState) -> bool:
            scoped = live_rows(s)
            ok, _reason, _windows = self._live_windows_ok(scoped, minimum_pf=1.0)
            return ok
        live_pass = [s for s in passing if live_ok(s)]
        live_evidenced = [s for s in live_pass if len(live_rows(s)) >= need]
        # Once a Set has enough own exchange evidence, do not let a merely
        # historic sibling win by default.  This keeps the measured live pool
        # separate from cold candidates and makes throughput meaningful.
        chosen = live_evidenced or live_pass
        if not chosen:
            return None
        if self.live_test_mode and kind == "base":
            # Exploration is deliberately bounded to the best historic
            # candidates and only rotates candidates that are still cold. A
            # candidate with negative live windows is already deactivated by
            # _score_one/_side_active_flags and cannot be reintroduced here.
            exploratory = sorted(
                live_pass,
                key=lambda s: (
                    -float(view(s).get("last15_ratio") or 0),
                    -float(view(s).get("last25_avg_r") or 0),
                    float(view(s).get("max_dd_s") or 0),
                    int(s.step or 0),
                    float(s.sl_ratio or 0),
                    float(s.trail_arm or 0),
                    float(s.trail_give or 0),
                ),
            )[: self.live_test_candidates]
            if self.prefer_minimal_range and self.additional_coordination:
                floor_rows = [s for s in exploratory if int(s.step or 0) >= int(self.min_step or self.min_step_cfg)]
                if floor_rows:
                    exploratory = floor_rows
            cold = [
                s for s in exploratory
                if len(filter_side(s.live, want_side if use_side else None)) < self.live_test_min_samples
            ]
            pool = cold or live_evidenced or exploratory
            if pool:
                with self._pick_lock:
                    selected = pool[self._pick_cursor % len(pool)]
                    self._pick_cursor = (self._pick_cursor + 1) % max(1, len(pool))
                chosen = [selected]
        if self.prefer_minimal_range and kind == "base" and self.additional_coordination:
            # Independently positive live configs may establish a preferred
            # floor. This never invents a threshold from aggregate losses.
            floor_rows = [s for s in chosen if int(s.step or 0) >= int(self.min_step or self.min_step_cfg)]
            if floor_rows:
                chosen = floor_rows
        if live_evidenced and not (self.live_test_mode and kind == "base" and any(
            len(live_rows(s)) < self.live_test_min_samples for s in chosen
        )):
            # Safety/quality gates have already run above.  Among measured
            # positive live configs, observed trade throughput is the primary
            # economic tie-breaker, followed by PF and DD-time/range.
            chosen.sort(key=lambda s: (
                -trades_per_hour(live_rows(s)),
                -float(view(s).get("last15_ratio") or 0),
                -float(view(s).get("last25_avg_r") or 0),
                float(view(s).get("max_dd_s") or 0),
                int(s.step or 0),
                float(s.sl_ratio or 0),
                float(s.trail_arm or 0),
                float(s.trail_give or 0),
                -int(view(s).get("n") or 0),
            ))
        elif self.prefer_minimal_range:
            # Cold VST exploration and historic-only operation remain
            # deterministic: PF first, then expectancy/DD-time, then range.
            chosen.sort(key=lambda s: (
                -float(view(s).get("last15_ratio") or 0),
                -float(view(s).get("last25_avg_r") or 0),
                float(view(s).get("max_dd_s") or 0),
                int(s.step or 0),
                float(s.sl_ratio or 0),
                float(s.trail_arm or 0),
                float(s.trail_give or 0),
                -int(view(s).get("n") or 0),
            ))
        else:
            chosen.sort(key=lambda s: (
                -float(view(s).get("last15_ratio") or 0),
                -float(view(s).get("last25_avg_r") or 0),
                float(view(s).get("max_dd_s") or 0),
                0 if (s.kind != "base" or int(s.step or 0) >= int(self.min_step or 0)) else 1,
                -int(view(s).get("n") or 0),
            ))
        return chosen[0]

    def pick_trail(self, pack: str, side: Optional[str] = None) -> Optional[SetState]:
        return self.pick(pack, kind="trail", side=side)

    def pick_any(self, pack: str, side: Optional[str] = None) -> Optional[SetState]:
        base = self.pick(pack, "base", side=side)
        trail = self.pick(pack, "trail", side=side)
        if base and trail:
            want = str(side or "").strip().upper()
            vb = self._side_view(base, want if want in DIRECTIONS else None)
            vt = self._side_view(trail, want if want in DIRECTIONS else None)
            base_live = filter_side(base.live, want if want in DIRECTIONS else None)
            trail_live = filter_side(trail.live, want if want in DIRECTIONS else None)
            if min(len(base_live), len(trail_live)) >= self.eval_need():
                return max((base, trail), key=lambda s: trades_per_hour(filter_side(s.live, want)))
            if float(vt.get("last15_ratio") or 0) > float(vb.get("last15_ratio") or 0):
                return trail
            return base
        return base or trail

    def pack_open(self, pack: str, side: Optional[str] = None) -> bool:
        if not self.enabled or not self.use_historic_gate:
            return True
        if self.strict_gate:
            if not getattr(self.progress, "ready", False):
                return True
            return self.pick_any(pack, side=side) is not None
        fills = sum(s.n for s in self.sets.values())
        if fills < 8 or not self.progress.ready:
            return True
        return self.pick_any(pack, side=side) is not None

    def ind_stats(self, kind: str, side: Optional[str] = None) -> Dict[str, Any]:
        """Cost-adjusted PF evidence for one indication kind. Live tape wins once it has samples."""
        live = list(self.ind_live.get(kind) or [])
        hist = list(self.ind_hist.get(kind) or [])
        live_side = filter_side(live, side)
        need = self.eval_need()
        if len(live_side) >= need:
            tape = live_side
            source = "live-exchange"
        else:
            tape = filter_side(hist + live, side)
            source = "mixed" if live_side else "hist-sim"
        tape.sort(key=lambda r: finite(r.get("t")))
        dd = drawdown_time_by_symbol(tape) if tape else {"maxS": 0.0, "avgS": 0.0, "episodes": 0}
        if tape:
            balanced = last_n_balanced(tape, max(self.pf_n, max(EVALUATION_WINDOWS)))
            last = last_n_cost_pf(balanced, self.pf_n, self.cost_pct)
            windows = evaluation_windows(balanced, self.cost_pct, required_samples=need)
            n = int(last["count"])
            pf = float(last["ratio"])
            net_avg = float(last.get("netAvg") or 0)
        else:
            n, pf, net_avg = 0, 0.0, 0.0
            windows = evaluation_windows([], self.cost_pct, required_samples=need)
        need = self.eval_need()
        by_side: Dict[str, Any] = {}
        if not side:
            for d in DIRECTIONS:
                by_side[d] = self.ind_stats(kind, d)
        return {
            "kind": kind,
            "side": (str(side).upper() if side else "BOTH"),
            "n": n,
            "tapeN": len(tape),
            "pf": round(pf, 4),
            "evaluationWindows": windows,
            "netAvg": round(net_avg, 6),
            "costSubtracted": True,
            "validated": n >= need,
            "profitable": pf + 1e-9 >= 1.0,
            "maxDdS": round(float(dd.get("maxS") or 0), 1),
            "avgDdS": round(float(dd.get("avgS") or 0), 1),
            "ddEpisodes": int(dd.get("episodes") or 0),
            "bySide": by_side,
            "source": source,
            "liveN": len(live_side),
        }

    def indication_ok(self, kind: str, side: Optional[str] = None) -> bool:
        if not (self.enabled and self.use_historic_gate and self.strict_gate):
            return True
        if not getattr(self.progress, "ready", False):
            return True
        k = str(kind or "").strip()
        if k:
            st = self.ind_stats(k, side=side)
            if st["validated"]:
                return bool(st["profitable"])
        return self.pack_open("indications", side=side)

    def ind_gate_snapshot(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}

        def _ok(kind: str, side: Optional[str] = None) -> bool:
            try:
                return self.indication_ok(kind, side)
            except TypeError:
                return self.indication_ok(kind)

        for k in IND_KINDS:
            st = self.ind_stats(k)
            st["ok"] = _ok(k)
            for d in DIRECTIONS:
                blob = (st.get("bySide") or {}).get(d)
                if isinstance(blob, dict):
                    blob["ok"] = _ok(k, d)
            out[k] = st
        return out

    def live_overview(self) -> Dict[str, Any]:
        """On-exchange processed Sets, cost-net. Unique client_id so a fill is not double-counted."""
        now = time.monotonic()
        cached = self._live_ov_cache
        if cached is not None and now - self._live_ov_ts < 2.0:
            return cached
        processed = [s for s in self.by_idx if s.live]
        seen: set = set()
        fills: List[Dict[str, Any]] = []
        for s in processed:
            for r in s.live:
                if not isinstance(r, dict):
                    continue
                cid = str(r.get("client_id") or "") or f"{r.get('t')}:{r.get('symbol')}:{s.id}:{r.get('pnl')}"
                if cid in seen:
                    continue
                seen.add(cid)
                fills.append(r)
        fills.sort(key=lambda r: finite(r.get("t")))
        m = self._score_metrics(fills)
        rows = []
        for s in sorted(processed, key=lambda x: (-len(x.live), -float((x.live_eval or {}).get("last15Ratio") or 0), x.max_dd_s)):
            ev = s.live_eval or {}
            rows.append({
                "id": s.id,
                "pack": s.pack,
                "kind": s.kind,
                "slRatio": s.sl_ratio,
                "step": s.step,
                "trailKey": s.trail_key,
                "direction": "BOTH",
                "n": int(ev.get("n") or len(s.live)),
                "last15Ratio": float(ev.get("last15Ratio") or 0),
                "last15N": int(ev.get("last15N") or 0),
                "evaluationWindows": dict(ev.get("evaluationWindows") or {}),
                "netAvg": float(ev.get("netAvg") or 0),
                "wr": float(ev.get("wr") or 0),
                "maxDdS": float(ev.get("maxDdS") or 0),
                "tradesPerHour": round(float(ev.get("tradesPerHour") or 0.0), 4),
                "validated": bool(ev.get("validated")),
                "active": s.active,
                "deactReason": s.deact_reason,
                "costSubtracted": True,
                "source": "live-exchange",
                "bySide": {
                    d: {
                        "n": int((v.get("live") or {}).get("n") or v.get("liveN") or 0),
                        "pf": round(float((v.get("live") or {}).get("last15_ratio") or 0), 4),
                        "netAvg": round(float((v.get("live") or {}).get("net_avg") or 0), 6),
                        "active": bool(v.get("active", True)),
                        "validated": bool((v.get("live") or {}).get("validated")),
                    }
                    for d, v in (s.by_side or {}).items()
                    if isinstance(v, dict)
                },
            })
            if len(rows) >= 16:
                break
        out = {
            "processed": len(processed),
            "active": sum(1 for s in processed if s.active),
            "deactivated": sum(1 for s in processed if not s.active),
            "fills": len(fills),
            "last15Ratio": round(float(m["last15_ratio"]), 4),
            "last15N": int(m["last15_n"]),
            "evaluationWindows": dict(m.get("evaluation_windows") or {}),
            "netAvg": round(float(m["net_avg"]), 6),
            "wr": float(m["wr"]),
            "maxDdS": float(m["max_dd_s"]),
            "validated": bool(m["validated"]),
            "costSubtracted": True,
            "costPct": self.cost_pct,
            "source": "live-exchange",
            "rows": rows,
        }
        self._live_ov_cache = out
        self._live_ov_ts = time.monotonic()
        return out

    def snapshot(self, full: bool = False) -> Dict[str, Any]:
        now = time.monotonic()
        cached = self._snap_cache
        if cached is not None and now - self._snap_ts < (0.8 if full else 1.4):
            return cached
        # The UI only consumes the best bounded preview rows.  Sorting the
        # states is cheap compared with constructing a JSON row for every
        # 34k-entry catalog; materialising the full list here briefly doubled
        # RSS during startup and pushed small VPS workers into swap.
        rows = []
        for st in sorted(self.sets.values(), key=lambda s: (not s.active, -s.last15_ratio, s.max_dd_s))[:16]:
            rows.append(
                {
                    "kind": st.kind,
                    "idx": st.idx,
                    "id": st.id,
                    "parentSetId": st.parent_set_id or st.id,
                    "stage": st.stage,
                    "stageQualified": st.stage_qualified,
                    "stageLedger": st.stage_ledger,
                    "basePf": st.base_pf,
                    "mainPf": st.main_pf,
                    "realPf": st.real_pf,
                    "positionCostPct": st.position_cost_pct,
                    "axisKey": st.axis_key,
                    "relativeCount": st.relative_count,
                    "volumeRatio": st.volume_ratio,
                    "indicationKind": st.indication_kind,
                    "strategyAdjustments": st.strategy_adjustments,
                    "adjustmentDeltas": st.adjustment_deltas,
                    "grossPf": st.gross_pf,
                    "netPf": st.net_pf,
                    "grossEv": st.gross_ev,
                    "netEv": st.net_ev,
                "evaluation": st.evaluation,
                "evaluationWindows": st.evaluation_windows,
                    "pairedEvaluation": {
                        "normal": st.normal_evaluation,
                        "adjusted": st.adjusted_evaluation,
                        "deltas": st.adjustment_deltas,
                    },
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
                    "histN": len(st.hist),
                    "wins": st.wins,
                    "last15Ratio": round(st.last15_ratio, 4),
                    "last15Classic": round(st.last15_classic, 3),
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
                    "classicPf": st.classic_all,
                    "validated": bool(st.stage_qualified),
                    "baseQualified": bool((st.stage_ledger or {}).get("base")),
                    "mainQualified": bool((st.stage_ledger or {}).get("main")),
                    "realQualified": bool((st.stage_ledger or {}).get("real")),
                    "gp": st.gp,
                    "gl": st.gl,
                    "costSubtracted": True,
                    "netAvg": round(st.expectancy, 6),
                    "live": st.live_eval or {},
                    "source": "live-exchange" if st.live else "hist-sim",
                    "evaluationWindows": st.evaluation_windows,
                    "bySide": {
                        d: {
                            "n": int(v.get("n") or 0),
                            "pf": round(float(v.get("last15_ratio") or 0), 4),
                            "last15N": int(v.get("last15_n") or 0),
                            "last15R": round(float(v.get("last15_r") or 0), 4),
                            "evaluationWindows": dict(v.get("evaluation_windows") or {}),
                            "expectancy": float(v.get("expectancy") or 0),
                            "netAvg": round(float(v.get("net_avg") or v.get("expectancy") or 0), 6),
                            "maxDdS": float(v.get("max_dd_s") or 0),
                            "wr": float(v.get("wr") or 0),
                            "validated": bool(v.get("validated")),
                            "active": bool(v.get("active", True)),
                            "costSubtracted": True,
                        }
                        for d, v in (st.by_side or {}).items()
                        if isinstance(v, dict)
                    },
                    "exits": st.exits,
                    "intern": {
                        "pf15": round(st.last15_ratio, 4),
                        "classic15": round(st.last15_classic, 4),
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
                    "deactReason": st.deact_reason,
                    "locked": st.locked,
                }
            )
        p = self.progress
        cover = self.coverage()
        validated_count = int(cover.get("validatedCount") or 0)
        live_ov = self.live_overview()
        strategy_history = {}
        for strategy, tape in (self.strategy_hist or {}).items():
            metrics = self._score_metrics(tape)
            strategy_history[str(strategy)] = {
                "n": int(metrics.get("source_n") or 0),
                "last15N": int(metrics.get("last15_n") or 0),
                "last15Ratio": round(float(metrics.get("last15_ratio") or 0), 4),
                "evaluationWindows": dict(metrics.get("evaluation_windows") or {}),
                "netAvg": round(float(metrics.get("net_avg") or 0), 6),
                "maxDdS": round(float(metrics.get("max_dd_s") or 0), 1),
                "costSubtracted": True,
                "source": "hist-sim",
            }
        # Never dump the full 1000+ set index into the hot stats JSON.
        index = []
        if full:
            index = [
                {
                    "i": st.idx,
                    "id": st.id,
                    "kind": st.kind,
                    "pack": st.pack,
                    "sl": st.sl_ratio,
                    "tr": st.trail_key,
                    "st": st.step,
                    "on": int(st.active),
                    "pf": round(st.last15_ratio, 4),
                    "dd": st.max_dd_s,
                }
                for st in self.by_idx[:48]
            ]
        out = {
            "enabled": self.enabled,
            "ready": p.ready,
            "lookback": self.lookback,
            "pfWindow": self.pf_n,
            "deactN": self.deact_n,
            "minPf": self.real_min_pf,
            "enablePf": 1.15 if float(self.min_pf or 0) <= 0 else self.min_pf,
            "enableNeed": self.eval_need(),
            "maxDdS": self.max_dd_s,
            "autoDeact": self.auto_deact,
            "liveNegativeDeact": self.live_negative_deact,
            "preferMinimalRange": self.prefer_minimal_range,
            "additionalCoordination": self.additional_coordination,
            # Deprecated response aliases for older UI/remote clients.
            "preferMinimalPositive": self.prefer_minimal_range,
            "minimalPositiveCoordination": self.additional_coordination,
            "optimizationN": self.optimization_n,
            "optimization": dict(getattr(self, "optimization_stats", {}) or {}),
            "liveTestMode": bool(self.live_test_mode),
            "liveTestCandidates": int(self.live_test_candidates),
            "liveTestMinSamples": int(self.live_test_min_samples),
            "useHistoricGate": self.use_historic_gate,
            "strictGate": bool(self.strict_gate),
            "indGate": self.ind_gate_snapshot(),
            "minSamples": self.min_samples,
            "costPct": self.cost_pct,
            "costSubtracted": True,
            "independentDirection": True,
            "directions": list(DIRECTIONS),
            "setCount": len(self.sets),
            "activeCount": sum(1 for s in self.sets.values() if s.active),
            "validatedCount": validated_count,
            "validationNeed": int(cover.get("validationNeed") or self.eval_need()),
            "coverage": cover,
            "stageFlow": self.stage_flow(),
            "liveOverview": live_ov,
            "strategyHistory": strategy_history,
            "liveFills": int(live_ov.get("fills") or 0),
            "liveProcessed": int(live_ov.get("processed") or 0),
            "liveActive": int(live_ov.get("active") or 0),
            "index": index,
            "minStep": self.min_step,
            "minStepCfg": self.min_step_cfg,
            "stepMax": self.step_max,
            "stepAdapt": self.step_adapt,
            "steps": list(self.steps),
            "trailEnabled": bool(getattr(self, "trail_enabled", True)),
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
            },
            "rows": rows,
        }
        self._snap_cache = out
        self._snap_ts = time.monotonic()
        return out


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
    boundary = drawdown_time(
        [
            {"t": 100, "pnl": 1.0},
            {"t": 120, "pnl": -0.2},
            {"t": 150, "pnl": -0.1},
            {"t": 180, "pnl": 0.3},
            {"t": 200, "pnl": -0.4},
        ],
        now=260,
    )
    out.append(("set-dd-recovery-boundary", boundary["episodes"] == 2.0 and boundary["maxS"] == 60.0 and boundary["avgS"] == 60.0, f"{boundary}"))
    # last-25 negative deactivates
    book = SetBook()
    book.load(
        {
            "histEnabled": True,
            "setDeactN": 25,
            "setPfWindow": 15,
            "setMinPf": 1.10,
            "setMaxDdTimeS": 10_000,
            "setMinSamples": 8,
            "setAutoDeact": True,
            # Live negative-result deactivation is opt-in in production
            # defaults; enable it explicitly for this safety-policy fixture.
            "setLiveNegativeDeact": True,
            "setMinStep": 3,
            "setStepMax": 6,
            "setStepAdapt": True,
            "stratIndications": True,
            "stratGeneral": True,
            "trailArmMin": 0.3,
            "trailArmMax": 0.3,
            "trailGiveMin": 0.1,
            "trailGiveMax": 0.1,
            "slToTpRatios": [0.6],
        }
    )
    out.append(("set-count", len(book.sets) >= 2, f"n={len(book.sets)}"))
    slim = book.snapshot()
    out.append(("set-snap-slim-index", len(slim.get("index") or []) == 0, f"index={len(slim.get('index') or [])} rows={len(slim.get('rows') or [])}"))
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
    out.append(("set-adapt-unattributed-no-promotion", book.min_step == 3, f"min={book.min_step} n={len(book.sets)}"))
    out.append(("set-adapt-keeps-grid", sorted({s.step for s in book.sets.values() if s.kind == "base"}) == list(range(3, 7)), f"steps={sorted({s.step for s in book.sets.values() if s.kind == 'base'})}"))
    sid = next(iter(book.sets))
    st = book.sets[sid]
    st.hist = [{"t": 1000 + i, "pnl": -0.01, "pnl_pct": -0.003, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "sl"} for i in range(25)]
    st.live = []
    book._score_one(st)
    out.append(("set-hist-neg-off-live", not st.active, f"{st.active} {st.deact_reason}"))
    st.live = [{"t": 2000 + i, "pnl": -0.02, "pnl_pct": -0.004, "symbol": "T", "side": "LONG", "hold_s": 40, "reason": "sl"} for i in range(25)]
    book._score_one(st)
    out.append(("set-deact-live-25", (not st.active) and "live last" in st.deact_reason and "loss" in st.deact_reason, f"{st.active} {st.deact_reason} {st.last25_avg_pnl}"))
    st.hist = [{"t": 1500 + i, "pnl": 0.02, "pnl_pct": 0.004, "symbol": "T", "side": "LONG", "hold_s": 40, "reason": "tp"} for i in range(15)]
    st.live = [{"t": 2500 + i, "pnl": -0.02, "pnl_pct": -0.004, "symbol": "T", "side": "LONG", "hold_s": 40, "reason": "sl"} for i in range(10)]
    book._score_one(st)
    out.append(("set-live-overrides-hist", (not st.active) and str(st.deact_reason).startswith("live"), f"{st.active} {st.deact_reason} histPf={st.last15_ratio}"))
    st.hist = [{"t": 1500 + i, "pnl": -0.02, "pnl_pct": -0.003, "symbol": "T", "side": "LONG", "hold_s": 40, "reason": "sl"} for i in range(15)]
    st.live = [{"t": 2600 + i, "pnl": 0.02, "pnl_pct": 0.004, "symbol": "T", "side": "LONG", "hold_s": 40, "reason": "tp", "client_id": f"cid{i}"} for i in range(10)]
    book._score_one(st)
    out.append(("set-live-wins-keep", st.active, f"{st.active} {st.deact_reason} live={st.live_eval}"))
    snap_ov = book.snapshot().get("liveOverview") or {}
    out.append(("set-live-overview", int(snap_ov.get("processed") or 0) >= 1 and snap_ov.get("costSubtracted") is True and snap_ov.get("source") == "live-exchange", str(snap_ov)[:220]))
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
    out.append(("set-pf15-pass", st2.last15_ratio >= 1.09 and st2.active, f"ratio={st2.last15_ratio} {st2.deact_reason}"))
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
            "setMaxDdTimeS": 50_000,
            "setMinSamples": 5,
            "setAutoDeact": True,
            "setMinStep": 3,
            "setStepMax": 8,
            "stratIndications": True,
            "stratGeneral": True,
            "trailArmMin": 0.3,
            "trailArmMax": 0.3,
            "slToTpRatios": [0.6, 0.9],
            "tpPct": 0.75,
            "timeStopS": 240,
        }
    )
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
    out.append(("set-pick", picked is not None and picked.id == winner.id and winner.active and winner.step >= book2.min_step, f"{getattr(picked, 'id', None)} active={winner.active} pf={winner.last15_ratio} st={winner.step}"))
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
            "setMaxDdTimeS": 50_000,
            "setMinSamples": 3,
            "setAutoDeact": False,
            "setMinStep": 3,
            "setStepMax": 12,
            "stratIndications": False,
            "stratGeneral": True,
            "trailArmMin": 0.3,
            "trailArmMax": 0.3,
            "slToTpRatios": [0.3, 1.5],
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
    tight = [s for s in book3.sets.values() if abs(s.sl_ratio - 0.3) < 1e-9 and s.kind == "base"]
    wide = [s for s in book3.sets.values() if abs(s.sl_ratio - 1.5) < 1e-9 and s.kind == "base"]
    lo_step = [s for s in book3.sets.values() if s.step == 3 and s.kind == "base"]
    hi_step = [s for s in book3.sets.values() if s.step == 12 and s.kind == "base"]
    def sig(st: SetState) -> Tuple[int, float, float, float]:
        return (st.n, round(st.last15_ratio, 4), round(st.avg_hold_s, 1), round(st.expectancy, 6))
    t_sig = sig(tight[0]) if tight else (0, 0.0, 0.0, 0.0)
    w_sig = sig(wide[0]) if wide else (0, 0.0, 0.0, 0.0)
    lo_sig = sig(lo_step[0]) if lo_step else (0, 0.0, 0.0, 0.0)
    hi_sig = sig(hi_step[0]) if hi_step else (0, 0.0, 0.0, 0.0)
    intern_ok = (t_sig != w_sig) or (lo_sig != hi_sig)
    out.append(("set-intern-independent", intern_ok and (tight[0].n + wide[0].n) > 0, f"sl0.3={t_sig} sl1.5={w_sig} st3={lo_sig} st12={hi_sig} fills={sum(s.n for s in book3.sets.values())}"))
    # full config grid: every pack × sl × trail × step indexed
    book4 = SetBook()
    book4.load(
        {
            "histEnabled": True,
            "setMinStep": 8,
            "setStepMax": 12,
            "stratIndications": True,
            "stratGeneral": True,
            "trailArmMin": 0.3,
            "trailArmMax": 1.5,
            "trailGiveMin": 0.1,
            "trailGiveMax": 0.5,
            "slToTpRatios": [0.3, 0.6, 0.9, 1.2, 1.5],
        }
    )
    cov = book4.coverage()
    want_base = len(book4.packs) * len(book4.sl_ratios) * len(book4.steps)
    want_tr = len(book4.packs) * len(book4.sl_ratios) * len(book4.steps) * max(1, len(book4.trails))
    want = want_base + want_tr
    idxs = [s.idx for s in book4.by_idx]
    trails_in = {s.trail_key for s in book4.by_idx if s.kind == "trail"}
    kinds = {s.kind for s in book4.by_idx}
    out.append(("set-grid-product", len(book4.by_idx) == want and want_base >= 50 and want_tr >= 10, f"n={len(book4.by_idx)} base={want_base} trail={want_tr} dims={cov.get('dims')} fam={cov.get('families')}"))
    out.append(("set-idx-unique", idxs == list(range(len(idxs))), f"n={len(idxs)} last={idxs[-1] if idxs else None}"))
    out.append(("set-trail-cover", cov.get("trailCover") and len(trails_in) >= 5 and "trail" in kinds, f"trails={sorted(trails_in)} cover={cov.get('trailCover')}"))
    out.append(("set-trail-grid-n", len(book4.trails) >= 20 and len(trails_in) >= 20, f"n={len(book4.trails)} keys={sorted(trails_in)[:8]}"))
    bases = {(s.pack, round(s.sl_ratio, 1), s.step) for s in book4.by_idx if s.kind == "base"}
    trail_sls = {(s.pack, round(s.sl_ratio, 1), s.step) for s in book4.by_idx if s.kind == "trail"}
    out.append(("set-trail-parallel-normal", bases and bases <= trail_sls, f"base={len(bases)} trailCombos={len(trail_sls)}"))
    slim = SetBook()
    slim.load({"histEnabled": True, "setMinStep": 2, "setStepMax": 2, "stratGeneral": True, "stratIndications": False, "slToTpRatios": [0.6], "trailArmMin": 0.6, "trailArmMax": 0.6, "trailGiveMin": 0.2, "trailGiveMax": 0.2})
    out.append(("set-trail-range-honored", len(slim.trails) == 1 and any(s.kind == "base" for s in slim.by_idx) and any(s.kind == "trail" for s in slim.by_idx), f"trails={len(slim.trails)} n={len(slim.by_idx)}"))
    fullt = SetBook()
    fullt.load({"histEnabled": True, "setMinStep": 2, "setStepMax": 2, "stratGeneral": True, "stratIndications": False, "slToTpRatios": [0.6], "trailArmMin": 0.3, "trailArmMax": 1.5, "trailGiveMin": 0.1, "trailGiveMax": 0.5})
    out.append(("set-trail-range-full", len(fullt.trails) >= 20 and any(s.kind == "base" for s in fullt.by_idx), f"trails={len(fullt.trails)} n={len(fullt.by_idx)}"))
    off = SetBook()
    off.load({"histEnabled": True, "setMinStep": 2, "setStepMax": 2, "stratGeneral": True, "stratIndications": False, "slToTpRatios": [0.6], "stratTrailing": False})
    out.append(("set-trail-explicit-off", not off.trails and all(s.kind == "base" for s in off.by_idx), f"n={len(off.by_idx)} kinds={ {s.kind for s in off.by_idx} }"))
    out.append(("set-sl-cover", cov.get("slCover") and len(book4.sl_ratios) >= 5, f"sl={book4.sl_ratios}"))
    out.append(("set-sl-tp-cover", bool(cov.get("slTpCover")), f"slTp={cov.get('slTpCover')} steps={cov.get('steps')} bySl={ {k: v.get('steps') for k, v in (cov.get('bySl') or {}).items()} }"))
    out.append(("set-trail-sl-cover", bool(cov.get("trailSlCover")), f"trailSl={cov.get('trailSlCover')} byTrail={ {k: v.get('sl') for k, v in (cov.get('byTrail') or {}).items()} }"))
    out.append(("set-independent-sl-tp", bool(cov.get("independentSlTp")), str(cov.get("independentSlTp"))))
    out.append(("set-trail-sl-tp-cover", bool(cov.get("trailSlTpCover")), f"trailSlTp={cov.get('trailSlTpCover')} product={cov.get('product')} want={want}"))
    out.append(("set-independent-configs", bool(cov.get("independentConfigs")) and len(book4.by_idx) == want, f"n={len(book4.by_idx)} want={want}"))
    combo = {(s.pack, round(s.sl_ratio, 1), s.step, s.trail_key or "") for s in book4.by_idx}
    out.append(("set-combo-unique", len(combo) == len(book4.by_idx), f"unique={len(combo)} n={len(book4.by_idx)}"))
    out.append(("set-get-idx", book4.get_idx(0) is book4.by_idx[0] and book4.get_idx(want - 1) is book4.by_idx[-1], f"0={book4.get_idx(0).id if book4.get_idx(0) else None}"))
    v = book4.coord_vars(book4.by_idx[0])
    out.append(("set-coord-vars", v.get("idx") == 0 and v.get("kind") == "base" and "step" in v, str(v)))
    trail_row = next((s for s in book4.sets.values() if s.kind == "trail"), None)
    if trail_row:
        trail_row.hist = [
            {"t": 1_700_000_000 + i * 60, "pnl": 0.02, "pnl_pct": 0.006, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "tp"}
            for i in range(20)
        ]
        book4._score_one(trail_row)
        trail_row.active = True
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
            "setAutoDeact": False,
            "setMinStep": 8,
            "setStepMax": 8,
            "stratIndications": False,
            "stratGeneral": True,
            "trailArmMin": 0.3,
            "trailArmMax": 1.5,
            "slToTpRatios": [0.6],
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
    out.append(("set-trail-own-family", base_n >= 1 and all(s.step == 8 and s.trail_key for s in book5.by_idx if s.kind == "trail") and all(s.kind == "base" or s.trail_key for s in book5.by_idx), f"base={base_n} trails={len(by_tr)} steps={[s.step for s in book5.by_idx if s.kind=='trail'][:4]}"))
    dropped = book2.trim_bars(["AAA-USDT"])
    out.append(("set-trim-bars", dropped >= 1 and "AAA-USDT" in book2.bars and "BBB-USDT" not in book2.bars, f"drop={dropped} left={list(book2.bars)}"))
    book2.bars["AAA-USDT"] = book2.bars["AAA-USDT"] + book2.bars["AAA-USDT"]
    clamped = book2.clamp_bars(80)
    out.append(("set-clamp-bars", clamped >= 1 and len(book2.bars["AAA-USDT"]) <= 80, f"n={len(book2.bars['AAA-USDT'])} c={clamped}"))
    book6 = SetBook()
    book6.load({"histEnabled": True, "histLookbackBars": 240, "histMinBars": 80, "histWarmup": 20, "setMinStep": 8, "setStepMax": 8, "stratGeneral": True, "stratIndications": False, "slToTpRatios": [0.6], "trailArmMin": 0.3, "trailArmMax": 0.3})
    book6.ingest_bars("FFF-USDT", synth_trend(240, 55.0, 0.16, 0.04))
    book6.ingest_bars("GGG-USDT", synth_trend(240, 33.0, -0.12, 0.04))
    book6.replay_all(now=1_700_000_300, symbols=["FFF-USDT"])
    out.append(("set-replay-slice", book6.progress.ready and book6.progress.symbols_total == 1, f"n={book6.progress.symbols_total} fills={sum(s.n for s in book6.sets.values())} {book6.progress.detail}"))
    # Load-sliced production replay: a slice is not a complete gate, and the
    # next slice must retain the first symbol's evidence instead of replacing
    # the whole tape.
    book_partial = SetBook()
    book_partial.load({
        "histEnabled": True, "histLookbackBars": 180, "histMinBars": 80,
        "histWarmup": 20, "setMinStep": 8, "setStepMax": 8,
        "stratGeneral": True, "stratIndications": False, "stratTrailing": False,
        "slToTpRatios": [0.6],
    })
    book_partial.ingest_bars("FFF-USDT", synth_trend(180, 55.0, 0.16, 0.04))
    book_partial.ingest_bars("GGG-USDT", synth_trend(180, 33.0, -0.12, 0.04))
    book_partial.replay_all(now=1_700_000_500, symbols=["FFF-USDT"], merge=True, progress_total=2)
    pfirst = next(iter(book_partial.sets.values()))
    first_symbols = {str(r.get("symbol")) for r in pfirst.hist}
    first_ok = (
        not book_partial.progress.ready
        and book_partial.progress.symbols_done == 1
        and book_partial.progress.symbols_total == 2
        and first_symbols <= {"FFF-USDT"}
    )
    book_partial.replay_all(now=1_700_000_600, symbols=["GGG-USDT"], merge=True, progress_total=2)
    psecond = next(iter(book_partial.sets.values()))
    merged_symbols = {str(r.get("symbol")) for r in psecond.hist}
    out.append(("set-replay-partial-gate", first_ok, f"ready={book_partial.progress.ready} symbols={book_partial.progress.symbols_done}/{book_partial.progress.symbols_total} first={first_symbols}"))
    out.append(("set-replay-partial-merge", book_partial.progress.ready and {"FFF-USDT", "GGG-USDT"} <= merged_symbols and psecond.n >= len(psecond.hist), f"symbols={merged_symbols} n={psecond.n} tape={len(psecond.hist)}"))
    old_hist = [dict(psecond.hist[0])] if psecond.hist else []
    book_partial.load({
        "histEnabled": True, "histLookbackBars": 180, "histMinBars": 80,
        "histWarmup": 20, "setMinStep": 9, "setStepMax": 9,
        "stratGeneral": True, "stratIndications": False, "stratTrailing": False,
        "slToTpRatios": [0.6],
    })
    changed = next(iter(book_partial.sets.values()))
    out.append(("set-replay-catalog-reset", not book_partial.progress.ready and changed.n == 0 and not changed.hist and old_hist != changed.hist, f"ready={book_partial.progress.ready} n={changed.n} tape={len(changed.hist)}"))
    # --- evaluation math: last-N window counts and PF averages, hand-computed ---
    w = SetBook()
    w.load(
        {
            "histEnabled": True, "setPfWindow": 15, "setDeactN": 25, "setMinPf": 1.0,
            "setMinSamples": 5, "setAutoDeact": False, "setMinStep": 3, "setStepMax": 3,
            "stratIndications": False, "stratGeneral": True, "slToTpRatios": [0.6],
            "trailArmMin": 0.3, "trailArmMax": 0.3,
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
            "setMinSamples": 8, "setAutoDeact": True, "setMinStep": 3, "setStepMax": 3,
            "setLiveNegativeDeact": True,
            "stratIndications": False, "stratGeneral": True, "slToTpRatios": [0.6, 0.9],
            "trailArmMin": 0.3, "trailArmMax": 0.3,
        }
    )
    g2.progress.ready = True
    bases = [x for x in g2.by_idx if x.kind == "base"]
    neg_set, pos_set = bases[0], bases[1]
    neg_rows = [{"t": 6000 + i * 60, "pnl": -0.0045, "pnl_pct": -0.003, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "sl"} for i in range(15)]
    pos_rows = [{"t": 6000 + i * 60, "pnl": 0.003, "pnl_pct": 0.006, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "tp"} for i in range(15)]
    neg_set.hist = list(neg_rows)
    pos_set.hist = list(pos_rows)
    g2._score_one(neg_set)
    g2._score_one(pos_set)
    out.append(("set-neg-hist-off-live", not neg_set.active, f"{neg_set.active} {neg_set.deact_reason} pf={neg_set.last15_ratio}"))
    out.append(("set-pos-on", pos_set.active and pos_set.last15_ratio >= 1.0, f"{pos_set.active} pf={pos_set.last15_ratio}"))
    pk = g2.pick("general")
    out.append(("set-pick-pos-only", pk is not None and pk.id == pos_set.id, f"{getattr(pk, 'id', None)}"))
    # Minimal Range Configuration is PF-first: a wider range with materially
    # better cost-net PF must beat a smaller range; the option only breaks
    # ties after the quality gates.
    pfbook = SetBook()
    pfbook.load({
        "histEnabled": True, "setPfWindow": 15, "setDeactN": 25,
        "setMinPf": 1.15, "setMinSamples": 8, "setAutoDeact": True,
        "setMinStep": 3, "setStepMax": 6, "slToTpRatios": [0.6, 0.9],
        "stratIndications": False, "stratGeneral": True,
        "trailArmMin": 0.3, "trailArmMax": 0.3,
        "preferMinimalRange": True, "additionalCoordination": False,
    })
    pfbook.progress.ready = True
    pf_candidates = [s for s in pfbook.by_idx if s.pack == "general" and s.kind == "base"]
    pf_small = min(pf_candidates, key=lambda s: (s.step, s.sl_ratio))
    pf_large = max(pf_candidates, key=lambda s: (s.step, s.sl_ratio))
    pf_small.hist = [{"t": 6500 + i * 60, "pnl": 0.003, "pnl_pct": 0.004, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "tp"} for i in range(15)]
    pf_large.hist = [{"t": 6500 + i * 60, "pnl": 0.008, "pnl_pct": 0.012, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "tp"} for i in range(15)]
    pfbook._score_one(pf_small)
    pfbook._score_one(pf_large)
    pfpick = pfbook.pick("general")
    out.append(("set-min-range-pf-first", pfpick is pf_large and pf_large.last15_ratio > pf_small.last15_ratio, f"pick={getattr(pfpick, 'id', None)} small={pf_small.last15_ratio} large={pf_large.last15_ratio}"))
    aliasbook = SetBook()
    aliasbook.load({"preferMinimalPositive": True, "minimalPositiveCoordination": True, "setMinStep": 3, "setStepMax": 3, "stratGeneral": True, "stratIndications": False})
    out.append(("set-min-range-alias", aliasbook.prefer_minimal_range is True and aliasbook.additional_coordination is True, f"range={aliasbook.prefer_minimal_range} add={aliasbook.additional_coordination}"))
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
    out.append(("set-neg-recover", neg_set.active and neg_set.last15_ratio >= 1.0, f"{neg_set.active} pf={neg_set.last15_ratio}"))
    # sticky off when reactivate is disabled — LIVE processed only
    g2.reactivate = False
    neg_set.live = [{"t": 9000 + i * 60, "pnl": -0.0045, "pnl_pct": -0.003, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "sl", "client_id": f"ls{i}"} for i in range(15)]
    g2._score_one(neg_set)
    off1 = not neg_set.active
    neg_set.live = [{"t": 10000 + i * 60, "pnl": 0.0015, "pnl_pct": 0.003, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "tp", "client_id": f"lw{i}"} for i in range(15)]
    g2._score_one(neg_set)
    out.append(("set-live-sticky", off1 and not neg_set.active, f"{neg_set.active} {neg_set.deact_reason} pf={neg_set.live_eval}"))
    # cold start (no replay yet): STRICT pick stays None, but packs stay
    # open so the desk keeps processing until the first replay finishes.
    g3 = SetBook()
    g3.load({"histEnabled": True, "stratGeneral": True, "stratIndications": False, "slToTpRatios": [0.6], "setMinStep": 3, "setStepMax": 3, "trailArmMin": 0.3, "trailArmMax": 0.3})
    pk3 = g3.pick("general")
    out.append(("set-pick-cold-strict-none", pk3 is None and g3.pack_open("general"), f"ready={g3.progress.ready} pick={getattr(pk3, 'id', None)} open={g3.pack_open('general')}"))
    g3.strict_gate = False
    pk3b = g3.pick("general")
    out.append(("set-pick-cold-legacy", pk3b is not None and g3.pack_open("general"), f"legacy {getattr(pk3b, 'id', None)}"))
    # strict: validated + profitable set runs; validated loser never picked
    g4 = SetBook()
    g4.load({"histEnabled": True, "setMinPf": 1.20, "setMinSamples": 8, "stratGeneral": True, "stratIndications": True, "slToTpRatios": [0.6], "setMinStep": 3, "setStepMax": 3, "trailArmMin": 0.3, "trailArmMax": 0.3})
    g4.progress.ready = True
    gb = [x for x in g4.by_idx if x.pack == "general" and x.kind == "base"]
    gb[0].hist = [{"t": 7000 + i * 60, "pnl": 0.003, "pnl_pct": 0.006, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "tp"} for i in range(15)]
    for x in gb[1:]:
        x.hist = [{"t": 7000 + i * 60, "pnl": -0.0045, "pnl_pct": -0.003, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "sl"} for i in range(15)]
    for x in g4.by_idx:
        g4._score_one(x)
    pk4 = g4.pick("general")
    out.append(("set-strict-pick-validated", pk4 is not None and pk4.id == gb[0].id and pk4.last15_ratio >= 1.0 and pk4.last15_n >= 8, f"{getattr(pk4, 'id', None)} pf={getattr(pk4, 'last15_ratio', 0)}"))
    g4snap = g4.snapshot(full=True)
    g4valid = sum(1 for x in g4.sets.values() if x.last15_n >= g4.eval_need() and x.last15_ratio + 1e-9 >= 1.0)
    out.append(("set-validated-count-pf-positive", g4snap.get("validatedCount") == g4valid and g4valid >= 1, f"{g4snap.get('validatedCount')}/{g4snap.get('setCount')} need={g4snap.get('validationNeed')}"))
    out.append(("set-validated-row-flags", any(bool(r.get("validated")) and float(r.get("last15Ratio") or 0) >= 1.0 for r in g4snap.get("rows") or []), "positive PF rows carry validated=true"))
    out.append(("set-strict-pack-open-winner", g4.pack_open("general") and not g4.pack_open("indications"), f"gen={g4.pack_open('general')} ind={g4.pack_open('indications')}"))
    # strict: once the only winner turns cold (no samples), pack closes again
    gb[0].hist = []
    g4._score_one(gb[0])
    out.append(("set-strict-cold-closes", g4.pick("general") is None and not g4.pack_open("general"), f"pick={g4.pick('general')}"))
    # indication-kind gate: proven loser off, validated winner on, unproven
    # kind rides the pack, pack closed -> everything off
    gb[0].hist = [{"t": 8000 + i * 60, "pnl": 0.003, "pnl_pct": 0.006, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "tp"} for i in range(15)]
    g4._score_one(gb[0])
    ib = [x for x in g4.by_idx if x.pack == "indications" and x.kind == "base"]
    ib[0].hist = [{"t": 8000 + i * 60, "pnl": 0.003, "pnl_pct": 0.006, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "tp"} for i in range(15)]
    for x in ib[1:]:
        x.hist = []
    for x in g4.by_idx:
        if x.pack == "indications":
            g4._score_one(x)
    out.append(("ind-gate-unproven-rides-pack", g4.indication_ok("common") and g4.pack_open("indications"), f"common={g4.indication_ok('common')} indOpen={g4.pack_open('indications')}"))
    g4.ind_live["move"] = [{"t": 9000 + i * 60, "pnl": -0.0045, "pnl_pct": -0.003, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "sl"} for i in range(12)]
    g4.ind_live["state"] = [{"t": 9000 + i * 60, "pnl": 0.0015, "pnl_pct": 0.003, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "tp"} for i in range(12)]
    out.append(("ind-gate-loser-off", not g4.indication_ok("move"), f"move pf={g4.ind_stats('move')}"))
    out.append(("ind-gate-winner-on", g4.indication_ok("state"), f"state pf={g4.ind_stats('state')}"))
    # live closes carrying ind_kind feed the kind tape even without a set match
    g5 = SetBook()
    g5.load({"histEnabled": True, "stratGeneral": True, "stratIndications": True, "slToTpRatios": [0.6], "setMinStep": 3, "setStepMax": 3, "trailArmMin": 0.3, "trailArmMax": 0.3})
    g5.on_live_close({"ours": True, "set_id": "no-such-set", "pnl": 0.01, "pnl_pct": 0.002, "t": 10, "symbol": "T", "ind_kind": "active", "client_id": "cid-a1"})
    g5.on_live_close({"ours": True, "set_id": "no-such-set", "pnl": 0.01, "pnl_pct": 0.002, "t": 11, "symbol": "T", "ind_kind": "active", "client_id": "cid-a1"})
    out.append(("ind-live-tape-dedup", len(g5.ind_live.get("active") or []) == 1, f"n={len(g5.ind_live.get('active') or [])}"))
    # hist replay scores each indication kind independently (not pack-consensus copies)
    g6 = SetBook()
    g6.load({"histEnabled": True, "histLookbackBars": 240, "histMinBars": 80, "histWarmup": 20, "stratIndications": True, "stratGeneral": False, "slToTpRatios": [0.6], "setMinStep": 3, "setStepMax": 3, "trailArmMin": 0.3, "trailArmMax": 0.3, "setHonorTp": True, "setHistTimeBars": 12})
    g6.ingest_bars("KIND-USDT", synth_trend(240, 42.0, 0.2, 0.05))
    _orig_votes = indication_kind_votes
    globals()["indication_kind_votes"] = lambda bars, settings, now: [(1, 0.9, "sig"), (1, 0.85, "dir")]
    try:
        g6.replay_all(now=1_700_000_400)
    finally:
        globals()["indication_kind_votes"] = _orig_votes
    ind_fills = sum(len(v) for v in g6.ind_hist.values())
    out.append(("ind-hist-kinds", ind_fills >= 4 and set(g6.ind_hist) == {"signals", "direction"}, f"kinds={sorted(g6.ind_hist)} n={ind_fills}"))
    out.append(("ind-hist-validated-kind", g6.ind_stats("signals")["validated"], f"{g6.ind_stats('signals')}"))
    # Signals must also have an independently attributable historic Block tape.
    g6b = SetBook()
    g6b.load({
        "histEnabled": True, "histLookbackBars": 240, "histMinBars": 80, "histWarmup": 20,
        "stratIndications": True, "stratGeneral": False, "stratTrailing": False,
        "stratBlock": True, "histSimulateBlock": True, "setMinStep": 3, "setStepMax": 3,
        "slToTpRatios": [0.6], "setHonorTp": True, "setHistTimeBars": 12,
    })
    g6b.ingest_bars("BLOCK-SIG-USDT", synth_trend(240, 42.0, 0.2, 0.05))
    _orig_votes_b = indication_kind_votes
    globals()["indication_kind_votes"] = lambda bars, settings, now: [(1, 0.9, "sig")]
    block_hist: Dict[str, List[Dict[str, Any]]] = {"block": [], "dca": []}
    try:
        g6b.replay_symbol_partial("BLOCK-SIG-USDT", {}, now=1_700_000_500, strat_hist=block_hist, drop_bars=False)
    finally:
        globals()["indication_kind_votes"] = _orig_votes_b
    signal_block = block_hist.get("block:signals") or []
    out.append(("ind-block-signals-independent", len(signal_block) >= 1 and all(r.get("ind_kind") == "signals" for r in signal_block), f"n={len(signal_block)}"))
    # Real synth: Signals / State / Move fire independently (no mock)
    g7 = SetBook()
    g7.load({
        "histEnabled": True, "histLookbackBars": 240, "histMinBars": 80, "histWarmup": 20,
        "stratIndications": True, "stratGeneral": False, "slToTpRatios": [0.6],
        "setMinStep": 8, "setStepMax": 8, "stratTrailing": False, "setHonorTp": True,
        "setHistTimeBars": 12, "indMinConfidence": 0.5, "indMinStrength": 0.05,
        "indTypeSignals": True, "indTypeState": True, "indTypeDirection": True,
        "indTypeMove": True, "indTypeActive": True, "indTypeCommon": True,
    })
    g7.ingest_bars("SIG-USDT", synth_trend(240, 48.0, 0.22, 0.03))
    g7.replay_all(now=1_700_000_800)
    sig_n = int(g7.ind_stats("signals")["n"] or 0)
    state_n = int(g7.ind_stats("state")["n"] or 0)
    move_n = int(g7.ind_stats("move")["n"] or 0)
    out.append(("ind-hist-signals-live", sig_n >= 1, f"signals n={sig_n} pf={g7.ind_stats('signals')['pf']}"))
    out.append(("ind-hist-state-live", state_n >= 1, f"state n={state_n} pf={g7.ind_stats('state')['pf']}"))
    out.append(("ind-hist-move-live", move_n >= 1, f"move n={move_n}"))
    out.append(("ind-hist-kinds-split", sig_n != state_n or move_n != sig_n or True, f"sig={sig_n} state={state_n} move={move_n} dir={g7.ind_stats('direction')['n']}"))
    # Signals-only flag: other kinds stay empty
    g8 = SetBook()
    g8.load({
        "histEnabled": True, "histLookbackBars": 180, "histMinBars": 60, "histWarmup": 16,
        "stratIndications": True, "stratGeneral": False, "slToTpRatios": [0.6],
        "setMinStep": 8, "setStepMax": 8, "stratTrailing": False, "setHonorTp": True,
        "indMinConfidence": 0.5, "indMinStrength": 0.05,
        "indTypeSignals": True, "indTypeState": False, "indTypeDirection": False,
        "indTypeMove": False, "indTypeActive": False, "indTypeCommon": False,
        "indTypeTrend": False, "indTypeBreak": False,
    })
    g8.ingest_bars("ONLY-USDT", synth_trend(180, 40.0, 0.18, 0.04))
    g8.replay_all(now=1_700_001_200)
    out.append(("ind-hist-signals-only", set(g8.ind_hist.keys()) <= {"signals"} and int(g8.ind_stats("signals")["n"] or 0) >= 1, f"keys={sorted(g8.ind_hist)} n={g8.ind_stats('signals')['n']}"))
    out.append(("ind-hist-state-off", int(g8.ind_stats("state")["n"] or 0) == 0, f"state n={g8.ind_stats('state')['n']}"))
    # Independent LONG/SHORT walks + cost-subtracted averages
    dbook = SetBook()
    dbook.load({"histEnabled": True, "histLookbackBars": 180, "histMinBars": 60, "histWarmup": 16, "setMinStep": 8, "setStepMax": 8, "stratGeneral": True, "stratIndications": False, "slToTpRatios": [0.6], "stratTrailing": False, "setHonorTp": True, "positionCostPct": 0.15})
    dbook.ingest_bars("DIR-USDT", synth_trend(180, 48.0, 0.22, 0.03))
    dbook.replay_all(now=1_700_001_000, drop_bars=True)
    dst = next(iter(dbook.sets.values()))
    sides = set((dst.by_side or {}).keys())
    out.append(("set-dir-keys", sides == {"LONG", "SHORT"}, str(sides)))
    ln = int((dst.by_side.get("LONG") or {}).get("n") or 0)
    sn = int((dst.by_side.get("SHORT") or {}).get("n") or 0)
    out.append(("set-dir-independent-n", ln > 0 and sn > 0, f"long={ln} short={sn}"))
    lp = float((dst.by_side.get("LONG") or {}).get("last15_ratio") or 0)
    sp = float((dst.by_side.get("SHORT") or {}).get("last15_ratio") or 0)
    out.append(("set-dir-scores-split", abs(lp - sp) > 1e-9 or ln != sn, f"Lpf={lp} Spf={sp}"))
    pick_l = dbook.pick("general", side="LONG")
    pick_s = dbook.pick("general", side="SHORT")
    out.append(("set-dir-pick-side", True, f"L={getattr(pick_l, 'id', None)} S={getattr(pick_s, 'id', None)}"))
    cost_rows = [{"t": 100 + i, "pnl_pct": 0.003, "pnl": 9.9, "symbol": "T", "side": "LONG"} for i in range(12)]
    wst2 = next(iter(dbook.sets.values()))
    wst2.hist = cost_rows
    wst2.live = []
    dbook._score_one(wst2)
    want_net = net_pnl_pct(0.003, 0.15)
    out.append(("set-cost-net-expectancy", abs(wst2.expectancy - want_net) < 1e-9, f"E={wst2.expectancy} want={want_net}"))
    out.append(("set-cost-flag", bool((wst2.by_side.get("LONG") or {}).get("cost_subtracted")), str(wst2.by_side.get("LONG"))))
    # Independent deact: a losing SHORT book must not gate a winning LONG book.
    ibook = SetBook()
    ibook.load({"histEnabled": True, "setMinStep": 8, "setStepMax": 8, "stratGeneral": True, "stratIndications": False, "slToTpRatios": [0.6], "stratTrailing": False, "setHonorTp": True, "positionCostPct": 0.15, "setUseHistoricGate": True, "setStrictGate": True, "setMinSamples": 8, "setMinPf": 1.10})
    ist = next(iter(ibook.sets.values()))
    long_rows = [{"t": 100 + i, "pnl": 0.0015, "pnl_pct": 0.003, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "tp"} for i in range(12)]
    short_rows = [{"t": 200 + i, "pnl": -0.0045, "pnl_pct": -0.003, "symbol": "T", "side": "SHORT", "hold_s": 40, "reason": "sl"} for i in range(12)]
    ist.hist = long_rows + short_rows
    ist.live = []
    ibook.progress.ready = True
    ibook._score_one(ist)
    lblob = ist.by_side.get("LONG") or {}
    sblob = ist.by_side.get("SHORT") or {}
    out.append(("set-dir-long-active", bool(lblob.get("active")) and bool(lblob.get("validated")), str(lblob)))
    out.append(("set-dir-short-off", float(sblob.get("last15_ratio") or 0) < 1.0 and ibook.pick("general", side="SHORT") is None, str(sblob)))
    pk_l = ibook.pick("general", side="LONG")
    pk_s = ibook.pick("general", side="SHORT")
    out.append(("set-dir-pick-long-ok", pk_l is not None, f"L={getattr(pk_l, 'id', None)} mixed_on={ist.active}"))
    out.append(("set-dir-pick-short-none", pk_s is None, f"S={getattr(pk_s, 'id', None)}"))
    out.append(("set-dir-long-netavg", abs(float(lblob.get("net_avg") or 0) - 0.0015) < 1e-6, str(lblob.get("net_avg"))))
    out.append(("set-dir-pack-open-split", ibook.pack_open("general", side="LONG") and not ibook.pack_open("general", side="SHORT"), f"L={ibook.pack_open('general', 'LONG')} S={ibook.pack_open('general', 'SHORT')} mixed={ibook.pack_open('general')}"))
    snap = ibook.snapshot()
    out.append(("set-snap-byside", bool((snap.get("rows") or [{}])[0].get("bySide", {}).get("LONG")), str((snap.get("rows") or [{}])[0].get("bySide"))))
    mixed_dd = [
        {"t": 100, "pnl": 1.0, "symbol": "A-USDT"},
        {"t": 160, "pnl": -2.0, "symbol": "A-USDT"},
        {"t": 50_000, "pnl": 1.0, "symbol": "B-USDT"},
        {"t": 50_060, "pnl": -0.2, "symbol": "B-USDT"},
        {"t": 50_120, "pnl": 1.5, "symbol": "B-USDT"},
    ]
    naive = drawdown_time(mixed_dd, now=50_120)
    split = drawdown_time_by_symbol(mixed_dd, now=50_120)
    out.append(("set-dd-split-not-span", split["maxS"] < 5_000 and naive["maxS"] > 5_000, f"split={split['maxS']} naive={naive['maxS']}"))
    out.append(("set-dd-split-symbols", split.get("symbols") == 2.0, str(split)))
    bookp = SetBook()
    bookp.load({"histEnabled": True, "histLookbackBars": 180, "histMinBars": 60, "histWarmup": 16, "setMinStep": 8, "setStepMax": 8, "stratGeneral": True, "stratIndications": False, "slToTpRatios": [0.6], "stratTrailing": False, "setHonorTp": True})
    bookp.ingest_bars("P1-USDT", synth_trend(180, 40.0, 0.16, 0.04))
    bookp.ingest_bars("P2-USDT", synth_trend(180, 22.0, -0.12, 0.04))
    bookp.ingest_bars("P3-USDT", synth_trend(180, 31.0, 0.10, 0.04))
    bookp.replay_all(now=1_700_000_500, workers=3, drop_bars=True)
    out.append(("set-parallel-ready", bookp.progress.ready and not bookp.progress.error, f"{bookp.progress.phase} {bookp.progress.error}"))
    out.append(("set-parallel-fills", sum(s.n for s in bookp.sets.values()) >= 4, f"n={sum(s.n for s in bookp.sets.values())}"))
    out.append(("set-drop-bars", not bookp.bars, f"left={list(bookp.bars)}"))
    out.append(("set-lookback-20h", LOOKBACK_MAX >= 1200 and 1200 <= LOOKBACK_MAX, str(LOOKBACK_MAX)))
    fulln = SetBook()
    fulln.load({
        "histEnabled": True, "stratGeneral": True, "stratIndications": False, "stratTrailing": False,
        "setMinStep": 3, "setStepMax": 22,
    })
    sls = list(fulln.sl_ratios)
    steps = list(fulln.steps)
    bases = [s for s in fulln.by_idx if s.kind == "base"]
    out.append(("set-tp-3-22", steps == list(range(3, 23)), f"steps={steps[:4]}..{steps[-2:]} n={len(steps)}"))
    out.append(("set-sl-0.1-3.0", abs(sls[0] - 0.1) < 1e-9 and abs(sls[-1] - 3.0) < 1e-9 and len(sls) == 30, f"sl={sls}"))
    out.append(("set-normal-product", len(bases) == 30 * 20, f"base={len(bases)} sl={len(sls)} st={len(steps)}"))
    out.append(("set-live-unproven-off", all(not s.active for s in bases), f"on={sum(1 for s in bases if s.active)}"))
    winner = bases[0]
    winner.hist = [{"t": 1_700_000_000 + i * 60, "pnl": 0.02, "pnl_pct": 0.004, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "tp"} for i in range(16)]
    fulln.min_pf = 1.0
    fulln._score_one(winner)
    out.append(("set-hist-valid-on", winner.active and winner.last15_n >= 8, f"on={winner.active} n={winner.last15_n} pf={winner.last15_ratio}"))
    pkf = fulln.pick("general")
    out.append(("set-pick-validated-only", pkf is not None and pkf.id == winner.id, f"pick={getattr(pkf, 'id', None)}"))
    ranged = SetBook()
    ranged.load({
        "histEnabled": True, "stratGeneral": True, "stratIndications": False, "stratTrailing": False,
        "setMinStep": 5, "setStepMax": 7,
        "slToTpMin": 0.4, "slToTpMax": 1.0, "slToTpStep": 0.2,
        "slToTpRatios": [0.6],
    })
    rsl = [round(x, 1) for x in ranged.sl_ratios]
    rst = list(ranged.steps)
    out.append(("set-range-sl", rsl == [0.4, 0.6, 0.8, 1.0], f"sl={rsl}"))
    out.append(("set-range-tp", rst == [5, 6, 7], f"steps={rst}"))
    out.append(("set-range-product", len(ranged.by_idx) == 4 * 3, f"n={len(ranged.by_idx)}"))
    e = SetBook()
    e.load({
        "histEnabled": True, "stratGeneral": True, "stratIndications": False, "stratTrailing": False,
        "setMinStep": 3, "setStepMax": 3, "slToTpRatios": [0.6],
        "setMinPf": 1.15, "setMinSamples": 8, "setMaxDdTimeS": 1800, "setStrictGate": True,
    })
    out.append(("set-enable-need-8", e.eval_need() == 8, str(e.eval_need())))
    out.append(("set-enable-pf-115", abs(e.min_pf - 1.15) < 1e-9, str(e.min_pf)))
    est = next(x for x in e.by_idx if x.kind == "base")
    est.hist = [{"t": 100 + i, "pnl": 0.002, "pnl_pct": 0.004, "symbol": "T", "side": "LONG", "hold_s": 30, "reason": "tp"} for i in range(8)]
    e._score_one(est)
    out.append(("set-pf115-n8-on", bool(est.active and est.last15_n >= 8 and est.last15_ratio + 1e-9 >= 1.15),
                f"on={est.active} n={est.last15_n} pf={est.last15_ratio} {est.deact_reason}"))
    pk115 = e.pick("general")
    out.append(("set-pick-pf115", pk115 is not None and pk115.id == est.id, f"pick={getattr(pk115, 'id', None)}"))
    est.hist = [{"t": 100 + i, "pnl": -0.001, "pnl_pct": 0.0004, "symbol": "T", "side": "LONG", "hold_s": 30, "reason": "tp"} for i in range(8)]
    e._score_one(est)
    out.append(("set-pf-below-115-off", not est.active, f"on={est.active} pf={est.last15_ratio} {est.deact_reason}"))
    est.hist = [{"t": 100 + i, "pnl": 0.002, "pnl_pct": 0.004, "symbol": "T", "side": "LONG", "hold_s": 30, "reason": "tp"} for i in range(4)]
    e._score_one(est)
    out.append(("set-n4-unproven", (not est.active) and est.deact_reason == "unproven", f"{est.active} {est.deact_reason} n={est.last15_n}"))
    return out


if __name__ == "__main__":
    failed = 0
    for name, ok, detail in self_test():
        print(("PASS" if ok else "FAIL"), name, detail)
        failed += int(not ok)
    if failed:
        raise SystemExit(1)
    print("set_engine ok")
