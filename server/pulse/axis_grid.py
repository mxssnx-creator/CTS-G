"""Exact Axis child evidence from chronological parent closes and price bars.

Each child keeps its parent indication/config/symbol/direction. Axis windows
filter the parent's original signal; child positions then run the same native
TP/SL/trailing/time/price-tactic rules and are scored from their own closes.
Identical entry masks share one price replay, never a parent's PF or tape.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple
from collections.abc import Sequence as SequenceABC


class AxisStates(list):
    """List-compatible children; capture-mode open marks stay outside PF."""
    def __init__(self):
        super().__init__()
        self.open = []


def _axis_metadata(row, child):
    row = dict(row)
    row.update(set_id=child.id, parent_set_id=child.parent_set_id, strategy="axis",
               axis_key=child.axis_key, relative_count=child.relative_count,
               volume_ratio=child.volume_ratio, ind_kind=child.indication_kind,
               ind_config=child.indication_config)
    return row


class AxisTape(SequenceABC):
    """Shared immutable execution rows with each child's independent identity."""
    __slots__ = ("rows", "state")

    def __init__(self, rows, state):
        self.rows, self.state = rows, state

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        return _axis_metadata(self.rows[index], self.state)

    def __eq__(self, other):
        return isinstance(other, SequenceABC) and len(self) == len(other) and all(a == b for a, b in zip(self, other))


def _gate_masks(book, rows, variants, lane, warmup, base_ts, direction):
    """Closed-only prefix arithmetic; never recompute 29 rolling row windows."""
    from set_engine import BAR_S, _np
    from position_cost import clears_pf, ratio_from_r, signed_result_r
    np = _np
    n, k = len(lane), len(rows)
    times = np.asarray([float(r["t"]) for r in rows])
    values = np.asarray([signed_result_r(float(r.get("pnl_pct") or 0), book.cost_pct) for r in rows])
    prefix = np.r_[0., np.cumsum(values)]
    negative = np.r_[0, np.cumsum(values < 0)]
    count = np.searchsorted(times, base_ts + np.arange(n) * BAR_S, side="left")
    samples = np.arange(k + 1)
    need, window = book.eval_need(), book.pf_n
    qualified = np.zeros(k + 1, dtype=bool)
    floor = float(book.stage_min_pf.get("base", book.min_pf))
    for size in range(need, k + 1):
        width = min(size, window)
        ratio = ratio_from_r((prefix[size] - prefix[size-width]) / width)
        if not clears_pf(ratio, floor):
            continue
        first = max(0, size-book.ddt_window())
        # Any episode lies inside the observed window; this bound is exact
        # regardless of position volume and avoids unnecessary DDT rescans.
        dd_ok = not size or times[size-1]-times[first] <= book.max_dd_s
        if not dd_ok:
            dd_ok = float(book.ddt_when_enough(rows[:size], need, ordered=True)["maxS"]) <= book.max_dd_s
        qualified[size] = dd_ok
    signal = np.asarray([int(s[0]) == direction for s in lane], dtype=bool)
    signal[:warmup] = False
    masks = {}
    for axis, width in variants:
        if axis == "prev":
            stop = np.maximum(0, samples-width)
            start = np.maximum(0, samples-2*width)
            available = np.where(samples >= 2*width, width, 0)
        else:
            stop, start = samples, np.maximum(0, samples-width)
            available = np.minimum(samples, width)
        mean = (prefix[stop]-prefix[start]) / np.maximum(available, 1)
        ratios = [ratio_from_r(float(v)) for v in mean]
        accepted = qualified & (available >= min(3, width)) & np.asarray([clears_pf(v, floor) for v in ratios])
        if axis == "pause":
            accepted &= ~((samples >= width) & (negative[stop]-negative[start] == width))
        masks[(axis, width)] = accepted[count] & signal
    return masks


