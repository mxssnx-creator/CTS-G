"""Causal Last-N stage sets backed by ordered references to their Base tape."""
from __future__ import annotations

from bisect import bisect_left
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import cached_property, lru_cache
from itertools import product
import math
import re
import weakref
from typing import Callable, NamedTuple, Sequence

import auto_set_pf

try:
    import numpy as np
except ImportError:
    np = None

STAGES = ("base", "main", "real", "exec")
BASE_LONG = (10, 20, 30, 40, 50)
SHORT = (4, 8, 12, 16)
LATE_SHORT = (*SHORT, 20, 24, 28, 30)
EXEC_SHORT = LATE_SHORT
LAST = (1, 2, 3, 4, 5)
SIGN_MODES = ("positive", "negative")
NEGATIVE_SHORT_MAX = 12
NEG_EPS = 1e-12
BLOCK_NAMES = ("long", "short", "last")
# Bound each fit on busy Sets. Both halves still need independent sample support.
MAX_WINDOW_OBSERVATIONS = 256


class Window(NamedTuple):
    long: int
    short: int
    last: int
    last_mode: str
    short_mode: str

    @property
    def need(self) -> int:
        return self.long + self.short + self.last


Path = tuple[Window, ...]


class StageOptions(NamedTuple):
    long_modes: tuple[str, ...]
    shorts: tuple[int, ...]
    lasts: tuple[int, ...]
    last_modes: tuple[str, ...]
    short_modes: tuple[str, ...]


@dataclass(frozen=True)
class StageGrid:
    longs: tuple[int, ...]
    options: tuple[StageOptions, ...]

    @classmethod
    def from_settings(cls, raw) -> StageGrid | None:
        if raw is None or raw is False:
            return None
        if not isinstance(raw, dict):
            raise ValueError("stageLastN must be an object")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("stageLastN.enabled must be a boolean")
        if not enabled:
            return None
        if set(raw) - {"enabled", *STAGES}:
            raise ValueError("Unknown stageLastN field")

        def counts(config, key, choices):
            values = config.get(key, choices)
            if not isinstance(values, (tuple, list)) or not values or any(
                type(n) is not int or n not in choices for n in values
            ) or len(set(values)) != len(values):
                raise ValueError(f"Invalid {key} choices in stageLastN")
            return tuple(sorted(values))

        def modes(config, key, defaults, stage):
            values = config.get(key, defaults)
            if not isinstance(values, (list, tuple)) or not values or any(
                value not in defaults for value in values
            ) or len(values) != len(set(values)):
                raise ValueError(f"Invalid stageLastN.{stage}.{key}")
            return tuple(mode for mode in defaults if mode in values)

        longs, stages = BASE_LONG, []
        for stage in STAGES:
            config = raw.get(stage, {})
            is_base = stage == "base"
            if not isinstance(config, dict) or set(config) - {"short", "last", "lastModes", "shortModes", "long" if is_base else "longModes"}:
                raise ValueError(f"Invalid stageLastN.{stage} options")
            if is_base:
                longs = counts(config, "long", BASE_LONG)
            long_modes = () if is_base else modes(config, "longModes", ("none", "inherit"), stage)
            shorts = counts(config, "short", LATE_SHORT if stage in ("real", "exec") else SHORT)
            short_modes = modes(config, "shortModes", SIGN_MODES, stage)
            if short_modes == ("negative",) and min(shorts) > NEGATIVE_SHORT_MAX:
                raise ValueError(f"Negative Short requires 4, 8 or 12 in stageLastN.{stage}")
            stages.append(StageOptions(long_modes, shorts, counts(config, "last", LAST),
                                       modes(config, "lastModes", SIGN_MODES, stage), short_modes))
        return cls(longs, tuple(stages))

    @lru_cache(maxsize=128)
    def windows(self, depth: int, base_long: int) -> tuple[Window, ...]:
        modes, shorts, lasts, last_modes, short_modes = self.options[depth]
        longs = self.longs if depth == 0 else tuple(base_long if mode == "inherit" else 0 for mode in modes)
        return tuple(Window(long, short, last, last_mode, short_mode)
                     for long, short, last, last_mode, short_mode in product(longs, shorts, lasts, last_modes, short_modes)
                     if short_mode == "positive" or short <= NEGATIVE_SHORT_MAX)

    def capacities(self) -> tuple[int, ...]:
        product_so_far = 1
        counts = []
        for depth in range(len(STAGES)):
            product_so_far *= len(self.windows(depth, self.longs[0] if depth else 0))
            counts.append(product_so_far)
        return tuple(counts)

    @cached_property
    def sample_bounds(self) -> dict[tuple[int, int], tuple[int, int]]:
        bounds = {}
        for depth in range(len(STAGES)):
            for base_long in self.longs if depth else (0,):
                sizes = [window.need for window in self.windows(depth, base_long)]
                bounds[depth, base_long] = min(sizes), max(sizes)
        return bounds

    def path(self, base_id: str, identity: str) -> Path:
        prefix = base_id + "/"
        if not identity.startswith(prefix):
            return ()
        parts = identity[len(prefix):].split("/")
        if len(parts) != len(STAGES):
            return ()
        out = []
        for depth, (name, part) in enumerate(zip(STAGES, parts)):
            match = re.fullmatch(r"([a-z]+):([0-9]{1,2})-([0-9]{1,2})([pn])-([0-9])([pn])", part)
            if match is None or match[1] != name:
                return ()
            w = Window(int(match[2]), int(match[3]), int(match[5]),
                       "positive" if match[6] == "p" else "negative",
                       "positive" if match[4] == "p" else "negative")
            if w not in self.windows(depth, out[0].long if out else 0):
                return ()
            out.append(w)
        return tuple(out)

    def settings(self) -> dict:
        return {"enabled": True, **{
            stage: {"short": option.shorts, "last": option.lasts, "lastModes": option.last_modes, "shortModes": option.short_modes,
                    **({"long": self.longs} if not option.long_modes else {"longModes": option.long_modes})}
            for stage, option in zip(STAGES, self.options)
        }}


