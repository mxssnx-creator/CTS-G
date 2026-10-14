"""Exact Axis child evidence from chronological parent closes and price bars.

Each child keeps its parent indication/config/symbol/direction. Axis windows
filter the parent's original signal; child positions then run the same native
TP/SL/trailing/time/price-tactic rules and are scored from their own closes.
Identical entry masks share one price replay, never a parent's PF or tape.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple


def axis_ranges(book: Any) -> List[Tuple[str, int]]:
    """Canonical finest count grid; optional axis_settings bounds an offline run."""
    from coord_engine import AXIS_SPECS
    configured = getattr(book, "axis_settings", {}) or {}
    out = []
    for axis, spec in AXIS_SPECS.items():
        cfg = configured.get(axis, {}) if isinstance(configured, dict) else {}
        if isinstance(cfg, dict) and cfg.get("enabled") is False:
            continue
        maximum = min(int(spec["max"]), int(cfg.get("maxWindow", spec["max"])) if isinstance(cfg, dict) else int(spec["max"]))
        out.extend((axis, count) for count in range(int(spec["min"]), maximum + 1, int(spec["step"])))
    return out


def replay_parent_closes(book: Any, symbol: str, bars: Sequence, parent: Any,
                         lane: Sequence, warmup: int, now: float, *,
                         lane_exits: Optional[Dict[int, Tuple[str, float]]] = None,
                         side: Optional[str] = None) -> List[Any]:
    """Full, untrimmed native scalar closes; no future or open-position evidence."""
    from indication_engine import tactic_should_close
    from set_engine import BAR_S, hist_fill
    n = len(bars)
    if n < 2:
        return []
    base_ts = float((getattr(book, "_bar_end_ts", {}) or {}).get(symbol) or now) - (n - 1) * BAR_S
    time_bars = max(8, min(int(book.hist_time_bars), max(8, n - warmup - 1)))
    scratch_bars = max(8, int(book.scratch_s / BAR_S))
    trailing = bool(parent.kind == "trail" or parent.trail_key)
    arm, give = float(parent.trail_arm or 0), float(parent.trail_give or 0)
    if arm > 0.05:
        arm /= 100
    if give > 0.05:
        give /= 100
    native_pack = parent.pack == "general" or parent.indication_kind in ("general", "combined")
    floor = 0.58 if native_pack or not parent.indication_config else 0.52
    sides = (1,) if side == "LONG" else (-1,) if side == "SHORT" else (1, -1)
    result = []
    with book.sl_scope(symbol):
        sl, tp = book.pair_sl_tp(float(parent.tp_pct), float(parent.sl_ratio or 0.6))
        for direction in sides:
            pos, cooldown = None, 0
            for i in range(max(0, warmup), min(n, len(lane))):
                bar, ts = bars[i], base_ts + i * BAR_S
                if pos is None:
                    cooldown = max(0, cooldown - 1)
                else:
                    entry = float(pos["entry"])
                    held, entry_i = i - int(pos["i"]), int(pos["i"])
                    # Defer native time/scratch exits until after the price
                    # tactic, matching the columnar engine's exit priority.
                    pos, closed = book._advance_pos(pos, bar, i, sl, tp, trailing, arm, give,
                        10 ** 9, 10 ** 9, bool(book.hist_honor_tp), ts, symbol,
                        parent.id, parent.pack, "core")
                    if closed is None:
                        why = ""
                        tactic, level = (lane_exits or {}).get(entry_i, ("", 0.0))
                        enabled = bool(book.ind_settings.get("exitTacticOn", True))
                        if enabled and held >= 1 and tactic_should_close(tactic, direction,
                                float(bar[3]), level, entry,
                                float(book.ind_settings.get("exitTacticBufferPct", 0.1)),
                                float(book.ind_settings.get("exitTacticMinGainPct", 0.15))):
                            why = "tactic-" + tactic
                        elif held >= time_bars:
                            why = "time"
                        elif held >= scratch_bars and (float(bar[3]) - entry) / entry * direction >= book.scratch_min:
                            why = "scratch+"
                        if why:
                            closed = hist_fill(ts, symbol, direction,
                                (float(bar[3]) - entry) / entry * direction, held * BAR_S, why)
                            pos = None
                    if closed is not None:
                        result.append(closed)
                        cooldown = max(0, int(book.cooldown_bars or 0))
                signal = lane[i]
                d, confidence = int(signal[0]), float(signal[1])
                if pos is None and cooldown <= 0 and d == direction and confidence >= floor:
                    close = float(bar[3])
                    why = str(signal[2]) if len(signal) >= 3 else parent.indication_config
                    pos = book._seed_pos(direction, close, 0.0, 0.0, i, why)
                    book._rearm_stops(pos, sl, tp)
    result.sort(key=lambda row: float(row["t"]))
    return result


def _axis_accept(axis: str, count: int, closed: Sequence, cost_pct: float, floor: float) -> bool:
    """The Coordinator.axis_variants closed-only window contract."""
    from position_cost import clears_pf, last_n_cost_pf, row_net_pnl
    tape = closed[-2 * count:-count] if axis == "prev" and len(closed) >= 2 * count else ([] if axis == "prev" else closed[-count:])
    if len(tape) < min(3, count):
        return False
    if axis == "pause" and len(tape) >= count and all(row_net_pnl(row, cost_pct) < 0 for row in tape[-count:]):
        return False
    return clears_pf(last_n_cost_pf(tape, count, cost_pct, ordered=True, simple=True)["ratio"], floor)


def calculate_axes(book: Any, symbol: str, bars: Sequence, parent_states: Sequence,
                   lane: Sequence, warmup: int, now: float, *,
                   lane_exits: Optional[Dict[int, Tuple[str, float]]] = None,
                   capture: bool = False) -> List[Any]:
    """Build each parent's own axis children, with actual independent trade tapes.

    All configured children remain present when evidence rejects them. Parent
    Base and axis-window gates use only closes known before the entry close.
    Runtime publication/axis enablement is the caller's responsibility.
    """
    from contracts import VOLUME_RATIO_UNIT, stable_key
    from indication_adjustment_grid import capture_parent_events
    from position_cost import clears_pf, last_n_cost_pf
    from set_engine import BAR_S, REPLAY_HIST_CAP, recent_direction_rows
    variants = axis_ranges(book)
    n = len(bars)
    if not variants or not n or len(lane) != n:
        return []
    base_ts = float((getattr(book, "_bar_end_ts", {}) or {}).get(symbol) or now) - (n - 1) * BAR_S
    floor = float(book.stage_min_pf.get("base", book.min_pf))
    need = int(book.eval_need())
    window = int(book.pf_n)
    out = []
    parents = [parent for parent in parent_states if parent.kind in ("base", "trail")
               and (not parent.indication_symbol or parent.indication_symbol == symbol)]
    captures = capture_parent_events(book, symbol, bars, parents, lane, warmup, now, lane_exits=lane_exits)
    for parent in parents:
        for side, direction in (("LONG", 1), ("SHORT", -1)):
            parent_closes = [row for row in captures[parent.id] if row["side"] == side]
            positions = {axis_count: [False] * n for axis_count in variants}
            closed, cursor, qualified = [], 0, False
            accepted = {axis_count: False for axis_count in variants}
            for i in range(max(0, warmup), n):
                ts = base_ts + i * BAR_S
                changed = False
                while cursor < len(parent_closes) and float(parent_closes[cursor]["t"]) < ts:
                    closed.append(parent_closes[cursor])
                    cursor += 1
                    changed = True
                if changed:
                    metric = last_n_cost_pf(closed, window, book.cost_pct, ordered=True, simple=True)
                    dd = book.ddt_when_enough(closed, need, ordered=True)
                    qualified = int(metric["count"]) >= need and clears_pf(metric["ratio"], floor) and float(dd["maxS"]) <= book.max_dd_s
                    accepted = {key: qualified and _axis_accept(*key, closed, book.cost_pct, floor) for key in variants}
                if lane[i][0] == direction:
                    for key, yes in accepted.items():
                        positions[key][i] = bool(yes)
            # Sibling counts often have identical masks. Share actual price
            # transitions for those mathematically identical trades only.
            groups: Dict[Tuple[bool, ...], List[Any]] = {}
            for axis, count in variants:
                child = parent.catalog_copy()
                child.id = "axis:" + stable_key(parent.id, symbol, side, axis, count)
                child.kind, child.parent_set_id = "axis", parent.id
                child.template_set_id = parent.template_set_id or parent.id
                child.axis_key, child.relative_count = f"{axis}:{count}", count
                child.volume_ratio = round(count * VOLUME_RATIO_UNIT, 6)
                child.indication_symbol = symbol
                child.locked = bool(parent.locked)
                groups.setdefault(tuple(positions[(axis, count)]), []).append(child)
            for mask, children in groups.items():
                filtered = [(int(lane[i][0]), float(lane[i][1]), str(lane[i][2]) if len(lane[i]) > 2 else parent.indication_config)
                            if mask[i] else (0, 0.0, "") for i in range(n)]
                actual = replay_parent_closes(book, symbol, bars, parent, filtered, warmup, now,
                    lane_exits=lane_exits, side=side) if any(mask) else []
                retained = actual if capture else recent_direction_rows(actual, REPLAY_HIST_CAP)
                current_base = bool(book._base_metrics_ok((parent.by_side or {}).get(side) or {}))
                for child in children:
                    child.hist = []
                    for fill in retained:
                        row = dict(fill)
                        row.update(set_id=child.id, parent_set_id=parent.id, strategy="axis",
                            axis_key=child.axis_key, relative_count=child.relative_count,
                            volume_ratio=child.volume_ratio, ind_kind=child.indication_kind,
                            ind_config=child.indication_config)
                        child.hist.append(row)
                    book._score_pair((child, None))
                    child.n = len(actual)
                    child.stage_ledger.update(parentBaseQualified=current_base, parentDirection=side,
                        closedOnly=True, openExcluded=True, actualReplay=True)
                    child.strategy_adjustments["axis"] = {"axisKey": child.axis_key,
                        "relativeCount": child.relative_count, "volumeRatio": child.volume_ratio,
                        "parentSetId": parent.id, "parentDirection": side}
                    if not current_base:
                        child.active = False
                        child.stage_ledger["main"] = child.stage_ledger["real"] = False
                        child.stage_ledger["parentQualificationReason"] = "own parent direction has not passed Base"
                        for stage in ("Main", "Real"):
                            record = (child.stage_ledger.get("records") or {}).get(stage)
                            if isinstance(record, dict):
                                record.update(evaluated=False, qualified=False,
                                    reason="own parent direction has not passed Base")
                            adjustment = child.strategy_adjustments.get(stage.lower())
                            if isinstance(adjustment, dict):
                                adjustment.update(evaluated=False, qualified=False)
                        for metric in child.by_side.values():
                            metric["active"] = False
                        child.stage = child.stage_qualified = "Base" if child.stage_ledger.get("base") else "Unqualified"
                    out.append(child)
    return out
