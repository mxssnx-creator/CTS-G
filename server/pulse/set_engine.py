#!/usr/bin/env python3
"""Independent config Sets: 1m historic replay, last-15 PF, max DD time, last-25 deact.

A Set is one (pack × SL:TP ratio × trail) book. Historic walks 1-minute OHLC,
simulates entries/exits, then scores each Set on its own tape. Live closes
merge into the same book. Last 25 average Result-R < 0 deactivates that Set.
"""
from __future__ import annotations

import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from position_cost import (
    LAST_N_DEFAULT,
    POSITION_COST_PCT_DEFAULT,
    last_n_cost_pf,
    signed_result_r,
    snap_ratio,
    SL_TP_RATIOS,
    cost_as_frac,
    net_pnl_pct,
    row_net_pnl,
    row_side,
    filter_side,
)
from indication_engine import bars_to_candles, evaluate_signal_candles, evaluate_ta_pack, evaluate_direction, evaluate_move, evaluate_active, evaluate_common, ohlcv_row
from risk_variants import TRAIL_VARIANTS, give_from_arm, parse_trail, trail_candidates, trail_key

PACKS = ("indications", "general")
DIRECTIONS = ("LONG", "SHORT")
DEACT_N_DEFAULT = 25
PF_N_DEFAULT = 15
LOOKBACK_DEFAULT = 480
LOOKBACK_MAX = 1440
WARMUP_DEFAULT = 30
BAR_S = 60.0
FEE_PCT = 0.001  # round-trip, matches live close_pos
STEP_MIN = 3
STEP_MAX = 22
HIST_CAP = 240
# Indication kinds (live) <-> historic replay vote tags (indication_signal why).
IND_KINDS = ("state", "signals", "active", "direction", "move", "common")
IND_TAG_KIND = {"sig": "signals", "ta": "state", "dir": "direction", "move": "move", "act": "active", "common": "common"}


def clamp_step(v: Any, lo: int = STEP_MIN, hi: int = STEP_MAX) -> int:
    try:
        n = int(v)
    except Exception:
        n = lo
    return max(lo, min(hi, n))