def lane_id(base_set_id: str, path: Path) -> str:
    return base_set_id + "".join(
        f"/{stage}:{w.long}-{w.short}{w.short_mode[0]}-{w.last}{w.last_mode[0]}" for stage, w in zip(STAGES, path)
    )


def path_settings(path: Path) -> dict:
    return {stage: {"long": w.long, "short": w.short, "last": w.last, "lastMode": w.last_mode, "shortMode": w.short_mode}
            for stage, w in zip(STAGES, path)}


def pf(gain: float, loss: float) -> float:
    return gain / loss if loss > 0 else (99.0 if gain > 0 else 0.0)


class Evidence(NamedTuple):
    count: int = 0
    gain: float = 0.0
    loss: float = 0.0

    def __add__(self, other):
        return Evidence(self.count + other.count, self.gain + other.gain, self.loss + other.loss)

    def report(self) -> dict:
        return {"orders": self.count, "gain": self.gain, "loss": self.loss,
                "pf": round(self.gain / self.loss, 4) if self.loss else 1.0 if self.count and not self.gain else None}


class WindowAuto(NamedTuple):
    fallback: float
    minimum: int
    candidates: tuple[float, ...]
    tolerance: float
    orders_factor: float


@dataclass(slots=True)
class StageCount:
    evaluated: int = 0
    sample_full: int = 0
    qualified: int = 0
    negative_short: int = 0
    negative_last: int = 0
    orders: int = 0
    gain: float = 0.0
    loss: float = 0.0

    def add(self, other: StageCount, copies: int = 1) -> None:
        self.evaluated += other.evaluated * copies
        self.sample_full += other.sample_full * copies
        self.qualified += other.qualified * copies
        self.negative_short += other.negative_short * copies
        self.negative_last += other.negative_last * copies
        self.orders += other.orders * copies
        self.gain += other.gain * copies
        self.loss += other.loss * copies

    def report(self, total: int) -> dict:
        return {"configured": total, "evaluated": self.evaluated, "sampleFull": self.sample_full,
                "qualified": self.qualified, "negativeShort": self.negative_short, "negativeLast": self.negative_last,
                "samplePct": round(100 * self.sample_full / self.evaluated, 2) if self.evaluated else 0,
                **Evidence(self.orders, self.gain, self.loss).report()}