def _replay_mask_groups(book, symbol, bars, groups, lane, warmup, now, lane_exits, capture):
    """512 independent masked price lanes at a time, using native protection."""
    from set_engine import BAR_S, REPLAY_HIST_CAP, _np, hist_fill
    from indication_adjustment_grid import _Positions
    np = _np
    base_ts = float((getattr(book, "_bar_end_ts", {}) or {}).get(symbol) or now)-(len(bars)-1)*BAR_S
    time_bars = max(8, min(book.hist_time_bars, max(8, len(bars)-warmup-1)))
    scratch_bars = max(8, int(book.scratch_s/BAR_S))
    for direction in (1, -1):
        pending = [g for g in groups.values() if g["direction"] == direction]
        for start in range(0, len(pending), 512):
            tile = pending[start:start+512]
            params = np.asarray([g["params"][:2] for g in tile])
            trailing = np.asarray([g["params"][2] for g in tile], dtype=bool)
            arms = np.asarray([g["params"][3] for g in tile])
            gives = np.asarray([g["params"][4] for g in tile])
            masks = np.vstack([g["mask"] for g in tile])
            state = _Positions(np, params[:,0], params[:,1], arms, gives, trailing)
            rows, counts = [[] for g in tile], np.zeros(len(tile), dtype=int)
            parent = tile[0]["parent"]
            native = parent.pack == "general" or parent.indication_kind in ("general", "combined")
            floor = .58 if native or not parent.indication_config else .52
            for i in range(warmup, len(bars)):
                high, low, close = (float(bars[i][j]) for j in (1,2,3))
                if min(high, low, close) <= 0:
                    continue
                state.cooldown[~state.active & (state.cooldown > 0)] -= 1
                dead, px, reasons = state.advance(high, low, close, i, direction, book,
                    time_bars, scratch_bars, bool(book.hist_honor_tp))
                for j in np.flatnonzero(dead):
                    rows[j].append(hist_fill(base_ts+i*BAR_S, symbol, direction,
                        float((px[j]-state.entry[j])/state.entry[j]*direction),
                        float((i-state.opened[j])*BAR_S), str(reasons[j])))
                    counts[j] += 1
                    if not capture and len(rows[j]) > REPLAY_HIST_CAP:
                        del rows[j][0]
                if lane[i][0] == direction and lane[i][1] >= floor:
                    seed = ~state.active & (state.cooldown <= 0) & masks[:,i]
                    state.seed(seed, close, i, direction, (lane_exits or {}).get(i, ("",0.)))
            for j, group in enumerate(tile):
                group["rows"], group["count"] = tuple(rows[j]), int(counts[j])
                if capture and state.active[j]:
                    opened, entry = int(state.opened[j]), float(state.entry[j])
                    terminal = float(bars[-1][3])
                    group["open"] = dict(t=base_ts+(len(bars)-1)*BAR_S, symbol=symbol,
                        side="LONG" if direction > 0 else "SHORT", reason="open",
                        pnl_pct=(terminal-entry)/entry*direction,
                        hold_s=(len(bars)-1-opened)*BAR_S,
                        _entry=opened, _parent_entry=opened, _exit=len(bars)-1,
                        _avg=entry, _entry_px=entry, _exit_px=terminal, _qty=1., _parent=1.,
                        _bundle_avg=entry, _bundle_qty=1., _log=(), _bundle_log=(), _adds=0,
                        entry_ts=base_ts+opened*BAR_S,
                        stop_loss=float(state.sl[j]), take_profit=float(state.tp[j]),
                        trail_stop=float(state.trail[j]), peak=float(state.peak[j]),
                        exit_tactic=str(state.tactic[j]), exit_level=float(state.level[j]),
                        confirmed_close=False, open_position=True)


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
                   capture: bool = False,
                   parent_captures: Optional[Dict[str, Sequence]] = None) -> List[Any]:
    """Build each parent's own axis children, with actual independent trade tapes.

    All configured children remain present when evidence rejects them. Parent
    Base and axis-window gates use only closes known before the entry close.
    Runtime publication/axis enablement is the caller's responsibility.
    """
    from contracts import VOLUME_RATIO_UNIT, stable_key
    from indication_adjustment_grid import capture_parent_events
    from set_engine import BAR_S, _np
    variants = axis_ranges(book)
    n = len(bars)
    out = AxisStates()
    if not variants or not n or len(lane) != n:
        return out
    if _np is None:
        raise RuntimeError("independent axes require NumPy replay")
    base_ts = float((getattr(book, "_bar_end_ts", {}) or {}).get(symbol) or now) - (n - 1) * BAR_S
    parents = [parent for parent in parent_states if parent.kind in ("base", "trail")
               and (not parent.indication_symbol or parent.indication_symbol == symbol)]
    if len({(p.indication_kind, p.indication_config) for p in parents}) > 1:
        raise ValueError("one signal lane can only capture one indication kind/configuration")
    captures = dict(parent_captures) if parent_captures is not None else {}
    missing = [p for p in parents if p.id not in captures]
    if missing:
        captures.update(capture_parent_events(book, symbol, bars, missing, lane, warmup, now, lane_exits=lane_exits))
    groups, records = {}, []
    for parent in parents:
        with book.sl_scope(symbol):
            sl, tp = book.pair_sl_tp(float(parent.tp_pct), float(parent.sl_ratio or .6))
        arm = parent.trail_arm/100 if parent.trail_arm > .05 else parent.trail_arm
        give = parent.trail_give/100 if parent.trail_give > .05 else parent.trail_give
        params = (sl, tp, bool(parent.kind == "trail" or parent.trail_key), arm, give)
        for side, direction in (("LONG", 1), ("SHORT", -1)):
            rows = [row for row in captures[parent.id] if row["side"] == side]
            masks = _gate_masks(book, rows, variants, lane, warmup, base_ts, direction)
            for axis, count in variants:
                child = parent.catalog_copy()
                child.id = "axis:" + stable_key(parent.id, symbol, side, axis, count)
                child.kind, child.parent_set_id = "axis", parent.id
                child.template_set_id = parent.template_set_id or parent.id
                child.axis_key, child.relative_count = f"{axis}:{count}", count
                child.volume_ratio = round(count * VOLUME_RATIO_UNIT, 6)
                child.indication_symbol, child.locked = symbol, bool(parent.locked)
                mask = masks[(axis, count)]
                key = (direction, params, mask.tobytes())
                group = groups.setdefault(key, dict(parent=parent, direction=direction,
                    params=params, mask=mask, rows=(), count=0))
                records.append((child, parent, side, group))
    active_groups = {key: group for key, group in groups.items() if group["mask"].any()}
    _replay_mask_groups(book, symbol, bars, active_groups, lane, warmup, now, lane_exits, capture)
    for child, parent, side, group in records:
        child.hist = AxisTape(group["rows"], child)
        if capture and group.get("open"):
            out.open.append(_axis_metadata(group["open"], child))
        book._score_pair((child, None))
        child.n = group["count"]
        current_base = bool(book._base_metrics_ok((parent.by_side or {}).get(side) or {}))
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