def step_tp_pct(step: int, cost_pct: float) -> float:
    """TP fraction = step × position cost. Cost 0.15 means 0.15% → step 3 = 0.45%."""
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
    now = now or time.time()
    ordered = sorted(rows, key=lambda r: finite(r.get("t")))
    last_t = finite(ordered[-1].get("t")) if ordered else 0.0
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
    return {
        "episodes": float(episodes),
        "maxS": round(max(p["maxS"] for p in parts), 1),
        "avgS": round(sum(p["avgS"] for p in parts) / len(parts), 1),
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
    if trail:
        return f"{pack}:1m:sl{sl_ratio:.1f}:tr{trail}"
    if step:
        return f"{pack}:1m:sl{sl_ratio:.1f}:st{int(step)}"
    return f"{pack}:1m:sl{sl_ratio:.1f}"


def make_trail_id(pack: str, trail: str, sl_ratio: float = 0.6) -> str:
    return make_set_id(pack, sl_ratio, trail=trail)


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
    active: bool = True
    deact_reason: str = ""
    locked: bool = False
    source_n: int = 0
    by_side: Dict[str, Dict[str, Any]] = field(default_factory=dict)

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
        self.min_pf = 1.10
        self.max_dd_s = 420.0
        self.auto_deact = True
        self.use_historic_gate = True
        self.min_samples = 12
        self.reactivate = True
        self.strict_gate = True
        self.max_active = 0
        self.cost_pct = POSITION_COST_PCT_DEFAULT
        self.time_stop_s = 21600.0
        self.hist_time_bars = 45
        self.scratch_s = 90.0
        self.scratch_min = 0.0016
        self.tp_pct = 0.0075
        self.cooldown_bars = 2
        self.ignore_tp = True
        self.opt_sl = 0.0030
        self.min_step_cfg = STEP_MIN
        self.min_step = STEP_MIN
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
        self._running = False

    def load(self, ov: Dict[str, Any], cts: Optional[Dict[str, Any]] = None) -> None:
        cts = cts or {}
        self.enabled = bool(ov.get("histEnabled", True))
        self.lookback = max(120, min(LOOKBACK_MAX, int(ov.get("histLookbackBars") or LOOKBACK_DEFAULT)))
        self.min_bars = max(60, min(self.lookback, int(ov.get("histMinBars") or 120)))
        self.warmup = max(16, min(80, int(ov.get("histWarmup") or WARMUP_DEFAULT)))
        self.refresh_s = max(30.0, min(600.0, float(ov.get("histRefreshS") or 90)))
        self.pf_n = max(5, min(50, int(ov.get("setPfWindow") or ov.get("pfWindow") or PF_N_DEFAULT)))
        self.deact_n = max(10, min(80, int(ov.get("setDeactN") or DEACT_N_DEFAULT)))
        self.min_pf = float(ov.get("setMinPf") or ov.get("minPf") or 1.10)
        self.max_dd_s = max(30.0, float(ov.get("setMaxDdTimeS") or 1800))
        self.auto_deact = bool(ov.get("setAutoDeact", True))
        self.use_historic_gate = bool(ov.get("setUseHistoricGate", True))
        self.min_samples = max(5, min(40, int(ov.get("setMinSamples") or 12)))
        self.reactivate = bool(ov.get("setReactivate", True))
        # Strict gate (default ON): only VALIDATED (last-N samples >=
        # max(minSamples, 8)) AND PROFITABLE (cost-adjusted PF >= 1.00) sets
        # and indication kinds may drive live orders. Cold/unproven sets keep
        # collecting historic + simulated evidence but never trade live.
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
        self.time_stop_s = float(ov.get("timeStopS") or 21600)
        self.hist_time_bars = max(8, min(120, int(ov.get("setHistTimeBars") or 45)))
        self.scratch_s = float(ov.get("scratchS") or 90)
        tp = float(ov.get("tpPct") or 0.75)
        self.tp_pct = tp / 100.0 if tp > 0.05 else tp
        self.ignore_tp = bool(ov.get("exitIgnoreTp", True))
        self.hist_honor_tp = bool(ov.get("setHonorTp", True))
        opt = float(ov.get("exitOptSlPct") or 0.30)
        self.opt_sl = opt / 100.0 if opt > 0.02 else opt
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
        raw_ratios = ov.get("slToTpRatios") or list(SL_TP_RATIOS)
        ratios: List[float] = []
        for x in raw_ratios:
            try:
                ratios.append(snap_ratio(float(x)))
            except Exception:
                continue
        self.sl_ratios = sorted(set(ratios)) or list(SL_TP_RATIOS)
        self.trail_enabled = bool(ov.get("stratTrailing", True))
        if self.trail_enabled:
            self.trails = trail_candidates(
                float(ov.get("trailArmMin") or 0.3),
                float(ov.get("trailArmMax") or 1.5),
                float(ov.get("trailGiveMin") or 0.1),
                float(ov.get("trailGiveMax") or 0.5),
                float(ov.get("trailGiveFactor") or 1.0 / 3.0),
                bool(ov.get("trailRecalcGive", True)),
                ov.get("trailVariants") or list(TRAIL_VARIANTS),
            )
        else:
            self.trails = []
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
        self._rebuild_sets()

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
        trails = list(self.trails) if self.trails and getattr(self, "trail_enabled", True) else []
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
            for sl_i, sl in enumerate(self.sl_ratios):
                for tr_i, (tkey, arm, give) in enumerate(trails):
                    sid = make_trail_id(pack, tkey, sl)
                    prev = keep.get(sid)
                    mid_step = self.steps[len(self.steps) // 2] if self.steps else 8
                    tp = step_tp_pct(mid_step, self.cost_pct)
                    if prev:
                        st = prev
                        st.trail_key = tkey
                        st.trail_arm = arm
                        st.trail_give = give
                        st.sl_ratio = sl
                        st.step = 0
                        st.tp_pct = tp
                        st.kind = "trail"
                        st.locked = bool(self.locks.get(sid))
                    else:
                        st = SetState(
                            id=sid, pack=pack, tf="1m", sl_ratio=sl,
                            trail_key=tkey, trail_arm=arm, trail_give=give,
                            step=0, tp_pct=tp, kind="trail",
                            locked=bool(self.locks.get(sid)),
                        )
                    st.pack_i = pack_i
                    st.sl_i = sl_i
                    st.tr_i = tr_i
                    st.step_i = -1
                    _put(st)
        self.sets = next_sets
        self.by_idx = by_idx
        self.progress.sets_total = len(self.sets)

    def adapt_from_live(self, closed: Sequence[Any]) -> None:
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
                if signed_result_r(pct if pct else pnl, self.cost_pct) > 0:
                    n_ok += 1
            avg = sum(pnls) / len(pnls) if pnls else 0.0
            if avg < 0:
                n = n_ok if n_ok else n_pos
                nxt = clamp_step(n if n else floor, floor, self.step_max)
            else:
                nxt = floor
        self.min_step = nxt
        # Prefer a higher step when picking; never drop SL×TP sets.

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
            }
            ind_kind = str(rec.get("ind_kind") or rec.get("indKind") or "")
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
            }
            ind_kind = str(getattr(rec, "ind_kind", "") or "")
        # Per-kind live evidence feeds the indication gate even when the close
        # cannot be attributed to a known set below.
        if ind_kind:
            tape = self.ind_live.setdefault(ind_kind, [])
            cid0 = row.get("client_id") or ""
            if not (cid0 and any(r.get("client_id") == cid0 for r in tape)):
                tape.append(dict(row))
                self.ind_live[ind_kind] = trim_hist(tape, HIST_CAP)
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
            self.progress = Progress(
                phase="replay",
                pct=1.0,
                sets_total=len(self.sets),
                symbols_total=len(names),
                bars_total=sum(len(self.bars.get(s) or []) for s in names),
                cycle=self.progress.cycle + 1,
                detail=f"{len(names)} symbols · {len(self.sets)} sets",
            )
            hist: Dict[str, List[Dict[str, Any]]] = {sid: [] for sid in self.sets}
            ind_hist: Dict[str, List[Dict[str, Any]]] = {}
            aborted = False
            w = max(1, min(int(workers or 1), 8, len(names) or 1))
            lock = threading.Lock()

            def _merge(symbol: str, local: Dict[str, List[Dict[str, Any]]], local_ind: Dict[str, List[Dict[str, Any]]]) -> None:
                for sid, rows in local.items():
                    if rows:
                        hist.setdefault(sid, []).extend(rows)
                for k, rows in local_ind.items():
                    if rows:
                        ind_hist.setdefault(k, []).extend(rows)
                nbar = len(self.bars.get(symbol) or [])
                if drop_bars:
                    self.bars.pop(symbol, None)
                self.progress.bars_done += nbar

            def _one(symbol: str) -> Tuple[str, Dict[str, List[Dict[str, Any]]], Dict[str, List[Dict[str, Any]]]]:
                local: Dict[str, List[Dict[str, Any]]] = {sid: [] for sid in self.sets}
                local_ind: Dict[str, List[Dict[str, Any]]] = {}
                self._replay_symbol(symbol, local, now, on_step=None, ind_hist=local_ind)
                return symbol, local, local_ind

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
                    _sym, local, local_ind = _one(symbol)
                    _merge(_sym, local, local_ind)
                    done += 1
                    if on_symbol:
                        on_symbol(symbol, done, len(names))
                    if on_step:
                        on_step()
            else:
                with ThreadPoolExecutor(max_workers=w, thread_name_prefix="set-replay") as pool:
                    futs = {pool.submit(_one, s): s for s in names}
                    for fut in as_completed(futs):
                        if abort and abort():
                            aborted = True
                            for pending in futs:
                                pending.cancel()
                            break
                        symbol, local, local_ind = fut.result()
                        with lock:
                            _merge(symbol, local, local_ind)
                            done += 1
                            self.progress.symbol = symbol
                            self.progress.symbols_done = done
                            self.progress.pct = 5.0 + (done / max(1, len(names))) * 80.0
                            self.progress.elapsed_ms = (time.time() - t0) * 1000
                            self.progress.detail = f"replay {symbol} {done}/{len(names)}"
                        if on_symbol:
                            on_symbol(symbol, done, len(names))
                        if on_step:
                            on_step()
            self.progress.phase = "score"
            self.progress.pct = 90.0
            self._commit_hist(hist, ind_hist)
            self.progress.phase = "ready"
            self.progress.pct = 100.0
            self.progress.ready = True
            self.progress.symbols_done = done if aborted else len(names)
            self.progress.sets_done = len(self.sets)
            self.progress.detail = (
                f"{sum(1 for s in self.sets.values() if s.active)}/{len(self.sets)} active · "
                f"{sum(s.n for s in self.sets.values())} hist fills"
                + (" · partial" if aborted else "")
            )
        except Exception as exc:
            self.progress.phase = "error"
            self.progress.error = str(exc)[:220]
        finally:
            self.progress.last_run_ms = (time.time() - t0) * 1000
            self.progress.elapsed_ms = self.progress.last_run_ms
            self.last_run = time.time()
            self._running = False

    def _commit_hist(
        self,
        hist: Dict[str, List[Dict[str, Any]]],
        ind_hist: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> None:
        if ind_hist is not None:
            self.ind_hist = {k: trim_hist(v, HIST_CAP) for k, v in ind_hist.items()}
        for st in self.by_idx:
            full = hist.get(st.id, [])
            full.sort(key=lambda r: finite(r.get("t")))
            st.hist = full
            self._score_one(st)
            n_full = len(full)
            st.hist = trim_hist(full, HIST_CAP)
            st.n = n_full
        self._cap_active()

    def replay_symbol_partial(
        self,
        symbol: str,
        hist: Dict[str, List[Dict[str, Any]]],
        now: Optional[float] = None,
        ind_hist: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        drop_bars: bool = True,
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
        self._replay_symbol(symbol, hist, now, ind_hist=ind_hist)
        if drop_bars:
            self.bars.pop(symbol, None)
        return nbar

    def _replay_symbol(self, symbol: str, hist: Dict[str, List[Dict[str, Any]]], now: float, on_step: Optional[Callable[[], None]] = None, ind_hist: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> None:
        bars = self.bars[symbol]
        n = len(bars)
        warmup = min(self.warmup, max(16, n // 5))
        signals: Dict[str, List[Tuple[int, float, str]]] = {p: [(0, 0.0, "")] * n for p in self.packs}
        kind_sigs: Dict[str, List[Tuple[int, float]]] = {k: [(0, 0.0)] * n for k in IND_KINDS}
        base_ts = now - (n - 1) * BAR_S
        for i in range(warmup, n):
            lo = i + 1 - 60
            window = bars[lo if lo > 0 else 0 : i + 1]
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
        time_bars = max(8, min(self.hist_time_bars, max(8, n - warmup - 1)))
        scratch_bars = max(8, int(self.scratch_s / BAR_S))
        honor_tp = bool(getattr(self, "hist_honor_tp", True))
        for st in self.by_idx:
            pack_sig = signals.get(st.pack) or [(0, 0.0, "")] * n
            sl_frac_base = max(0.0015, st.tp_pct * max(0.3, float(st.sl_ratio or 0.6)))
            use_trail = st.kind == "trail"
            arm = (st.trail_arm / 100.0 if st.trail_arm > 0.05 else st.trail_arm) if use_trail else 0.0
            give = (st.trail_give / 100.0 if st.trail_give > 0.05 else st.trail_give) if use_trail else 0.0
            tp_frac = max(0.0020, st.tp_pct)
            # LONG and SHORT walk independently so one side never blocks the other.
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
                        if use_trail:
                            if side > 0:
                                open_pos["peak"] = max(open_pos["peak"], float(bar[1]))
                                fav = (open_pos["peak"] - entry) / entry
                                if fav >= arm:
                                    trail = open_pos["peak"] * (1 - give)
                                    open_pos["trail"] = max(open_pos.get("trail") or 0.0, trail)
                            else:
                                open_pos["peak"] = min(open_pos["peak"], float(bar[2]))
                                fav = (entry - open_pos["peak"]) / entry
                                if fav >= arm:
                                    trail = open_pos["peak"] * (1 + give)
                                    cur = open_pos.get("trail")
                                    open_pos["trail"] = trail if cur is None else min(cur, trail)
                        elif side > 0:
                            open_pos["peak"] = max(open_pos["peak"], float(bar[1]))
                        else:
                            open_pos["peak"] = min(open_pos["peak"], float(bar[2]))
                        why, px = hit_exit(side, entry, open_pos["sl"], open_pos["tp"], open_pos.get("trail"), bar, ignore_tp=not honor_tp)
                        if why is None and held >= time_bars:
                            why, px = "time", float(bar[3])
                        if why is None and held >= scratch_bars:
                            move = (float(bar[3]) - entry) / entry * side
                            if move >= self.scratch_min:
                                why, px = "scratch+", float(bar[3])
                        if why:
                            raw = (px - entry) / entry * side
                            rec = {
                                "t": ts,
                                "symbol": symbol,
                                "side": "LONG" if side > 0 else "SHORT",
                                "direction": "LONG" if side > 0 else "SHORT",
                                "pnl": net_pnl_pct(raw, self.cost_pct),
                                "pnl_pct": raw,
                                "hold_s": held * BAR_S,
                                "reason": why,
                                "set_id": st.id,
                                "pack": st.pack,
                                "costPct": self.cost_pct,
                            }
                            bucket = hist[st.id]
                            bucket.append(rec)
                            open_pos = None
                            cool = self.cooldown_bars
                        continue
                    if cool > 0:
                        cool -= 1
                        continue
                    d, conf, why = pack_sig[i]
                    if d == 0 or conf < 0.58:
                        continue
                    if d != want_side:
                        continue
                    close = float(bar[3])
                    sl_frac = max(0.0015, sl_frac_base)
                    if d > 0:
                        sl = close * (1 - sl_frac)
                        tp = close * (1 + tp_frac)
                    else:
                        sl = close * (1 + sl_frac)
                        tp = close * (1 - tp_frac)
                    open_pos = {"side": d, "entry": close, "sl": sl, "tp": tp, "peak": close, "i": i, "trail": None, "tags": str(why or "")}
            self.progress.set_id = st.id
        if ind_hist is not None and "indications" in self.packs:
            self._replay_kind_tapes(
                symbol, bars, kind_sigs, ind_hist, now, warmup, time_bars, scratch_bars, honor_tp,
            )

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

        Each kind walks its own entries on every SL ratio vs the configured
        min-step TP so PF / DDT is independent of pack consensus and of a
        single representative SL.
        """
        n = len(bars)
        if n <= warmup:
            return
        base_ts = now - (n - 1) * BAR_S
        sls = list(self.sl_ratios) or [0.6]
        tp_frac = max(0.0020, step_tp_pct(self.min_step_cfg, self.cost_pct))
        for sl in sls:
            sl_frac = max(0.0015, tp_frac * max(0.3, float(sl or 0.6)))
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
                                    "slRatio": sl,
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
        last25 = ordered[-self.deact_n :]
        if last25:
            rs = [signed_result_r(finite(r.get("pnl_pct")), self.cost_pct) for r in last25]
            last25_avg_r = sum(rs) / len(rs)
            last25_avg_pnl = sum(row_net_pnl(r, self.cost_pct) for r in last25) / len(last25)
        else:
            last25_avg_r = 0.0
            last25_avg_pnl = 0.0
        dd = drawdown_time_by_symbol(ordered)
        need = max(self.min_samples, min(self.pf_n, 8))
        n15 = int(last15["count"])
        ratio = float(last15["ratio"])
        validated = n15 >= need and ratio + 1e-9 >= 1.0
        proven_neg = n15 >= need and ratio + 1e-9 < 1.0
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
            "active": (not proven_neg),
            "cost_subtracted": True,
            "cost_pct": self.cost_pct,
            "net_avg": float(last15.get("netAvg") or expectancy),
        }

    def _side_active_flags(self, m: Dict[str, Any], live: Sequence[Dict[str, Any]]) -> Tuple[bool, str]:
        """Per-side deact from that side's tape only. Mixed fills never gate the other side."""
        if not self.auto_deact:
            return True, ""
        live_rows = [r for r in live if isinstance(r, dict)]
        live25 = live_rows[-self.deact_n :]
        live_avg = (
            sum(row_net_pnl(r, self.cost_pct) for r in live25) / len(live25) if live25 else 0.0
        )
        live_tail = live_rows[-max(8, min(self.deact_n, 15)) :]
        live_tail_avg = (
            sum(row_net_pnl(r, self.cost_pct) for r in live_tail) / len(live_tail) if live_tail else 0.0
        )
        need = max(self.min_samples, min(self.pf_n, 8))
        n15 = int(m.get("last15_n") or 0)
        ratio = float(m.get("last15_ratio") or 0)
        if len(live25) >= self.deact_n and live_avg < 0:
            return False, f"live last{len(live25)} avg loss {live_avg:.4f}"
        if len(live_rows) >= 8 and live_tail_avg < 0:
            return False, f"live last{len(live_tail)} avg loss {live_tail_avg:.4f}"
        notes: List[str] = []
        if n15 >= need and ratio + 1e-9 < 1.0:
            notes.append(f"last{n15} PF {ratio:.2f}<1.00 neg")
            return False, "; ".join(notes)
        if n15 >= need and ratio + 1e-9 < self.min_pf:
            notes.append(f"last{n15} PF {ratio:.2f}<{self.min_pf:.2f}")
        return True, "; ".join(notes)

    def _score_one(self, st: SetState) -> None:
        tape = st.tape()
        tape.sort(key=lambda r: finite(r.get("t")))
        m = self._score_metrics(tape, hist_n=len(st.hist))
        st.n = m["n"]
        st.wins = m["wins"]
        st.gp = m["gp"]
        st.gl = m["gl"]
        st.wr = m["wr"]
        st.expectancy = m["expectancy"]
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
        by: Dict[str, Dict[str, Any]] = {}
        for side in DIRECTIONS:
            sub_hist = filter_side(st.hist, side)
            sub_tape = filter_side(tape, side)
            sm = self._score_metrics(sub_tape, hist_n=len(sub_hist))
            sm["side"] = side
            active_s, reason_s = self._side_active_flags(sm, filter_side(st.live, side))
            sm["active"] = active_s
            sm["deact_reason"] = reason_s
            by[side] = sm
        st.by_side = by
        live25 = st.live[-self.deact_n :]
        live_avg = 0.0
        if live25:
            live_avg = sum(row_net_pnl(r, self.cost_pct) for r in live25) / len(live25)
        live_n = len(st.live)
        live_tail = st.live[-max(8, min(self.deact_n, 15)) :]
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
        # Hard deactivation: latest 25 LIVE exchange fills, overall average is a loss.
        if len(live25) >= self.deact_n and live_avg < 0:
            st.active = False
            st.deact_reason = f"live last{len(live25)} avg loss {live_avg:.4f}"
            st.last25_avg_pnl = live_avg
            st.last25_n = len(live25)
            return
        if live_n >= 8 and live_tail_avg < 0:
            st.active = False
            st.deact_reason = f"live last{len(live_tail)} avg loss {live_tail_avg:.4f}"
            st.last25_avg_pnl = live_tail_avg
            return
        notes = []
        need = max(self.min_samples, min(self.pf_n, 8))
        if st.last15_n >= need and st.last15_ratio + 1e-9 < self.min_pf:
            notes.append(f"last{st.last15_n} PF {st.last15_ratio:.2f}<{self.min_pf:.2f}")
        live_dd = False
        if len(live25) >= max(8, self.min_samples) and st.max_dd_s > self.max_dd_s:
            notes.append(f"maxDDt {st.max_dd_s:.0f}s>{self.max_dd_s:.0f}s")
            live_dd = True
        # Hard rule: only positive-PF sets (cost-adjusted last-N PF >= 1.00)
        # stay validated and may be processed. Proven-negative sets deactivate;
        # reactivate=on lets them return once the window rolls non-negative,
        # reactivate=off keeps them off until PF recovers to min_pf.
        proven_neg = st.last15_n >= need and st.last15_ratio + 1e-9 < 1.0
        was_neg_off = (not st.active) and ("<1.00 neg" in st.deact_reason)
        if proven_neg:
            st.active = False
            notes.append(f"last{st.last15_n} PF {st.last15_ratio:.2f}<1.00 neg")
        elif was_neg_off and not self.reactivate and st.last15_ratio + 1e-9 < self.min_pf:
            st.active = False
            notes.append(st.deact_reason)
        elif notes and not self.reactivate:
            # Historic PF below min is a rank penalty; only live DD / live last25 hard-stops.
            if live_dd or (st.last15_n >= self.pf_n and len(live25) >= self.min_samples and st.last15_ratio + 1e-9 < 1.0):
                st.active = False
            else:
                st.active = True
        else:
            st.active = True
        st.deact_reason = "; ".join(dict.fromkeys(notes))

    def _cap_active(self) -> None:
        for kind, cap in (("base", self.max_active), ("trail", max(len(self.trails) * max(1, len(self.packs)), 4))):
            if kind == "base" and cap <= 0:
                continue
            active = [s for s in self.by_idx if s.active and s.kind == kind]
            if len(active) <= cap:
                continue
            active.sort(key=lambda s: (s.last15_ratio, s.last25_avg_r, -s.max_dd_s), reverse=True)
            for extra in active[cap:]:
                extra.active = False
                extra.deact_reason = extra.deact_reason or f"cap>{cap}"

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
        trail_sl_cover = all(
            all(any(abs(st.sl_ratio - sl) < 1e-9 and st.trail_key == t for st in trail_sets) for sl in self.sl_ratios)
            for t in trails
        ) if trails and self.sl_ratios else True
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
            "indexed": True,
            "independentTrail": bool(getattr(self, "trail_enabled", True)),
            "independentDirection": True,
            "independentIndication": True,
            "independentStrategy": True,
            "independentSlTp": True,
            "costSubtracted": True,
            "directions": list(DIRECTIONS),
            "byTrail": by_tr,
            "bySl": by_sl,
            "trailCover": all(any(st.trail_key == t for st in trail_sets) for t in trails) if trails else True,
            "slCover": all(any(abs(st.sl_ratio - sl) < 1e-9 for st in base_sets) for sl in self.sl_ratios),
            "slTpCover": sl_tp_cover,
            "trailSlCover": trail_sl_cover,
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
                "validated": st.last15_n >= max(self.min_samples, 8) and st.last15_ratio + 1e-9 >= 1.0,
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
        need = max(self.min_samples, 8)

        def view(s: SetState) -> Dict[str, Any]:
            return self._side_view(s, want_side if use_side else None)

        def side_on(s: SetState) -> bool:
            if use_side:
                blob = (s.by_side or {}).get(want_side)
                if isinstance(blob, dict) and "active" in blob:
                    return bool(blob.get("active"))
            return s.active

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
            if int(view(s).get("last15_n") or 0) >= need and float(view(s).get("last15_ratio") or 0) + 1e-9 >= self.min_pf
        ]
        if not passing:
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
        def live_ok(s: SetState) -> bool:
            tail = filter_side(s.live, want_side if use_side else None)[-8:]
            if len(tail) < 8:
                return True
            return sum(row_net_pnl(r, self.cost_pct) for r in tail) / len(tail) >= 0.0
        live_pass = [s for s in passing if live_ok(s)]
        chosen = live_pass or passing
        chosen.sort(key=lambda s: (
            float(view(s).get("last15_ratio") or 0),
            1 if (s.kind != "base" or int(s.step or 0) >= int(self.min_step or 0)) else 0,
            float(view(s).get("last25_avg_r") or 0),
            -float(view(s).get("max_dd_s") or 0),
            int(view(s).get("n") or 0),
        ), reverse=True)
        return chosen[0]

    def pick_trail(self, pack: str, side: Optional[str] = None) -> Optional[SetState]:
        return self.pick(pack, kind="trail", side=side)

    def pick_any(self, pack: str, side: Optional[str] = None) -> Optional[SetState]:
        return self.pick(pack, "base", side=side) or self.pick(pack, "trail", side=side)

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
        """Cost-adjusted PF evidence for one indication kind (hist + live), optional side."""
        tape = list(self.ind_hist.get(kind) or []) + list(self.ind_live.get(kind) or [])
        tape = filter_side(tape, side)
        tape.sort(key=lambda r: finite(r.get("t")))
        dd = drawdown_time_by_symbol(tape) if tape else {"maxS": 0.0, "avgS": 0.0, "episodes": 0}
        if tape:
            last = last_n_cost_pf(last_n_balanced(tape, self.pf_n), self.pf_n, self.cost_pct)
            n = int(last["count"])
            pf = float(last["ratio"])
            net_avg = float(last.get("netAvg") or 0)
        else:
            n, pf, net_avg = 0, 0.0, 0.0
        need = max(self.min_samples, 8)
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
            "netAvg": round(net_avg, 6),
            "costSubtracted": True,
            "validated": n >= need,
            "profitable": pf + 1e-9 >= 1.0,
            "maxDdS": round(float(dd.get("maxS") or 0), 1),
            "avgDdS": round(float(dd.get("avgS") or 0), 1),
            "ddEpisodes": int(dd.get("episodes") or 0),
            "bySide": by_side,
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

    def snapshot(self) -> Dict[str, Any]:
        rows = []
        for st in sorted(self.sets.values(), key=lambda s: (not s.active, -s.last15_ratio, s.max_dd_s)):
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
                    "gp": st.gp,
                    "gl": st.gl,
                    "costSubtracted": True,
                    "netAvg": round(st.expectancy, 6),
                    "bySide": {
                        d: {
                            "n": int(v.get("n") or 0),
                            "pf": round(float(v.get("last15_ratio") or 0), 4),
                            "last15N": int(v.get("last15_n") or 0),
                            "last15R": round(float(v.get("last15_r") or 0), 4),
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
                "on": int(st.active),
                "pf": round(st.last15_ratio, 4),
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
            "autoDeact": self.auto_deact,
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
            "coverage": cover,
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
    out.append(("set-adapt-keeps-grid", sorted({s.step for s in book.sets.values() if s.kind == "base"}) == list(range(3, 7)), f"steps={sorted({s.step for s in book.sets.values() if s.kind == 'base'})}"))
    sid = next(iter(book.sets))
    st = book.sets[sid]
    st.hist = [{"t": 1000 + i, "pnl": -0.01, "pnl_pct": -0.003, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "sl"} for i in range(25)]
    st.live = []
    book._score_one(st)
    out.append(("set-hist-neg-off", (not st.active) and "neg" in st.deact_reason, f"{st.active} {st.deact_reason}"))
    st.live = [{"t": 2000 + i, "pnl": -0.02, "pnl_pct": -0.004, "symbol": "T", "side": "LONG", "hold_s": 40, "reason": "sl"} for i in range(25)]
    book._score_one(st)
    out.append(("set-deact-live-25", (not st.active) and "live last" in st.deact_reason and "loss" in st.deact_reason, f"{st.active} {st.deact_reason} {st.last25_avg_pnl}"))
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
    tight = [s for s in book3.sets.values() if abs(s.sl_ratio - 0.3) < 1e-9]
    wide = [s for s in book3.sets.values() if abs(s.sl_ratio - 1.5) < 1e-9]
    lo_step = [s for s in book3.sets.values() if s.step == 3]
    hi_step = [s for s in book3.sets.values() if s.step == 12]
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
    want_tr = len(book4.packs) * len(book4.sl_ratios) * max(1, len(book4.trails))
    want = want_base + want_tr
    idxs = [s.idx for s in book4.by_idx]
    trails_in = {s.trail_key for s in book4.by_idx if s.kind == "trail"}
    kinds = {s.kind for s in book4.by_idx}
    out.append(("set-grid-product", len(book4.by_idx) == want and want_base >= 50 and want_tr >= 10, f"n={len(book4.by_idx)} base={want_base} trail={want_tr} dims={cov.get('dims')} fam={cov.get('families')}"))
    out.append(("set-idx-unique", idxs == list(range(len(idxs))), f"n={len(idxs)} last={idxs[-1] if idxs else None}"))
    out.append(("set-trail-cover", cov.get("trailCover") and len(trails_in) >= 5 and "trail" in kinds, f"trails={sorted(trails_in)} cover={cov.get('trailCover')}"))
    out.append(("set-sl-cover", cov.get("slCover") and len(book4.sl_ratios) >= 5, f"sl={book4.sl_ratios}"))
    out.append(("set-sl-tp-cover", bool(cov.get("slTpCover")), f"slTp={cov.get('slTpCover')} steps={cov.get('steps')} bySl={ {k: v.get('steps') for k, v in (cov.get('bySl') or {}).items()} }"))
    out.append(("set-trail-sl-cover", bool(cov.get("trailSlCover")), f"trailSl={cov.get('trailSlCover')} byTrail={ {k: v.get('sl') for k, v in (cov.get('byTrail') or {}).items()} }"))
    out.append(("set-independent-sl-tp", bool(cov.get("independentSlTp")), str(cov.get("independentSlTp"))))
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
    out.append(("set-trail-own-family", base_n >= 1 and all(s.step == 0 for s in book5.by_idx if s.kind == "trail"), f"base={base_n} trails={len(by_tr)}"))
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
            "stratIndications": False, "stratGeneral": True, "slToTpRatios": [0.6, 0.9],
            "trailArmMin": 0.3, "trailArmMax": 0.3,
        }
    )
    g2.progress.ready = True
    bases = [x for x in g2.by_idx if x.kind == "base"]
    neg_set, pos_set = bases[0], bases[1]
    neg_rows = [{"t": 6000 + i * 60, "pnl": -0.0045, "pnl_pct": -0.003, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "sl"} for i in range(15)]
    pos_rows = [{"t": 6000 + i * 60, "pnl": 0.0015, "pnl_pct": 0.003, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "tp"} for i in range(15)]
    neg_set.hist = list(neg_rows)
    pos_set.hist = list(pos_rows)
    g2._score_one(neg_set)
    g2._score_one(pos_set)
    out.append(("set-neg-off", (not neg_set.active) and "neg" in neg_set.deact_reason, f"{neg_set.active} {neg_set.deact_reason} pf={neg_set.last15_ratio}"))
    out.append(("set-pos-on", pos_set.active and pos_set.last15_ratio >= 1.0, f"{pos_set.active} pf={pos_set.last15_ratio}"))
    pk = g2.pick("general")
    out.append(("set-pick-pos-only", pk is not None and pk.id == pos_set.id, f"{getattr(pk, 'id', None)}"))
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
    # sticky off when reactivate is disabled (until PF recovers to min_pf)
    g2.reactivate = False
    neg_set.hist = list(neg_rows)
    g2._score_one(neg_set)
    off1 = not neg_set.active
    neg_set.hist = list(pos_rows)  # ratio 1.10 < min_pf 1.20 -> stays off
    g2._score_one(neg_set)
    out.append(("set-neg-sticky", off1 and not neg_set.active, f"{neg_set.active} {neg_set.deact_reason} pf={neg_set.last15_ratio}"))
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
    gb[0].hist = [{"t": 7000 + i * 60, "pnl": 0.0015, "pnl_pct": 0.003, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "tp"} for i in range(15)]
    for x in gb[1:]:
        x.hist = [{"t": 7000 + i * 60, "pnl": -0.0045, "pnl_pct": -0.003, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "sl"} for i in range(15)]
    for x in g4.by_idx:
        g4._score_one(x)
    pk4 = g4.pick("general")
    out.append(("set-strict-pick-validated", pk4 is not None and pk4.id == gb[0].id and pk4.last15_ratio >= 1.0 and pk4.last15_n >= 8, f"{getattr(pk4, 'id', None)} pf={getattr(pk4, 'last15_ratio', 0)}"))
    out.append(("set-strict-pack-open-winner", g4.pack_open("general") and not g4.pack_open("indications"), f"gen={g4.pack_open('general')} ind={g4.pack_open('indications')}"))
    # strict: once the only winner turns cold (no samples), pack closes again
    gb[0].hist = []
    g4._score_one(gb[0])
    out.append(("set-strict-cold-closes", g4.pick("general") is None and not g4.pack_open("general"), f"pick={g4.pick('general')}"))
    # indication-kind gate: proven loser off, validated winner on, unproven
    # kind rides the pack, pack closed -> everything off
    gb[0].hist = [{"t": 8000 + i * 60, "pnl": 0.0015, "pnl_pct": 0.003, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "tp"} for i in range(15)]
    g4._score_one(gb[0])
    ib = [x for x in g4.by_idx if x.pack == "indications" and x.kind == "base"]
    ib[0].hist = [{"t": 8000 + i * 60, "pnl": 0.0015, "pnl_pct": 0.003, "symbol": "T", "side": "LONG", "hold_s": 60, "reason": "tp"} for i in range(15)]
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
    out.append(("set-dir-short-off", sblob.get("active") is False and float(sblob.get("last15_ratio") or 0) < 1.0, str(sblob)))
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
    return out


if __name__ == "__main__":
    failed = 0
    for name, ok, detail in self_test():
        print(("PASS" if ok else "FAIL"), name, detail)
        failed += int(not ok)
    if failed:
        raise SystemExit(1)
    print("set_engine ok")