class StageOrder(NamedTuple):
    close: float
    entry: float
    net: float


@dataclass(frozen=True, slots=True)
class GridResult:
    counts: tuple[StageCount, ...]
    path: Path = ()
    evidence: Evidence = field(default_factory=Evidence)
    streams: tuple[tuple[StageOrder, ...], ...] = ()
    row_ids: tuple[tuple[int, ...], ...] = ()
    selected_evidence: tuple[Evidence, ...] = ()
    post_evidence: tuple[Evidence, ...] = ()
    selected_blocks: tuple[tuple[Evidence, ...], ...] = ()
    validated_base: Evidence = field(default_factory=Evidence)
    validated_base_ids: tuple[int, ...] = ()
    stream_sizes: tuple[int, ...] = ()
    selected_floors: tuple[tuple[float, ...], ...] = ()

    @property
    def ready(self) -> bool:
        return len(self.path) == len(STAGES)

    @property
    def rank(self) -> tuple[float, int]:
        return evidence_rank(self.evidence)

    def report(self, grid: StageGrid, set_id: str) -> dict:
        ids = [lane_id(set_id, self.path[:depth]) for depth in range(len(self.path) + 1)]
        return {"stages": {s: n.report(total) for s, n, total in zip(STAGES, self.counts, grid.capacities())},
                "id": ids[-1] if self.path else None,
                "parentIds": dict(zip(STAGES, ids[:-1])),
                "setIds": dict(zip(STAGES, ids[1:])),
                "windows": path_settings(self.path),
                "selectedOrders": dict(zip(STAGES, self.stream_sizes or tuple(map(len, self.streams)))),
                "selectedWindows": {s: ev.report() for s, ev in zip(STAGES, self.selected_evidence)},
                "selectedBlocks": {s: {name: ev.report() for name, ev in zip(BLOCK_NAMES, blocks) if ev.count}
                                   for s, blocks in zip(STAGES, self.selected_blocks)},
                "selectedFloors": {s: {name: floor for name, floor in zip(BLOCK_NAMES, floors) if floor}
                                    for s, floors in zip(STAGES, self.selected_floors)},
                "validatedBase": self.validated_base.report(),
                "postStages": {s: ev.report() for s, ev in zip(STAGES, self.post_evidence)}}


def evidence_rank(evidence: Evidence) -> tuple[float, int]:
    count, gain, loss = evidence
    # Reduce the effect of very short, zero-loss samples on selection.
    return round((min(5.0, pf(gain, loss)) - 1) * count / (20 + count), 12), count


BYTE_BITS = tuple(tuple(bit for bit in range(8) if byte & (1 << bit)) for byte in range(256))
BaseRisk = Callable[[Sequence[float], Sequence[float], Sequence[float], Sequence[float], float, int, int], bool]


