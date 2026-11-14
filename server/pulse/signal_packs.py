"""Signals as Set-engine packs (owner 2026-11-14: "Signals ... for each source, symbol, type, range independent ... own coordination through the stages").

Every (source, range, timeframe) of `signal_sources` (the CTS-A-O sources ported in, see that module) is its own pack ``sig-<source>-<range>-t<tf>``:
it builds its own Sets (SL ratio x TP step, Exit Sets, trailing pairs, both directions) and every Set runs the same stage chain, last-N windows, lanes,
Auto floor and execution check as any other pack. ``set_engine.is_signal_pack`` already gives these packs the Signals floor (``minPfSignals``).

The signal of a pack on 1m bar i: the 1m bars up to and including i are aggregated into completed UTC-aligned timeframe bars
(``signal_sources.completed_bars``); a signal exists only on the 1m bar that completes a timeframe bar, and only when the source's state changed on that
bar to a non-zero side (the onset ``signal_sources.onsets`` reads). Bars after i are never read.

``signalConfigs`` entries: {"sources", "ranges", "timeframes", "stepMin", "stepMax", "slRatios", "types", "trailStepMin", "trailStepMax", "enabled", "label"};
each entry gives its packs their own TP-step range, SL ratios and Set types (the same geometry ``indicationConfigs`` uses).
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from signal_sources import RANGES, SOURCES, TIMEFRAMES, completed_bars, states, warmup_bars

PREFIX = "sig-"
KINDS = ("base", "exit", "trail")
MAX_ENTRIES = 32
MAX_PACKS = 72                 # 9 sources x 2 ranges x 4 timeframes
WINDOW_CAP = 1920              # 1m rows a Signals pack reads at most (the Set history's default lookback)
CONF = 0.6


def pack_name(source: str, range_name: str, tf: int) -> str:
    return f"{PREFIX}{source}-{range_name}-t{int(tf)}"


def parse(pack: str) -> Optional[Tuple[str, str, int]]:
    """(source, range, timeframe) of a Signals pack name, None for any other name."""
    if not isinstance(pack, str) or not pack.startswith(PREFIX):
        return None
    body = pack[len(PREFIX):]
    head, sep, tf = body.rpartition("-t")
    if not sep or not tf.isdigit() or int(tf) not in TIMEFRAMES:
        return None
    for rng in RANGES:
        suffix = "-" + rng
        if head.endswith(suffix) and head[: -len(suffix)] in SOURCES:
            return head[: -len(suffix)], rng, int(tf)
    return None


def window_minutes(pack: str) -> int:
    """1m rows the pack reads for one bar: six warm-ups of completed timeframe bars plus two, at most WINDOW_CAP."""
    p = parse(pack)
    if p is None:
        return 0
    source, rng, tf = p
    return int(min(WINDOW_CAP, (6 * warmup_bars(source, rng) + 2) * tf))


def signal(bars: Sequence[Sequence[float]], i: int, close_ts: float, pack: str) -> Tuple[int, float, str]:
    """The pack's signal on 1m bar i (rows [o, h, l, c, v]); close_ts = the close time of bar i in seconds."""
    p = parse(pack)
    if p is None or i < 1:
        return (0, 0.0, "")
    source, rng, tf = p
    if tf > 1 and int(round(close_ts)) % (tf * 60) != 0:
        return (0, 0.0, "")                                  # bar i does not complete a timeframe bar
    w = window_minutes(pack)
    lo = max(0, i + 1 - w)
    open0 = close_ts - 60.0 - (i - lo) * 60.0
    rows = [(int(round((open0 + 60.0 * k) * 1000.0)), float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
            for k, r in enumerate(bars[lo:i + 1])]
    try:
        agg = completed_bars(rows, tf) if tf > 1 else [list(r) for r in rows]
    except ValueError:
        return (0, 0.0, "")
    if len(agg) < 2 or agg[-1][0] + tf * 60000 != int(round(close_ts * 1000.0)):
        return (0, 0.0, "")                                  # the newest completed timeframe bar must end at bar i
    try:
        st = states([b[4] for b in agg], source, rng, [b[2] for b in agg], [b[3] for b in agg], [b[5] for b in agg])
    except ValueError:
        return (0, 0.0, "")
    if st[-1] and st[-1] != st[-2]:
        return (int(st[-1]), CONF, f"{source}:{rng}:t{tf}")
    return (0, 0.0, "")


def _words(raw: Any) -> List[str]:
    if isinstance(raw, str):
        raw = raw.replace(";", ",").split(",")
    return [str(x).strip().lower() for x in (raw or []) if str(x).strip()]


def _ints(raw: Any) -> List[int]:
    out: List[int] = []
    for x in (raw if isinstance(raw, (list, tuple)) else _words(raw)):
        try:
            v = int(float(x))
        except (TypeError, ValueError):
            continue
        if v not in out:
            out.append(v)
    return out


def expand(configs: Any, step_lo: int, step_hi: int, snap_ratio: Callable[[float], float], default_ratios: Sequence[float]) -> Dict[str, Any]:
    """Expand ``signalConfigs`` into {"packs", "ranges", "dropped", "entries"}: packs in a stable order; ranges = pack -> geometry
    ({"lo", "hi", "ratios", "kinds", "trailLo", "trailHi", "source"}); an unknown source / range / timeframe is dropped and listed; a pack named by two
    entries keeps the first entry's geometry (one pack, one Set grid)."""
    packs: Dict[str, None] = {}
    ranges: Dict[str, Dict[str, Any]] = {}
    dropped: List[Dict[str, str]] = []
    entries: List[Dict[str, Any]] = []
    if not isinstance(configs, (list, tuple)):
        return {"packs": [], "ranges": {}, "dropped": [], "entries": []}
    for e_i, raw in enumerate(list(configs)[:MAX_ENTRIES]):
        if not isinstance(raw, dict) or raw.get("enabled", True) is False:
            continue
        label = str(raw.get("label") or f"signals {e_i + 1}")[:60]
        srcs = _words(raw.get("sources")) or list(SOURCES)
        rngs = _words(raw.get("ranges")) or list(RANGES)
        tfs = _ints(raw.get("timeframes")) or list(TIMEFRAMES)
        lo = max(step_lo, min(step_hi, int(raw.get("stepMin") or step_lo)))
        hi = max(lo, min(step_hi, int(raw.get("stepMax") or step_hi)))
        ratios: List[float] = []
        for x in raw.get("slRatios") or default_ratios:
            try:
                v = snap_ratio(float(x))
            except Exception:
                continue
            if math.isfinite(v) and v not in ratios:
                ratios.append(v)
        kinds = [k for k in KINDS if k in (_words(raw.get("types")) or list(KINDS))] or list(KINDS)
        t_lo = _ints([raw.get("trailStepMin")]) if raw.get("trailStepMin") not in (None, "") else []
        t_hi = _ints([raw.get("trailStepMax")]) if raw.get("trailStepMax") not in (None, "") else []
        trail_lo = max(2, min(50, t_lo[0])) if t_lo else 0
        trail_hi = max(trail_lo, min(50, t_hi[0])) if t_hi else 0
        made = 0
        for s in srcs:
            for r in rngs:
                for tf in tfs:
                    if s not in SOURCES or r not in RANGES or tf not in TIMEFRAMES:
                        dropped.append({"entry": label, "name": f"{s}/{r}/t{tf}", "reason": "unknown source, range or timeframe"})
                        continue
                    name = pack_name(s, r, tf)
                    if name in packs:
                        continue
                    if len(packs) >= MAX_PACKS:
                        dropped.append({"entry": label, "name": name, "reason": "over the bound"})
                        continue
                    packs[name] = None
                    ranges[name] = {"lo": lo, "hi": hi, "ratios": sorted(set(ratios)) or list(default_ratios), "kinds": kinds,
                                    "trailLo": trail_lo, "trailHi": trail_hi, "source": f"{s}:{r}:t{tf}", "entry": e_i, "signals": True}
                    made += 1
        entries.append({"label": label, "packs": made, "stepMin": lo, "stepMax": hi, "ratios": len(ratios), "types": kinds,
                        "trailStepMin": trail_lo or None, "trailStepMax": trail_hi or None})
    return {"packs": list(packs), "ranges": ranges, "dropped": dropped, "entries": entries}