class _Tape:
    """An immutable history along one parent. Windows share its cumulative gains and losses."""
    __slots__ = ("ids", "times", "values", "gains", "losses", "decisions", "owner", "masks", "sign", "numeric",
                 "learned_floors")

    def __init__(self, owner: StageHistory, ids: Sequence[int]) -> None:
        self.owner, self.ids = weakref.proxy(owner), ids
        self.times = times = tuple(owner.closes[i] for i in ids)
        self.values = values = tuple(owner.nets[i] for i in ids)
        self.gains, self.losses = [0.0], [0.0]
        for net in values:
            self.gains.append(self.gains[-1] + max(net, 0.0))
            self.losses.append(self.losses[-1] + max(-net, 0.0))
        self.sign = 1 if values and all(v > NEG_EPS for v in values) else (
            -1 if values and all(v < -NEG_EPS for v in values) else 0
        )
        self.decisions = None
        self.numeric = None
        self.masks: dict[tuple[int, int, str], int] = {}
        self.learned_floors: dict[tuple[int, int, str, float], float] | None = None

    def indices(self, now: float) -> tuple[int, int]:
        return bisect_left(self.times, now - self.owner.lookback), bisect_left(self.times, now)

    def evidence(self, size: int, end: int) -> Evidence:
        start = end - size
        gain = self.gains[end] - self.gains[start]
        loss = self.losses[end] - self.losses[start]
        # Avoid cancellation of a tiny but nonzero window after large earlier values.
        if self.losses[end] > 0 and loss <= 1e-10 * self.losses[end]:
            loss = math.fsum(-v for v in self.values[start:end] if v < 0)
        if self.gains[end] > 0 and gain <= 1e-10 * self.gains[end]:
            gain = math.fsum(v for v in self.values[start:end] if v > 0)
        return Evidence(size, gain, loss)

    def floor(self, size: int, offset: int, kind: str, when: float) -> float:
        policy = self.owner.window_auto
        fallback = self.owner.floor_at(size, when) if policy is None else policy.fallback
        if policy is None or self.sign or size == 1 or len(self.ids) < size + offset + policy.minimum:
            return fallback
        lower, upper = auto_set_pf.training_window(when)
        first, stop = bisect_left(self.times, lower), bisect_left(self.times, upper)
        first = max(first, size + offset, stop - max(MAX_WINDOW_OBSERVATIONS, 2 * policy.minimum))
        if stop - first < policy.minimum:
            return fallback
        key = size, offset, kind, upper
        if self.learned_floors is None:
            self.learned_floors = {}
        cached = self.learned_floors.get(key)
        if cached is not None:
            return cached
        if self.decisions is None:
            self.decisions = [self.indices(self.owner.entries[i]) for i in self.ids]
        pairs = []
        for i in range(first, stop):
            lo, hi = self.decisions[i]
            hi -= offset
            if hi - lo < size:
                continue
            _, gain, loss = self.evidence(size, hi)
            if kind == "negative":
                gain, loss = loss, gain
            if gain > 0 and (kind not in SIGN_MODES or gain - loss > NEG_EPS):
                pairs.append((gain / loss if loss > 0 else math.inf, self.values[i]))
        fit = auto_set_pf.fit(pairs, policy.minimum, fallback, policy.candidates, policy.tolerance,
                              policy.orders_factor)
        floor = fallback if fit is None else fit
        self.learned_floors[key] = floor
        return floor

    def test(self, size: int, end: int, kind: str, entry: float, offset: int = 0) -> Evidence | None:
        evidence = self.evidence(size, end)
        gain, loss = evidence.gain, evidence.loss
        if kind in SIGN_MODES:
            # PF above/below neutral, compared in return units to handle loss-free windows.
            net = gain - loss
            if abs(abs(net) - NEG_EPS) <= 1e-8 * max(gain, loss):
                net = math.fsum(self.values[end - size:end])
            if kind == "negative":
                net, gain, loss = -net, loss, gain
            if net <= NEG_EPS:
                return None
            if loss > 0:
                threshold = self.floor(size, offset, kind, entry)
                if math.isclose(gain, loss * threshold, rel_tol=1e-8):
                    values = self.values[end - size:end]
                    positive = math.fsum(v for v in values if v > 0)
                    negative = math.fsum(-v for v in values if v < 0)
                    gain, loss = (positive, negative) if kind == "positive" else (negative, positive)
                    evidence = Evidence(size, positive, negative)
                if gain < loss * threshold:
                    return None
            return evidence
        floor = self.floor(size, offset, kind, entry)
        if loss > 0:
            if math.isclose(gain, loss * floor, rel_tol=1e-8):
                # A borderline result must be independent of any older prefix.
                values = self.values[end - size:end]
                gain = math.fsum(v for v in values if v > 0)
                loss = math.fsum(-v for v in values if v < 0)
                evidence = Evidence(size, gain, loss)
            if gain < loss * floor:
                return None
        elif gain <= 0:
            return None
        if kind == "risk" and self.owner.base_risk and not self.owner.base_risk(
            self.times, self.values, self.gains, self.losses, entry, end, end - size
        ):
            return None
        return evidence

    def mask(self, size: int, offset: int, kind: str) -> int:
        key = (size, offset, kind)
        if key in self.masks:
            return self.masks[key]
        always = (self.sign > 0 and (kind in ("positive", "pf") or kind == "risk" and not self.owner.base_risk)
                  or self.sign < 0 and kind == "negative")
        if self.sign and not always and kind not in ("risk",):
            return 0
        if always:
            # With a uniform sign the only remaining test is having all of the
            # disjoint blocks. Every equivalent mask reuses one count query.
            key = (size + offset, 0, "samples")
            if key in self.masks:
                return self.masks[key]
        if np is not None and len(self.ids) >= 96 and not always:
            mask = self.vector_mask(size, offset, kind)
            self.masks[key] = mask
            return mask
        if self.decisions is None:
            self.decisions = [self.indices(self.owner.entries[i]) for i in self.ids]
        bits = bytearray((len(self.ids) + 7) // 8)
        for j, (start, end) in enumerate(self.decisions):
            end -= offset
            if end - start >= size and (always or self.test(size, end, kind, self.owner.entries[self.ids[j]], offset) is not None):
                bits[j // 8] |= 1 << (j % 8)
        mask = int.from_bytes(bits, "little")
        self.masks[key] = mask
        return mask

    def vector_mask(self, size: int, offset: int, kind: str) -> int:
        if self.numeric is None:
            times = np.asarray(self.times)
            entries = np.fromiter((self.owner.entries[i] for i in self.ids), dtype=np.float64, count=len(self.ids))
            floors = self.owner.entry_floors
            local_floors = (np.fromiter((floors[i] for i in self.ids), dtype=np.float64, count=len(self.ids))
                            if floors is not None else None)
            self.numeric = (np.searchsorted(times, entries - self.owner.lookback),
                            np.searchsorted(times, entries), np.asarray(self.gains), np.asarray(self.losses), local_floors)
        first, ends, gains, losses, local_floors = self.numeric
        valid = ends - first >= offset + size
        rows = np.flatnonzero(valid)
        right = ends[rows] - offset
        left = right - size
        gain, loss = gains[right] - gains[left], losses[right] - losses[left]
        scoped = self.owner.window_auto is not None
        if scoped:
            floors = np.fromiter((self.floor(size, offset, kind, self.owner.entries[self.ids[i]]) for i in rows),
                                 dtype=np.float64, count=len(rows))
        else:
            floors = local_floors[rows] if local_floors is not None else self.owner.floors[size]
        if kind in SIGN_MODES:
            margin = gain - loss
            good = margin > NEG_EPS if kind == "positive" else margin < -NEG_EPS
            borderline = np.abs(np.abs(margin) - NEG_EPS) <= 1e-8 * np.maximum(gain, loss)
            numerator, denominator = (gain, loss) if kind == "positive" else (loss, gain)
            threshold = denominator * floors
            good &= numerator >= threshold
            borderline |= np.abs(numerator - threshold) <= 1e-8 * np.maximum(numerator, threshold)
        else:
            threshold = loss * floors
            margin = gain - threshold
            good = (gain > 0) & (margin >= 0)
            borderline = np.abs(margin) <= 1e-8 * np.maximum(gain, threshold)
        borderline |= ((loss <= 1e-10 * losses[right]) & (losses[right] > 0)
                       | (gain <= 1e-10 * gains[right]) & (gains[right] > 0))
        verify = (good | borderline) if kind == "risk" and self.owner.base_risk else borderline
        for j in np.flatnonzero(verify):
            row = self.ids[rows[j]]
            good[j] = self.test(size, int(right[j]), kind, self.owner.entries[row], offset) is not None
        valid[rows] = good
        return int.from_bytes(np.packbits(valid, bitorder="little").tobytes(), "little")

    def passes(self, w: Window, depth: int, now: float) -> bool:
        start, end = self.indices(now)
        if end - start < w.need:
            return False
        if depth == 1 and self.owner.main_filter and not self.owner.main_filter(
            self.values[max(start, end - self.owner.filter_need):end]
        ):
            return False
        return all(self.test(n, end - offset, kind, now, offset) is not None
                   for n, offset, kind in window_blocks(w, depth) if n)

    def child_mask(self, w: Window, depth: int) -> int:
        mask = self.mask(w.last, 0, w.last_mode) & self.mask(w.short, w.last, "pf" if w.short_mode == "positive" else "negative")
        if mask and w.long:
            mask &= self.mask(w.long, w.last + w.short, "risk" if depth == 0 else "pf")
        if mask and depth == 1 and self.owner.main_filter:
            mask &= self.owner.filter_mask(self)
        return mask


def window_blocks(w: Window, depth: int) -> tuple:
    return ((w.last, 0, w.last_mode),
            (w.short, w.last, "pf" if w.short_mode == "positive" else "negative"),
            (w.long, w.short + w.last, "risk" if depth == 0 else "pf"))


class StageHistory:
    """Build only reachable paths at a decision. Equal parent memberships share calculations, while keeping their counts and identities."""
    def __init__(self, grid: StageGrid, closes: Sequence[float], entries: Sequence[float], nets: Sequence[float],
                 lookback: float, floor: Callable[[int], float], base_risk: BaseRisk | None = None,
                 main_filter: Callable[[Sequence[float]], bool] | None = None, filter_need: int = 0,
                 entry_floor: Callable[[float], float] | None = None,
                 window_auto: WindowAuto | None = None) -> None:
        self.grid, self.closes, self.entries, self.nets = grid, closes, entries, nets
        self.lookback, self.base_risk = lookback, base_risk
        sizes = {n for options in grid.options for n in (*options.shorts, *options.lasts)}
        self.floors = {n: floor(n) for n in {*grid.longs, *sizes}}
        self.entry_floor = entry_floor
        self.window_auto = window_auto
        self._entry_floors = None
        self.main_filter, self.filter_need = main_filter, filter_need
        self._root: _Tape | None = None
        self._decision: tuple[float, _Tape] | None = None
        self._views: OrderedDict[Sequence[int], _Tape] = OrderedDict()
        self._children: OrderedDict[tuple[_Tape, int], _Tape] = OrderedDict()
        self._filter_masks: OrderedDict[_Tape, int] = OrderedDict()
        self._rules = {(i, n): grid.windows(i, n) for i in range(len(STAGES)) for n in ((0,) if i == 0 else grid.longs)}

    @property
    def root(self) -> _Tape:
        if self._root is None:
            self._root = _Tape(self, range(len(self.closes)))
        return self._root

    def _decision_tape(self, now: float) -> _Tape:
        """Find the raw closes the current gates and their history can reach."""
        if self._decision and self._decision[0] == now:
            return self._decision[1]
        end = first = bisect_left(self.closes, now)
        if self.window_auto is not None:
            # Auto can read older, independently admitted outcomes of any
            # parent. Keep the available strictly closed context for training.
            tape = self.root if end == len(self.closes) else _Tape(self, range(end))
            self._decision = now, tape
            return tape
        earliest = now
        # A child can reach older evidence through the entry of a position
        # closed inside its window. Walk back once for each historical stage,
        # including the Exec closes reported after the four gates.
        for _ in STAGES:
            start = bisect_left(self.closes, earliest - self.lookback, 0, first)
            if start == first:
                break
            earliest = min(earliest, min(self.entries[i] for i in range(start, first)))
            first = start
        start = bisect_left(self.closes, earliest - self.lookback, 0, first)
        tape = self.root if start == 0 and end == len(self.closes) else _Tape(self, range(start, end))
        self._decision = now, tape
        return tape

    def floor_at(self, n: int, at: float) -> float:
        return self.entry_floor(at) if self.entry_floor else self.floors[n]

    @property
    def entry_floors(self):
        if self.entry_floor and self._entry_floors is None:
            self._entry_floors = tuple(self.entry_floor(t) for t in self.entries)
        return self._entry_floors

    def filter_mask(self, tape: _Tape) -> int:
        if tape in self._filter_masks:
            self._filter_masks.move_to_end(tape)
            return self._filter_masks[tape]
        bits = bytearray((len(tape.ids) + 7) // 8)
        decisions = tape.decisions
        if decisions is None:
            decisions = [tape.indices(self.entries[i]) for i in tape.ids]
        for j, (start, end) in enumerate(decisions):
            if self.main_filter(tape.values[max(start, end - self.filter_need):end]):
                bits[j // 8] |= 1 << (j % 8)
        self._filter_masks[tape] = mask = int.from_bytes(bits, "little")
        if len(self._filter_masks) > 24:
            self._filter_masks.popitem(last=False)
        return mask

    def child(self, tape: _Tape, mask: int) -> _Tape:
        key = tape, mask
        view = self._children.get(key)
        if view is not None:
            self._children.move_to_end(key)
            return view
        size = mask.bit_count()
        if size == len(tape.ids):
            return tape
        end = mask.bit_length()
        if mask >> (end - size) == (1 << size) - 1:
            ids = tape.ids[end - size:end]
        else:
            ids = tuple(tape.ids[8 * b + i] for b, byte in enumerate(mask.to_bytes((end + 7) // 8, "little"))
                        for i in BYTE_BITS[byte])
        view = self._views.get(ids)
        if view is None:
            view = self._views[ids] = _Tape(self, ids)
            if len(self._views) > 128:
                self._views.popitem(last=False)
        else:
            self._views.move_to_end(ids)
        self._children[key] = view
        if len(self._children) > 128:
            self._children.popitem(last=False)
        return view

    @staticmethod
    def latest_members(tape: _Tape, mask: int, start: int, end: int, limit: int) -> tuple[int, ...]:
        """A terminal gate only has windows on the recent Real closes."""
        bits = mask & ((1 << end) - 1)
        latest = []
        while bits and len(latest) < limit:
            current = bits.bit_length() - 1
            if current < start:
                break
            latest.append(tape.ids[current])
            bits ^= 1 << current
        return tuple(reversed(latest))

    def related_tapes(self, path: Path, root: _Tape | None = None):
        tape = self.root if root is None else root
        for depth, w in enumerate(path):
            tape = self.child(tape, tape.child_mask(w, depth))
            yield tape

    def stream_ids(self, path: Path, cutoff: float, now: float, limit: int | None = None) -> tuple[tuple[int, ...], ...]:
        out = []
        for tape in self.related_tapes(path):
            start, end = bisect_left(tape.times, cutoff), bisect_left(tape.times, now)
            if limit is not None:
                start = max(start, end - limit)
            out.append(tuple(tape.ids[start:end]))
        return tuple(out)

    def selected_blocks(self, path: Path, now: float, root: _Tape | None = None):
        tape = self._decision_tape(now) if root is None else root
        evidence, floors = [], []
        for depth, w in enumerate(path):
            _, end = tape.indices(now)
            blocks = tuple(reversed(window_blocks(w, depth)))
            evidence.append(tuple(tape.evidence(n, end - offset) if n else Evidence() for n, offset, _ in blocks))
            floors.append(tuple(tape.floor(n, offset, kind, now) if n else 0.0 for n, offset, kind in blocks))
            if depth + 1 < len(path):
                tape = self.child(tape, tape.child_mask(w, depth))
        return tuple(evidence), tuple(floors)

    def read_streams(self, row_ids: tuple[tuple[int, ...], ...]) -> tuple[tuple[StageOrder, ...], ...]:
        return tuple(tuple(StageOrder(self.closes[i], self.entries[i], self.nets[i]) for i in ids) for ids in row_ids)

    def stream(self, path: Path, cutoff: float, now: float, limit: int | None = None) -> tuple[tuple[StageOrder, ...], ...]:
        return self.read_streams(self.stream_ids(path, cutoff, now, limit))

    def active_real(self, path: Path, now: float, limit: int) -> tuple[tuple[float, float], ...] | None:
        """Current Real evidence for a held position's exact Exec path."""
        if len(path) != len(STAGES):
            return None
        tape = self._decision_tape(now)
        for depth, w in enumerate(path):
            if not tape.passes(w, depth, now):
                return None
            if depth == len(STAGES) - 1:
                low, end = tape.indices(now)
                return tuple(zip(tape.times[max(low, end - limit):end],
                                 tape.values[max(low, end - limit):end]))
            tape = self.child(tape, tape.child_mask(w, depth))
        return None

    def evaluate(self, now: float, *, streams: bool = False, limit: int | None = None) -> GridResult:
        validated_base_mask = 0
        root = self._decision_tape(now)
        bounds = self.grid.sample_bounds

        @lru_cache(maxsize=2048)
        def last_stage(ids: tuple[int, ...], base_long: int) -> GridResult:
            return walk(len(STAGES) - 1, _Tape(self, ids), base_long)

        @lru_cache(maxsize=128)
        def walk(depth: int, tape: _Tape, base_long: int) -> GridResult:
            nonlocal validated_base_mask
            counts = tuple(StageCount() for _ in STAGES[depth:])
            tally = counts[0]
            choices = self._rules[depth, base_long]
            tally.evaluated = len(choices)
            start, end = tape.indices(now)
            size = end - start
            if size < bounds[depth, base_long][0]:
                return GridResult(counts)
            results: dict[tuple[int, int, str], Evidence | None] = {}
            paths: dict[tuple[int, int], tuple[Window, int]] = {}
            picked_path: Path = ()
            picked_evidence = Evidence()
            picked_key = (0, (0.0, 0))
            filter_ok = depth != 1 or not self.main_filter or self.main_filter(
                tape.values[max(start, end - self.filter_need):end]
            )

            for w in choices:
                if size < w.need:
                    continue
                tally.sample_full += 1
                if not filter_ok:
                    continue
                evidence = Evidence()
                for n, offset, kind in window_blocks(w, depth):
                    if not n:
                        continue
                    key = (n, offset, kind)
                    if key not in results:
                        results[key] = tape.test(n, end - offset, kind, now, offset)
                    window = results[key]
                    if window is None:
                        break
                    evidence += window
                else:
                    tally.qualified += 1
                    tally.negative_short += int(w.short_mode == "negative")
                    tally.negative_last += int(w.last_mode == "negative")
                    tally.orders += evidence.count
                    tally.gain += evidence.gain
                    tally.loss += evidence.loss
                    if depth == len(STAGES) - 1:
                        candidate_key = (1, evidence_rank(evidence))
                        if candidate_key > picked_key:
                            picked_path, picked_evidence, picked_key = (w,), evidence, candidate_key
                        continue
                    key = (w.long if depth == 0 else base_long, tape.child_mask(w, depth))
                    first, copies = paths.get(key, (w, 0))
                    paths[key] = (first, copies + 1)

            for (long, mask), (first, copies) in paths.items():
                if streams and depth == 0:
                    validated_base_mask |= mask
                if depth == len(STAGES) - 2 and self.window_auto is None:
                    max_window = bounds[depth + 1, long][1]
                    ids = self.latest_members(tape, mask, start, end, max_window)
                    next_result = last_stage(ids, long)
                else:
                    next_result = walk(depth + 1, self.child(tape, mask), long)
                for dest, source in zip(counts[1:], next_result.counts):
                    dest.add(source, copies)
                candidate_key = (1 + len(next_result.path), next_result.rank)
                if candidate_key > picked_key:
                    picked_path = (first, *next_result.path)
                    picked_evidence, picked_key = next_result.evidence, candidate_key
            return GridResult(counts, picked_path, picked_evidence)

        try:
            result = walk(0, root, 0)
        finally:
            walk.cache_clear()
            last_stage.cache_clear()
        if streams and result.path:
            row_ids, post = [], []
            for tape in self.related_tapes(result.path, root):
                start, end = tape.indices(now)
                post.append(tape.evidence(end - start, end))
                shown = start if limit is None else max(start, end - limit)
                row_ids.append(tuple(tape.ids[shown:end]))
            row_ids = tuple(row_ids)
            blocks, floors = self.selected_blocks(result.path, now, root)
            selected = tuple(sum(group, Evidence()) for group in blocks)
            low, high = root.indices(now)
            # A Base position can belong to several qualifying First Stage Sets.
            # Preserve its exact membership in each path, but contribute it once
            # to the Base Set's aggregate and let relations deduplicate signals.
            base_ids = self.latest_members(root, validated_base_mask, low, high, high - low)
            base_net = [self.nets[i] for i in base_ids]
            base = Evidence(len(base_ids), math.fsum(x for x in base_net if x > 0),
                            math.fsum(-x for x in base_net if x < 0))
            return GridResult(result.counts, result.path, result.evidence, self.read_streams(row_ids), row_ids,
                              selected, tuple(post), blocks, base, base_ids, selected_floors=floors)
        return result
