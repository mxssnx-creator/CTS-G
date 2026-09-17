"""CTS Block strategy — formulas and lifecycle as in BLOCK_STRATEGY_SYSTEM.md / block-count-state.ts."""
from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

from position_cost import (
    INTERN_PF,
    POSITION_COST_PCT_DEFAULT,
    POSITIVE_PF,
    cost_as_frac,
    normalize_position_cost_pct,
    ratio_from_r,
)

BLOCK_COUNT_MIN = 1
BLOCK_COUNT_PREVIEW = 6
BLOCK_EVAL_N = 6
BLOCK_STACK_DEFAULT = 6
BLOCK_STACK_MAX = 6
BLOCK_VOL_RATIO_MIN = 0.05
BLOCK_VOL_RATIO_MAX = 2.0
BLOCK_VOL_RATIO_DEFAULT = 0.25
BLOCK_MAX_VOLUME_MULTIPLIER = 2.0
BLOCK_EVAL_POS_DEFAULT = 50
BLOCK_EVAL_POS_MIN = 5
BLOCK_EVAL_POS_MAX = 75
# Base-1 PF coordination (position_cost): 1.00=neutral, 0.10=1×PositionCost.
# Floor moved 0.2 -> 0.5 in the same +0.3 relation as the 0.8 -> 1.1 default.
BLOCK_PF_RATIO_MIN = 0.5
BLOCK_PF_RATIO_MAX = 5.0


def finite_number(value: Any, fallback: float = 0.0) -> float:
    try:
        parsed = float(value)
    except Exception:
        return fallback
    return parsed if parsed == parsed and abs(parsed) != float("inf") else fallback


def clamp(n: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, n))


def parse_block_count(set_key: str) -> Optional[int]:
    import re
    m = re.search(r"#block:(?:(?:active|set):)?(\d+)(?:$|[#:_-])", str(set_key or ""), re.I)
    if not m:
        return None
    c = int(m.group(1))
    return c if c >= BLOCK_COUNT_MIN else None


def clamp_stack(n: Any) -> int:
    try:
        v = int(n)
    except Exception:
        v = 0
    if v <= 0:
        return BLOCK_STACK_DEFAULT
    return max(1, min(BLOCK_STACK_MAX, v))


def clamp_eval_pos_count(value: Any, fallback: int = BLOCK_EVAL_POS_DEFAULT) -> int:
    try:
        n = int(value)
    except Exception:
        n = int(fallback or BLOCK_EVAL_POS_DEFAULT)
    if n <= 0:
        n = int(fallback or BLOCK_EVAL_POS_DEFAULT)
    return max(BLOCK_EVAL_POS_MIN, min(BLOCK_EVAL_POS_MAX, n))


def calculate_block_volume_increment_ratio(block_count: int, volume_ratio: float) -> float:
    block_count = finite_number(block_count)
    volume_ratio = finite_number(volume_ratio)
    if block_count <= 0 or volume_ratio <= 0:
        return 0.0
    return int(block_count) * volume_ratio


def calculate_block_volume_multiplier(block_count: int, volume_ratio: float) -> float:
    """Base-1 arithmetic: an inactive increment leaves the original quantity."""
    return 1.0 + calculate_block_volume_increment_ratio(block_count, volume_ratio)


def calculate_block_max_additional_ratio(
    max_stack: int, volume_ratio: float, max_multiplier: float = BLOCK_MAX_VOLUME_MULTIPLIER
) -> float:
    """Bound the additive target, never sum targets or compound earlier fills."""
    cap = clamp(finite_number(max_multiplier, BLOCK_MAX_VOLUME_MULTIPLIER), 1.0, BLOCK_MAX_VOLUME_MULTIPLIER)
    return min(cap - 1.0, calculate_block_volume_increment_ratio(max_stack, volume_ratio))


def shared_block_volume_ratio(
    volume_ratio: float,
    live_count: int,
    extra_cap: float = 1.0,
) -> float:
    """Share the extra-size budget when count 1 would consume the whole cap.

    Absolute target remains ``min(extra_cap, n × ratio)``. When the configured
    ratio already saturates the extra cap at n=1, later live counts would have
    stepQty=0. Split the extra evenly across live counts so each enabled rung
    can emit, while the 2× parent cap is unchanged.
    """
    vr = clamp(finite_number(volume_ratio, BLOCK_VOL_RATIO_DEFAULT), BLOCK_VOL_RATIO_MIN, BLOCK_VOL_RATIO_MAX)
    extra = max(0.0, finite_number(extra_cap, 1.0))
    try:
        n = max(1, int(live_count or 0))
    except Exception:
        n = 1
    if n > 1 and extra > 0 and vr + 1e-12 >= extra:
        # Effective split may sit below the configured slider floor; the floor
        # applies to user input, not the derived per-count share.
        return extra / float(n)
    return vr


def normalize_block_counts(value: Any) -> List[int]:
    if not isinstance(value, (list, tuple)):
        return list(range(1, BLOCK_EVAL_N + 1))
    return sorted({int(n) for n in value if not isinstance(n, bool)
                   and isinstance(n, (int, float)) and finite_number(n) == n
                   and int(n) == n and 1 <= n <= BLOCK_EVAL_N})


def calculate_block_minimum_profit_factor(
    default_min_pf: float, block_pf_ratio: float, volume_increment: float
) -> float:
    if min(default_min_pf, block_pf_ratio, volume_increment) <= 0:
        return 0.0
    bounded = clamp(block_pf_ratio, BLOCK_PF_RATIO_MIN, BLOCK_PF_RATIO_MAX)
    return 1 + max(0.0, default_min_pf - 1) * bounded * volume_increment


def calculate_block_effective_minimum_profit_factor(configured: float, normal: float) -> float:
    return max(configured if configured > 0 else 0.0, normal if normal > 0 else 0.0)


def cost_pf_from_net_fracs(samples: Optional[List[float]], cost_pct: float = POSITION_COST_PCT_DEFAULT) -> float:
    """PositionCost ratio from cost-net fractions. Intern 1.00 != real 1.10.

    ``samples`` are already cost-net fractions (0.001 = +0.10% net). Cost is
    the CTS percent convention (0.10 = 0.10%); legacy fractions ≤ 0.02 are
    accepted via ``cost_as_frac``.
    """
    vals = [float(x) for x in (samples or []) if x is not None]
    if not vals:
        return 0.0
    cost_frac = cost_as_frac(normalize_position_cost_pct(cost_pct))
    if cost_frac <= 1e-15:
        return 0.0
    avg_r = sum(v / cost_frac for v in vals) / len(vals)
    return float(ratio_from_r(avg_r))


@dataclass
class BlockLeg:
    set_key: str
    block_count: int
    quantity: float
    base_quantity: float
    volume_ratio: float
    volume_increment_ratio: float
    target_additional_quantity: float
    confirmed_additional_quantity_before: float
    target_block_quantity: float
    target_satisfied: bool
    requested_quantity: float
    pause_count: int
    client_order_id: str = ""
    order_id: str = ""
    added_at: float = 0.0
    scope: str = "long"


@dataclass
class BlockLane:
    symbol: str
    side: str  # LONG/SHORT
    base_qty: float
    base_entry: float
    confirmed_add: float = 0.0
    legs: List[BlockLeg] = field(default_factory=list)
    pause_remaining: Dict[int, int] = field(default_factory=dict)
    pause_until: Dict[int, float] = field(default_factory=dict)
    pf_ring: Dict[int, List[float]] = field(default_factory=dict)
    parent_pf_ring: List[float] = field(default_factory=list)
    satisfied: Dict[int, bool] = field(default_factory=dict)
    held_factor: Dict[int, float] = field(default_factory=dict)
    active: bool = True
    group_key: str = ""


class BlockBook:
    """Independent Block book: never opens without a same-side parent."""

    def __init__(self, path: str, cfg: Optional[Dict[str, Any]] = None) -> None:
        self.path = path
        cfg = cfg or {}
        self.enabled = bool(cfg.get("variantBlockEnabled", True))
        raw_stack = cfg.get("blockMaxStack", 0)
        try:
            stack_n = int(raw_stack if raw_stack is not None else 0)
        except Exception:
            stack_n = 0
        # Six independent decisions share one bounded physical parent position.
        self.max_stack = clamp_stack(raw_stack)
        self.eval_n = BLOCK_EVAL_N
        self.volume_ratio = clamp(finite_number(cfg.get("blockVolumeRatio"), BLOCK_VOL_RATIO_DEFAULT), BLOCK_VOL_RATIO_MIN, BLOCK_VOL_RATIO_MAX)
        self.max_volume_multiplier = clamp(finite_number(cfg.get("blockMaxVolumeMultiplier"), BLOCK_MAX_VOLUME_MULTIPLIER), 1.0, BLOCK_MAX_VOLUME_MULTIPLIER)
        self.counts = normalize_block_counts(cfg.get("blockCounts"))
        self.pf_ratio = clamp(finite_number(cfg.get("blockProfitFactorRatio", 1.1) or 1.1, 1.1), BLOCK_PF_RATIO_MIN, BLOCK_PF_RATIO_MAX)
        self.pause_ratio = max(0, int(finite_number(cfg.get("blockPauseCountRatio", 1) or 1, 1.0)))
        self.active_real = bool(cfg.get("blockActiveRealEnabled", True))
        self.active_live = bool(cfg.get("blockActiveLiveEnabled", True))
        self.default_min_pf = float(cfg.get("defaultMinPF", POSITIVE_PF) or POSITIVE_PF)
        self.cost_pct = max(1e-9, float(cfg.get("positionCostPct", POSITION_COST_PCT_DEFAULT) or POSITION_COST_PCT_DEFAULT))
        self.min_samples = max(1, int(cfg.get("prevPosMinCount", 5) or 5))
        self.eval_pos_count = clamp_eval_pos_count(cfg.get("blockEvalPosCount") or cfg.get("mainEvalPosCount") or BLOCK_EVAL_POS_DEFAULT)
        self.window = max(self.min_samples, int(cfg.get("prevPosWindow", 25) or 25), self.eval_pos_count)
        self.lanes: Dict[str, BlockLane] = {}
        self.count_tape: Dict[int, List[float]] = {n: [] for n in range(1, BLOCK_EVAL_N + 1)}
        self.overall_tape: List[float] = []
        self.load()

    def key(self, symbol: str, side: str, group_key: str = "") -> str:
        """Return a stable lane key, optionally scoped to one logical range group."""
        base = f"{symbol}:{side}"
        return f"{base}:group:{group_key}" if group_key else base

    def live_counts(self) -> List[int]:
        """Enabled counts that may actually emit under the live stack cap."""
        return [n for n in self.counts if n <= self.max_stack]

    def extra_cap(self) -> float:
        return max(0.0, float(self.max_volume_multiplier) - 1.0)

    def effective_volume_ratio(self) -> float:
        """Configured ratio, shared across live counts when n=1 would eat the cap."""
        return shared_block_volume_ratio(self.volume_ratio, len(self.live_counts()), self.extra_cap())

    def specified_volume_ratio(self) -> float:
        """Configured Block Active ratio. Never a 6-count split."""
        return clamp(finite_number(self.volume_ratio, BLOCK_VOL_RATIO_DEFAULT), BLOCK_VOL_RATIO_MIN, BLOCK_VOL_RATIO_MAX)

    def active_increment(self, count: int = 1) -> float:
        """Each Active add is the specified ratio, capped at extra (2× parent).

        Count is attribution only. Size does not use n×ratio or shared crumbs.
        """
        return min(self.extra_cap(), self.specified_volume_ratio())

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path) as state_file:
                raw = json.load(state_file)
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        leg_fields = set(BlockLeg.__dataclass_fields__)
        for k, v in (raw.get("lanes") or {}).items():
            if not isinstance(v, dict):
                continue
            symbol = str(v.get("symbol") or "").strip().upper()
            side = str(v.get("side") or "").strip().upper()
            if not symbol or side not in ("LONG", "SHORT"):
                continue
            legs: List[BlockLeg] = []
            for raw_leg in v.get("legs") or []:
                if not isinstance(raw_leg, dict):
                    continue
                try:
                    leg = BlockLeg(**{name: raw_leg[name] for name in leg_fields if name in raw_leg})
                except (TypeError, ValueError):
                    continue
                legs.append(leg)
            lane = BlockLane(
                symbol=symbol,
                side=side,
                base_qty=float(v.get("base_qty") or 0),
                base_entry=float(v.get("base_entry") or 0),
                confirmed_add=float(v.get("confirmed_add") or 0),
                legs=legs,
                pause_remaining={int(a): int(b) for a, b in (v.get("pause_remaining") or {}).items()},
                pause_until={int(a): float(b) for a, b in (v.get("pause_until") or {}).items()},
                pf_ring={int(a): list(b) for a, b in (v.get("pf_ring") or {}).items()},
                parent_pf_ring=list(v.get("parent_pf_ring") or []),
                satisfied={int(a): bool(b) for a, b in (v.get("satisfied") or {}).items()},
                held_factor={int(a): float(b) for a, b in (v.get("held_factor") or {}).items()},
                active=bool(v.get("active", True)),
                group_key=str(v.get("group_key") or v.get("groupKey") or ""),
            )
            self.lanes[k] = lane
        for n, tape in (raw.get("countTape") or {}).items():
            try:
                self.count_tape[int(n)] = [float(x) for x in (tape or [])][-self.window :]
            except Exception:
                continue
        try:
            self.overall_tape = [float(x) for x in (raw.get("overallTape") or [])][-self.window :]
        except Exception:
            self.overall_tape = []

    def save(self) -> None:
        if not self.path:
            return
        blob = {
            "cfg": {
                "variantBlockEnabled": self.enabled,
                "blockMaxStack": self.max_stack,
                "blockVolumeRatio": self.volume_ratio,
                "blockMaxVolumeMultiplier": self.max_volume_multiplier,
                "blockCounts": self.counts,
                "blockProfitFactorRatio": self.pf_ratio,
                "blockPauseCountRatio": self.pause_ratio,
                "blockActiveRealEnabled": self.active_real,
                "blockActiveLiveEnabled": self.active_live,
                "blockEvalPosCount": self.eval_pos_count,
            },
            "lanes": {},
        }
        for k, lane in self.lanes.items():
            blob["lanes"][k] = {
                "symbol": lane.symbol,
                "side": lane.side,
                "base_qty": lane.base_qty,
                "base_entry": lane.base_entry,
                "confirmed_add": lane.confirmed_add,
                "legs": [asdict(x) for x in lane.legs],
                "pause_remaining": {str(a): b for a, b in lane.pause_remaining.items()},
                "pause_until": {str(a): b for a, b in lane.pause_until.items()},
                "pf_ring": {str(a): b for a, b in lane.pf_ring.items()},
                "parent_pf_ring": lane.parent_pf_ring[-self.window :],
                "satisfied": {str(a): b for a, b in lane.satisfied.items()},
                "held_factor": {str(a): b for a, b in (lane.held_factor or {}).items()},
                "active": lane.active,
                "group_key": lane.group_key,
            }
        blob["countTape"] = {str(n): list(v)[-self.window :] for n, v in (self.count_tape or {}).items()}
        blob["overallTape"] = list(self.overall_tape or [])[-self.window :]
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(blob, f)
        os.replace(tmp, self.path)

    def register_parent(self, symbol: str, side: str, qty: float, entry: float, group_key: str = "") -> BlockLane:
        k = self.key(symbol, side, group_key)
        lane = self.lanes.get(k)
        if lane and lane.base_qty > 0:
            lane.active = True
            return lane
        if lane:
            # Re-entry on the same side: keep per-count tapes and last increased factor.
            lane.base_qty = float(qty)
            lane.base_entry = float(entry)
            lane.confirmed_add = 0.0
            lane.legs = []
            lane.satisfied = {}
            lane.active = True
            self.save()
            return lane
        lane = BlockLane(symbol=symbol, side=side, base_qty=qty, base_entry=entry, active=True, group_key=str(group_key or ""))
        self.lanes[k] = lane
        self.save()
        return lane

    def merge_parent(
        self,
        symbol: str,
        side: str,
        added_qty: float,
        entry: float,
        group_key: str = "",
    ) -> BlockLane:
        """Increase only a range group's parent after another entry fill.

        ``base_qty`` is the anchor for Block sizing. It must grow when two
        entries merge into one logical range group, but confirmed Block adds
        remain untouched because they are already recorded separately.
        """
        add = max(0.0, float(added_qty or 0.0))
        key = self.key(symbol, side, group_key)
        lane = self.lanes.get(key)
        if lane is None:
            return self.register_parent(symbol, side, add, entry, group_key=group_key)
        if add <= 0:
            return lane
        old_qty = max(0.0, float(lane.base_qty or 0.0))
        total = old_qty + add
        if total > 0:
            if entry > 0:
                lane.base_entry = ((lane.base_entry * old_qty) + float(entry) * add) / total
            self.refresh_parent_qty(lane, total, lane.base_entry)
            lane.active = True
            self.save()
        return lane

    def refresh_parent_qty(self, lane: BlockLane, qty: float, entry: float = 0.0) -> None:
        """Keep Overall parent size in sync. Uncover counts the new 2× cap can still fill."""
        new_qty = max(0.0, float(qty or 0.0))
        if new_qty <= 0:
            return
        lane.base_qty = new_qty
        if entry > 0:
            lane.base_entry = float(entry)
        confirmed = float(lane.confirmed_add or 0.0)
        for n in list(lane.satisfied.keys()):
            try:
                target = float(self.formula(new_qty, int(n), lane).get("targetAddQty") or 0.0)
            except Exception:
                continue
            if confirmed + 1e-12 < target:
                lane.satisfied.pop(n, None)

    def last_n_avg(self, count: int, lane: Optional[BlockLane] = None) -> Tuple[float, int]:
        """Independent last-`count` average for this block count only."""
        n = max(1, int(count))
        # Global tape already includes lane samples. Combining them both double
        # counted outcomes and allowed another symbol's results to reset a lane.
        samples = list(lane.pf_ring.get(n) or []) if lane is not None else list(self.count_tape.get(n) or [])
        samples = samples[-n:]
        if not samples:
            return 0.0, 0
        return (sum(samples) / len(samples)), len(samples)

    def overall_avg(self, lane: Optional[BlockLane] = None) -> Tuple[float, int]:
        samples = list(lane.parent_pf_ring or []) if lane is not None else list(self.overall_tape or [])
        samples = samples[-self.window :]
        if not samples:
            return 0.0, 0
        return (sum(samples) / len(samples)), len(samples)

    def vol_factor(self, count: int, lane: Optional[BlockLane] = None) -> float:
        """Increment factor vs original base. Independent per count.

        This is per-count recovery state, not another sizing multiplier. The
        count is already included in the absolute target. Only this count's
        positive result releases its held state, never overall or other lanes.
        """
        n = max(1, int(count))
        increased = float(n)
        held = 1.0
        if lane is not None:
            try:
                held = max(1.0, float((lane.held_factor or {}).get(n) or 1.0))
            except Exception:
                held = 1.0
        avg, n_avg = self.last_n_avg(n, lane)
        if held > 1.0 or (n_avg >= n and avg <= 0):
            return max(held, increased)
        return 1.0

    def vol_scale(self, count: int, lane: Optional[BlockLane] = None) -> float:
        """Back-compat alias of vol_factor (kept ≥ 1.0; no shrink-on-loss)."""
        return self.vol_factor(count, lane)

    def count_avg(self, count: int, lane: Optional[BlockLane] = None) -> Tuple[float, int]:
        return self.last_n_avg(count, lane)

    def step_qty(self, base_qty: float, count: int, lane: Optional[BlockLane] = None) -> float:
        """Marginal portion between absolute targets; held state adds no portion."""
        n = max(1, int(count))
        factor = self.vol_factor(n, lane)
        if lane is not None:
            lane.held_factor[n] = float(factor)
        return max(0.0, self.cumulative_target(base_qty, n) - self.cumulative_target(base_qty, n - 1))

    def cumulative_target(self, base_qty: float, count: int, lane: Optional[BlockLane] = None) -> float:
        return max(0.0, finite_number(base_qty)) * calculate_block_max_additional_ratio(
            count, self.effective_volume_ratio(), self.max_volume_multiplier
        )

    def formula(self, base_qty: float, count: int, lane: Optional[BlockLane] = None) -> Dict[str, float]:
        n = max(1, int(count))
        scale = self.vol_scale(n, lane)
        step = self.step_qty(base_qty, n, lane)
        target_add = self.cumulative_target(base_qty, n, lane)
        target_block = float(base_qty) + target_add
        inc = (target_add / float(base_qty)) if float(base_qty) > 0 else 0.0
        min_pf = calculate_block_minimum_profit_factor(self.default_min_pf, self.pf_ratio, inc)
        return {
            "volumeIncrement": inc,
            "stepQty": step,
            "volScale": scale,
            "targetAddQty": target_add,
            "targetBlockQty": target_block,
            "blockMinPF": min_pf,
        }

    def normal_pf(self, lane: BlockLane) -> float:
        ring = [x for x in lane.parent_pf_ring if x is not None][-self.window :]
        if len(ring) < 1:
            # Parent is already live/qualified; inherit the real-stage floor.
            return float(self.default_min_pf or POSITIVE_PF)
        return cost_pf_from_net_fracs(ring, self.cost_pct)

    def observed_pf(self, lane: BlockLane, count: int) -> Tuple[float, int]:
        need = max(1, int(self.eval_pos_count or BLOCK_EVAL_POS_DEFAULT))
        ring = (lane.pf_ring.get(count) or [])[-need:]
        if not ring:
            return self.normal_pf(lane), 0
        return cost_pf_from_net_fracs(ring, self.cost_pct), len(ring)

    def main_stage_eval(self, lane: Optional[BlockLane], count: int) -> Dict[str, Any]:
        """Stage Main last-N check for one Block count. Independent of other counts.

        Too few samples → valid for Real/Live. A proven negative stays intern-only.
        """
        need = max(1, int(self.eval_pos_count or BLOCK_EVAL_POS_DEFAULT))
        if lane is not None:
            samples = list(lane.pf_ring.get(int(count)) or [])[-need:]
        else:
            samples = list(self.count_tape.get(int(count)) or [])[-need:]
        n = len(samples)
        insufficient = n < need
        observed = cost_pf_from_net_fracs(samples, self.cost_pct) if samples else 0.0
        floor = float(self.default_min_pf or POSITIVE_PF)
        from position_cost import clears_pf, is_positive_pf
        positive = bool(samples) and is_positive_pf(observed)
        better = bool(samples) and clears_pf(observed, floor)
        live_ok = True if insufficient else (positive and better)
        return {
            "evalPosCount": need,
            "sampleCount": n,
            "insufficientSample": insufficient,
            "observedProfitFactor": observed,
            "positive": positive,
            "better": better,
            "liveOk": live_ok,
            "internOk": True,
            "internOnly": not live_ok,
            "reason": "insufficient-sample" if insufficient else ("pass" if live_ok else "main-negative"),
        }

    def pf_decision(self, lane: BlockLane, count: int, intern_pf: float = INTERN_PF) -> Dict[str, Any]:
        # Gate against the same cumulative target used by the order planner.
        # This keeps PF coordination honest when a loss-held factor changes the
        # actual volume target; count × configured ratio would understate it.
        formula = self.formula(lane.base_qty, count, lane)
        inc = float(formula.get("volumeIncrement") or 0.0)
        configured = calculate_block_minimum_profit_factor(self.default_min_pf, self.pf_ratio, inc)
        normal = self.normal_pf(lane)
        main = self.main_stage_eval(lane, count)
        intern = float(intern_pf or 0.0)
        real_floor = float(self.default_min_pf or POSITIVE_PF)
        if main["insufficientSample"]:
            # Last-N check does not deactivate. Extra size still needs intern ≥ real floor.
            observed = intern if intern > 0 else INTERN_PF
            effective = configured if count > 1 else real_floor
            passes = observed + 1e-9 >= effective and observed + 1e-9 >= real_floor
            cold = True
            intern_only = False
            live_ok = True
        else:
            observed = float(main["observedProfitFactor"])
            effective = calculate_block_effective_minimum_profit_factor(configured, normal)
            effective = max(effective, real_floor)
            intern_only = bool(main["internOnly"])
            live_ok = bool(main["liveOk"])
            passes = live_ok and not intern_only
            cold = False
        return {
            "coldStart": cold,
            "sampleCount": main["sampleCount"],
            "observedProfitFactor": observed,
            "normalProfitFactor": normal,
            "configuredMinimumProfitFactor": configured,
            "effectiveMinimumProfitFactor": effective,
            "passesProfitFactor": passes,
            "comparisonAvailable": not cold,
            "internPf": round(intern, 4),
            "evalPosCount": main["evalPosCount"],
            "insufficientSample": main["insufficientSample"],
            "liveOk": live_ok,
            "internOk": True,
            "internOnly": intern_only,
            "mainReason": main["reason"],
        }

    def unlimited(self) -> bool:
        return False

    def _count_range(self, lane: Optional[BlockLane] = None) -> List[int]:
        """Enabled counts; each count has its own PF and pause state."""
        return [n for n in self.counts if n <= self.max_stack]

    def _eval_range(self) -> range:
        """Always show six independent evaluations, including disabled counts."""
        return range(1, int(self.eval_n or BLOCK_EVAL_N) + 1)

    def next_unsatisfied(self, lane: BlockLane, stack_cap: Optional[int] = None) -> Optional[int]:
        """Smallest uncovered target (eligibility is checked independently)."""
        if not lane or lane.base_qty <= 0:
            return None
        cap = int(stack_cap) if stack_cap is not None else int(self.max_stack)
        cap = max(1, min(int(self.max_stack), cap))
        for n in range(1, cap + 1):
            if n not in self.counts:
                continue
            f = self.formula(lane.base_qty, n, lane)
            sat = bool(lane.satisfied.get(n)) or lane.confirmed_add + 1e-12 >= f["targetAddQty"]
            if not sat:
                return int(n)
        return None

    def next_order_qty(self, lane: BlockLane, count: int) -> float:
        f = self.formula(lane.base_qty, count, lane)
        return max(0.0, f["targetAddQty"] - lane.confirmed_add)

    def remainder_qty(self, lane: BlockLane, count: int) -> float:
        """Physical leftover to the sequential target. Never a fresh n× parent."""
        return self.next_order_qty(lane, count)

    def mark_nearly_filled(self, lane: BlockLane, count: int) -> None:
        """Dust remainder that cannot be placed (below min lot) is treated as filled."""
        n = int(count)
        lane.satisfied[n] = True
        for c in range(1, n):
            fc = self.formula(lane.base_qty, c, lane)
            if lane.confirmed_add + 1e-12 >= fc["targetAddQty"]:
                lane.satisfied[c] = True
        self.save()

    def evaluate_counts(self, lane: BlockLane, live_n: int, intern_pf: float = INTERN_PF, stack_cap: Optional[int] = None) -> List[Dict[str, Any]]:
        """Evaluate all six independently; order selection remains serialized."""
        rows = []
        if not self.enabled or not lane.active or lane.base_qty <= 0:
            return rows
        now = time.time()
        live_stack = int(stack_cap) if stack_cap is not None else int(self.max_stack)
        live_stack = max(1, min(int(self.max_stack), live_stack))
        nxt = self.next_unsatisfied(lane, stack_cap=live_stack)
        for n in self._eval_range():
            f = self.formula(lane.base_qty, n, lane)
            paused = lane.pause_remaining.get(n, 0) > 0 or now < lane.pause_until.get(n, 0)
            sat = bool(lane.satisfied.get(n)) or lane.confirmed_add + 1e-12 >= f["targetAddQty"]
            pf = self.pf_decision(lane, n, intern_pf=intern_pf)
            avg, n_avg = self.count_avg(n, lane)
            live_ok = n <= live_stack and n in self.counts
            requested = 0.0
            intern_only = bool(pf.get("internOnly"))
            if live_ok and not sat and not paused and pf["passesProfitFactor"] and not intern_only:
                requested = max(0.0, f["targetAddQty"] - lane.confirmed_add)
            rows.append({
                "setKey": f"{lane.symbol}:{lane.side.lower()}#block:{n}",
                "blockCount": n,
                "kind": "regular",
                "paused": paused,
                "targetSatisfied": sat,
                "requestedAddQty": requested,
                "sequential": nxt == n,
                "liveStack": live_ok,
                "independent": True,
                "avgResult": round(avg, 8),
                "avgN": n_avg,
                "heldFactor": round(float(f.get("volScale") or 1), 4),
                **f,
                **pf,
                "evaluated": 1,
                "emitted": 0,
            })
        if self.active_real and self.active_live and live_n >= 1 and nxt is not None:
            n = int(nxt)
            f = self.formula(lane.base_qty, n, lane)
            pf = self.pf_decision(lane, n, intern_pf=intern_pf)
            paused = lane.pause_remaining.get(n, 0) > 0 or now < lane.pause_until.get(n, 0)
            sat = bool(lane.satisfied.get(n)) or lane.confirmed_add + 1e-12 >= f["targetAddQty"]
            requested = 0.0 if sat or paused or not pf["passesProfitFactor"] or pf.get("internOnly") else max(0.0, f["targetAddQty"] - lane.confirmed_add)
            rows.append({
                "setKey": f"{lane.symbol}:{lane.side.lower()}#block:active:{n}",
                "blockCount": n,
                "kind": "active-live",
                "paused": paused,
                "targetSatisfied": sat,
                "requestedAddQty": requested,
                "sequential": True,
                "liveStack": True,
                "independent": True,
                **f,
                **pf,
                "evaluated": 1,
                "emitted": 0,
            })
        return rows

    def pick_emit(self, rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Choose one eligible target; a paused/failed count cannot gate others.

        Active-live is a view of the same target, never an extra order portion.
        """
        regular = [r for r in rows if r.get("kind") == "regular"]
        unsat = [r for r in regular if not r.get("targetSatisfied")
                 and r.get("liveStack") and not r.get("paused")
                 and r.get("passesProfitFactor") and not r.get("internOnly")
                 and finite_number(r.get("requestedAddQty")) > 0]
        if not unsat:
            return None
        nxt = min(unsat, key=lambda r: int(r.get("blockCount") or 99))
        if (
            float(nxt.get("requestedAddQty") or 0) > 0
            and nxt.get("passesProfitFactor")
            and not nxt.get("paused")
        ):
            return nxt
        return None

    def record_fill(self, lane: BlockLane, row: Dict[str, Any], filled: float, cid: str, oid: str) -> None:
        n = int(row["blockCount"])
        f = self.formula(lane.base_qty, n, lane)
        before = lane.confirmed_add
        lane.confirmed_add += filled
        sat = lane.confirmed_add + 1e-12 >= f["targetAddQty"]
        lane.satisfied[n] = sat
        # lower counts already covered
        for c in range(1, n):
            fc = self.formula(lane.base_qty, c, lane)
            if lane.confirmed_add + 1e-12 >= fc["targetAddQty"]:
                lane.satisfied[c] = True
        lane.legs.append(
            BlockLeg(
                set_key=row["setKey"],
                block_count=n,
                quantity=filled,
                base_quantity=lane.base_qty,
                volume_ratio=self.effective_volume_ratio(),
                volume_increment_ratio=f["volumeIncrement"],
                target_additional_quantity=f["targetAddQty"],
                confirmed_additional_quantity_before=before,
                target_block_quantity=f["targetBlockQty"],
                target_satisfied=sat,
                requested_quantity=row["requestedAddQty"],
                pause_count=self.pause_ratio,
                client_order_id=cid,
                order_id=oid,
                added_at=time.time(),
                scope=lane.side.lower(),
            )
        )
        self.save()

    def pause_count(self, lane: BlockLane, n: int, seconds: float = 120.0) -> None:
        """Halt a count after an exchange hard-fail (max position / size). Independent of PF pause."""
        n = int(n)
        lane.pause_until[n] = time.time() + max(8.0, float(seconds))
        lane.pause_remaining[n] = max(int(lane.pause_remaining.get(n, 0)), max(1, self.pause_ratio))
        self.save()

    def on_parent_close(self, symbol: str, side: str, pnl: float, pnl_pct: Optional[float] = None, group_key: str = "") -> None:
        k = self.key(symbol, side, group_key)
        lane = self.lanes.get(k)
        if not lane:
            return
        # Prefer cost-net fraction so PF is size-independent and PositionCost-aware.
        sample = float(pnl_pct) if pnl_pct is not None else float(pnl)
        lane.parent_pf_ring.append(sample)
        lane.parent_pf_ring = lane.parent_pf_ring[-self.window :]
        self.overall_tape.append(sample)
        self.overall_tape = self.overall_tape[-self.window :]
        # advance every existing pause once — each count independently
        for n, rem in list(lane.pause_remaining.items()):
            if rem > 0:
                lane.pause_remaining[n] = rem - 1
        used_counts = {int(leg.block_count) for leg in lane.legs if int(leg.block_count or 0) >= 1}
        for n in self._eval_range():
            if n not in used_counts:
                continue
            lane.pf_ring.setdefault(n, []).append(sample)
            lane.pf_ring[n] = lane.pf_ring[n][-self.window :]
            self.count_tape.setdefault(n, []).append(sample)
            self.count_tape[n] = self.count_tape[n][-self.window :]
            if sample > 0:
                lane.pause_remaining[n] = self.pause_ratio
                lane.pause_until[n] = time.time() + 45 * self.pause_ratio
                lane.held_factor[n] = 1.0
            else:
                lane.held_factor[n] = max(float(n), lane.held_factor.get(n, 1.0))
        lane.active = False
        lane.confirmed_add = 0.0
        lane.legs = []
        lane.satisfied = {}
        lane.base_qty = 0.0
        self.save()

    def snapshot(self, intern_pf_lookup=None) -> Dict[str, Any]:
        lanes = []
        for lane in self.lanes.values():
            if not lane.active and not lane.legs:
                continue
            intern = float(self.default_min_pf or POSITIVE_PF)
            if callable(intern_pf_lookup):
                try:
                    intern = float(intern_pf_lookup(lane) or 0.0)
                except Exception:
                    intern = 0.0
            rows = self.evaluate_counts(lane, live_n=1 if lane.active else 0, intern_pf=intern)
            lanes.append({
                "symbol": lane.symbol,
                "side": lane.side,
                "groupKey": lane.group_key,
                "baseQty": lane.base_qty,
                "confirmedAdd": round(lane.confirmed_add, 8),
                "aggregate": round(lane.base_qty + lane.confirmed_add, 8),
                "legs": [asdict(x) for x in lane.legs[-8:]],
                "counts": [
                    {
                        "n": r["blockCount"],
                        "kind": r["kind"],
                        "inc": r["volumeIncrement"],
                        "stepQty": round(float(r.get("stepQty") or 0), 8),
                        "volScale": round(float(r.get("volScale") or 1), 4),
                        "heldFactor": round(float(r.get("heldFactor") or r.get("volScale") or 1), 4),
                        "avgResult": r.get("avgResult"),
                        "avgN": r.get("avgN"),
                        "targetAdd": round(r["targetAddQty"], 8),
                        "requested": round(r["requestedAddQty"], 8),
                        "minPF": round(r["blockMinPF"], 4),
                        "obsPF": round(r["observedProfitFactor"], 4),
                        "internPf": round(float(r.get("internPf") or 0), 4),
                        "pass": r["passesProfitFactor"],
                        "paused": r["paused"],
                        "satisfied": r["targetSatisfied"],
                        "cold": r["coldStart"],
                        "liveStack": r.get("liveStack"),
                        "independent": True,
                    }
                    for r in rows if r["kind"] == "regular"
                ],
            })
        catalog = []
        vr_eff = self.effective_volume_ratio()
        for n in self._eval_range():
            f = self.formula(1.0, n)
            avg, n_avg = self.count_avg(n)
            catalog.append({
                "n": n,
                "inc": f["volumeIncrement"],
                "stepQty": round(float(f.get("stepQty") or 0), 8),
                "volScale": round(float(f.get("volScale") or 1), 4),
                "heldFactor": round(float(f.get("volScale") or 1), 4),
                "avgResult": round(avg, 8),
                "avgN": n_avg,
                "targetAdd": round(f["targetAddQty"], 8),
                "targetBlock": round(f["targetBlockQty"], 8),
                "minPF": round(f["blockMinPF"], 4),
                "liveStack": n <= int(self.max_stack) and n in self.counts,
                "independent": True,
            })
        return {
            "enabled": self.enabled,
            "maxStack": self.max_stack,
            "evalN": int(self.eval_n or BLOCK_EVAL_N),
            "countN": len(catalog),
            "allCounts": catalog,
            "volumeRatio": vr_eff,
            "configuredVolumeRatio": self.volume_ratio,
            "maxVolumeMultiplier": self.max_volume_multiplier,
            "enabledCounts": self.counts,
            "profitFactorRatio": self.pf_ratio,
            "pauseCountRatio": self.pause_ratio,
            "activeLive": self.active_live,
            "activeReal": self.active_real,
            "evalPosCount": int(self.eval_pos_count or BLOCK_EVAL_POS_DEFAULT),
            "defaultMinPF": self.default_min_pf,
            "lanes": lanes,
        }


def self_test() -> List[Tuple[str, bool, str]]:
    import tempfile
    out: List[Tuple[str, bool, str]] = []
    tmp = tempfile.mkdtemp(prefix="block-self-")

    def rec(name: str, ok: bool, detail: str = "") -> None:
        out.append((name, bool(ok), str(detail)[:220]))

    rec("blk-inc-n1", calculate_block_volume_increment_ratio(1, 1.0) == 1.0)
    rec("blk-inc-n3", calculate_block_volume_increment_ratio(3, 1.0) == 3.0)
    rec("blk-inc-vr15", abs(calculate_block_volume_increment_ratio(2, 1.5) - 3.0) < 1e-9)
    rec("blk-mult-n3", calculate_block_volume_multiplier(3, 1.0) == 4.0)
    rec("blk-max-add-sequential", calculate_block_max_additional_ratio(3, 1.0) == 1.0, "not 1+2+3=6")
    rec("blk-max-add-vr", abs(calculate_block_max_additional_ratio(3, 1.5) - 1.0) < 1e-9)
    rec("blk-pf-n1", abs(calculate_block_minimum_profit_factor(1.1, 1.1, 1.0) - 1.11) < 1e-9,
        str(calculate_block_minimum_profit_factor(1.1, 1.1, 1.0)))
    rec("blk-pf-n3", abs(calculate_block_minimum_profit_factor(1.1, 1.1, 3.0) - 1.33) < 1e-9,
        str(calculate_block_minimum_profit_factor(1.1, 1.1, 3.0)))
    rec("blk-parse-active", parse_block_count("sol-usdt:long#block:active:2") == 2)
    rec("blk-parse-set", parse_block_count("xrp-usdt:short#block:set:3") == 3)
    rec("blk-parse-plain", parse_block_count("aaa:long#block:1") == 1)
    rec("blk-parse-none", parse_block_count("general:1m:sl0.6") is None)
    rec("blk-share-n1-eats-cap", abs(shared_block_volume_ratio(1.0, 3, 1.0) - (1.0 / 3.0)) < 1e-12)
    rec("blk-share-preview-not-stack", abs(shared_block_volume_ratio(1.0, 6, 1.0) - (1.0 / 6.0)) < 1e-12)
    rec("blk-share-keeps-025", abs(shared_block_volume_ratio(0.25, 6, 1.0) - 0.25) < 1e-12)
    rec("blk-share-single-keeps", abs(shared_block_volume_ratio(1.0, 1, 1.0) - 1.0) < 1e-12)

    b = BlockBook(os.path.join(tmp, "main.json"), {
        "variantBlockEnabled": True, "blockMaxStack": 3, "blockVolumeRatio": 0.25,
        "blockProfitFactorRatio": 1.1, "defaultMinPF": POSITIVE_PF,
        "blockActiveRealEnabled": True, "blockActiveLiveEnabled": True,
    })
    rec("blk-zero-remap", BlockBook(os.path.join(tmp, "z.json"), {"blockMaxStack": 0}).max_stack == 6)
    rec("blk-not-unlimited", not b.unlimited())
    rec("blk-025-not-shared", abs(b.effective_volume_ratio() - 0.25) < 1e-12, str(b.effective_volume_ratio()))

    long = b.register_parent("SOL-USDT", "LONG", 10.0, 100.0)
    short = b.register_parent("SOL-USDT", "SHORT", 8.0, 100.0)
    rec("blk-lanes-independent", b.key("SOL-USDT", "LONG") != b.key("SOL-USDT", "SHORT")
        and long.base_qty == 10.0 and short.base_qty == 8.0, f"L={long.base_qty} S={short.base_qty}")

    pick = b.pick_emit(b.evaluate_counts(long, live_n=3, intern_pf=1.5))
    rec("blk-no-jump-liven3", pick is not None and pick["blockCount"] == 1
        and abs(pick["requestedAddQty"] - 2.5) < 1e-9,
        f"n={pick and pick.get('blockCount')} qty={pick and pick.get('requestedAddQty')}")
    rec("blk-next-unsat-1", b.next_unsatisfied(long) == 1)

    rows = b.evaluate_counts(long, live_n=3, intern_pf=1.5)
    act = [r for r in rows if r["kind"] == "active-live"]
    rec("blk-active-clipped", len(act) == 1 and act[0]["blockCount"] == 1
        and abs(act[0]["requestedAddQty"] - 2.5) < 1e-9,
        f"act={act[0] if act else None}")

    # remainder: fill n=1 then next is n=2 requesting 1× parent
    b.record_fill(long, pick, 2.5, "c1", "o1")
    rec("blk-n1-satisfied", bool(long.satisfied.get(1)) and abs(long.confirmed_add - 2.5) < 1e-9)
    pick2 = b.pick_emit(b.evaluate_counts(long, live_n=1, intern_pf=1.5))
    rec("blk-n2-remainder-1x", pick2 is not None and pick2["blockCount"] == 2
        and abs(pick2["requestedAddQty"] - 2.5) < 1e-9,
        f"n={pick2 and pick2.get('blockCount')} qty={pick2 and pick2.get('requestedAddQty')}")
    b.record_fill(long, pick2, 2.5, "c2", "o2")
    rec("blk-n1-stays-sat-after-n2", bool(long.satisfied.get(1)) and bool(long.satisfied.get(2)))
    pick3 = b.pick_emit(b.evaluate_counts(long, live_n=1, intern_pf=1.5))
    rec("blk-n3-remainder-1x", pick3 is not None and pick3["blockCount"] == 3
        and abs(pick3["requestedAddQty"] - 2.5) < 1e-9,
        f"n={pick3 and pick3.get('blockCount')} qty={pick3 and pick3.get('requestedAddQty')}")
    rec("blk-agg-before-n3", abs(long.base_qty + long.confirmed_add - 15.0) < 1e-9, str(long.base_qty + long.confirmed_add))
    b.record_fill(long, pick3, 2.5, "c3", "o3")
    rec("blk-stack-full", b.next_unsatisfied(long) is None and b.pick_emit(b.evaluate_counts(long, live_n=1, intern_pf=1.5)) is None)
    rec("blk-agg-4x", abs(long.base_qty + long.confirmed_add - 17.5) < 1e-9, str(long.base_qty + long.confirmed_add))
    rec("blk-remainder-qty", abs(b.remainder_qty(long, 3) - 0.0) < 1e-9)
    dust = BlockLane(symbol="DUST-USDT", side="LONG", base_qty=10.0, base_entry=1.0, confirmed_add=9.99)
    b.mark_nearly_filled(dust, 1)
    rec("blk-dust-sat", bool(dust.satisfied.get(1)), str(dust.satisfied))

    # SHORT lane is untouched by LONG fills
    rec("blk-short-untouched", short.confirmed_add == 0.0 and b.next_unsatisfied(short) == 1)
    sp = b.pick_emit(b.evaluate_counts(short, live_n=1, intern_pf=1.5))
    rec("blk-short-n1-own-base", sp is not None and abs(sp["requestedAddQty"] - 2.0) < 1e-9,
        f"qty={sp and sp.get('requestedAddQty')}")

    # intern too low for n=2 must NOT skip to n=3
    lane2 = BlockLane(symbol="AAA-USDT", side="LONG", base_qty=10.0, base_entry=100.0, confirmed_add=2.5, satisfied={1: True})
    # n=2 gate = 1.22, intern 1.15 fails; n=3 gate 1.33 also fails
    rec("blk-no-skip-failed-rung", b.pick_emit(b.evaluate_counts(lane2, live_n=1, intern_pf=1.015)) is None)

    # pause blocks the next count only
    lane3 = BlockLane(symbol="BBB-USDT", side="SHORT", base_qty=5.0, base_entry=50.0)
    b.pause_count(lane3, 1, 600)
    rec("blk-pause-blocks", b.pick_emit(b.evaluate_counts(lane3, live_n=1, intern_pf=1.5))["blockCount"] == 2)

    rec("blk-disabled", BlockBook(os.path.join(tmp, "off.json"), {"variantBlockEnabled": False}).evaluate_counts(long, 1, 1.5) == [])
    rec("blk-no-base", b.evaluate_counts(BlockLane("X", "LONG", 0.0, 1.0), 1, 1.5) == [])

    # cost-net PF: losing ring (net frac) blocks, winning ring passes
    lane4 = BlockLane(symbol="PF-USDT", side="LONG", base_qty=1.0, base_entry=100.0, confirmed_add=0.25, satisfied={1: True})
    lane4.pf_ring[2] = [-0.0045] * 50  # 50 losing samples, Main last-N
    lane4.parent_pf_ring = [-0.0045] * 50
    d_loss = b.pf_decision(lane4, 2, intern_pf=1.5)
    rec("blk-warm-loss-blocks", d_loss["passesProfitFactor"] is False and d_loss["coldStart"] is False,
        str(d_loss))
    lane4.pf_ring[2] = [0.003] * 50
    lane4.parent_pf_ring = [0.003] * 50
    d_win = b.pf_decision(lane4, 2, intern_pf=1.0)
    rec("blk-warm-win-passes", d_win["passesProfitFactor"] is True and d_win["observedProfitFactor"] + 1e-9 >= POSITIVE_PF,
        str(d_win))

    intern_lane = BlockLane(symbol="INTERN-USDT", side="LONG", base_qty=10.0, base_entry=100.0)
    d_intern = b.pf_decision(intern_lane, 1, intern_pf=INTERN_PF)
    rec("blk-intern-1.00-not-real", d_intern["passesProfitFactor"] is False and d_intern["coldStart"] is True,
        str(d_intern))
    d_real = b.pf_decision(intern_lane, 1, intern_pf=POSITIVE_PF)
    rec("blk-real-1.15-n1-cold", d_real["passesProfitFactor"] is True and d_real["coldStart"] is True,
        str(d_real))
    cost_lane = BlockLane(symbol="COST-USDT", side="LONG", base_qty=10.0, base_entry=100.0)
    cost_lane.pf_ring[1] = [0.001] * 50  # +0.10% net = +1.0R at 0.10% cost → 1.10
    cost_lane.parent_pf_ring = [0.001] * 50
    d_eq = b.pf_decision(cost_lane, 1, intern_pf=INTERN_PF)
    rec("blk-cost-pf-1R-is-1.10", abs(d_eq["observedProfitFactor"] - 1.10) < 1e-9,
        str(d_eq))
    rec("blk-default-min-is-real", abs(float(b.default_min_pf) - POSITIVE_PF) < 1e-9, str(b.default_min_pf))
    rec("blk-cost-pf-matches-r", abs(cost_pf_from_net_fracs([0.001] * 5) - 1.10) < 1e-9)
    rec("blk-cost-pf-015", abs(cost_pf_from_net_fracs([0.0015] * 5, 0.15) - 1.10) < 1e-9,
        str(cost_pf_from_net_fracs([0.0015] * 5, 0.15)))
    rec("blk-cost-pf-floor", abs(cost_pf_from_net_fracs([0.0015] * 5) - POSITIVE_PF) < 1e-9,
        str(cost_pf_from_net_fracs([0.0015] * 5)))
    rec("blk-cost-pf-empty", cost_pf_from_net_fracs([]) == 0.0)

    # PF gates must use the same cumulative target as volume planning when a
    # loss-held factor changes a rung; count×ratio would understate the gate.
    held_lane = BlockLane(symbol="HELD-USDT", side="LONG", base_qty=10.0, base_entry=100.0)
    held_lane.pf_ring[2] = [-0.01] * 5
    held_lane.parent_pf_ring = [-0.01] * 8
    held_lane.held_factor[2] = 2.0
    held_decision = b.pf_decision(held_lane, 2, intern_pf=1.5)
    rec("blk-pf-uses-actual-target", abs(held_decision["configuredMinimumProfitFactor"] - (1 + (POSITIVE_PF - 1) * 1.1 * 0.5)) < 1e-9,
        str(held_decision))
    rec("blk-formula-minpf-is-inc",
        abs(b.formula(10.0, 2)["blockMinPF"] - held_decision["configuredMinimumProfitFactor"]) < 1e-9)

    # Overlay-like vr=1 / stack=3 must share extra so n=2,3 have non-zero step.
    ov = BlockBook(os.path.join(tmp, "ov.json"), {
        "variantBlockEnabled": True, "blockMaxStack": 3, "blockVolumeRatio": 1.0,
        "blockProfitFactorRatio": 1.1, "defaultMinPF": POSITIVE_PF,
    })
    rec("blk-ov-share-live-3", abs(ov.effective_volume_ratio() - (1.0 / 3.0)) < 1e-12
        and ov.volume_ratio == 1.0, str(ov.effective_volume_ratio()))
    steps = [ov.step_qty(10.0, n) for n in range(1, 4)]
    rec("blk-ov-all-live-steps", all(s > 1e-12 for s in steps) and abs(sum(steps) - 10.0) < 1e-9,
        str(steps))
    rec("blk-ov-n3-hits-2x", abs(ov.formula(10.0, 3)["targetBlockQty"] - 20.0) < 1e-9,
        str(ov.formula(10.0, 3)))
    rec("blk-ov-n4-not-live", ov.formula(10.0, 4)["stepQty"] == 0.0 or 4 not in ov.live_counts())
    lane_ov = ov.register_parent("OV-USDT", "LONG", 10.0, 100.0)
    filled = 0.0
    for n in (1, 2, 3):
        pick_n = ov.pick_emit(ov.evaluate_counts(lane_ov, live_n=1, intern_pf=1.5))
        rec(f"blk-ov-seq-n{n}", pick_n is not None and int(pick_n["blockCount"]) == n
            and abs(float(pick_n["requestedAddQty"]) - (10.0 / 3.0)) < 1e-9,
            f"n={pick_n and pick_n.get('blockCount')} qty={pick_n and pick_n.get('requestedAddQty')}")
        if pick_n:
            ov.record_fill(lane_ov, pick_n, float(pick_n["requestedAddQty"]), f"ov{n}", f"o{n}")
            filled += float(pick_n["requestedAddQty"])
    rec("blk-ov-seq-2x", abs(filled - 10.0) < 1e-9 and abs(lane_ov.base_qty + lane_ov.confirmed_add - 20.0) < 1e-9
        and ov.next_unsatisfied(lane_ov) is None, f"add={lane_ov.confirmed_add}")

    # vr=2 / 6 counts: share extra/6, never exceed 2×, every live step > 0.
    fat = BlockBook(os.path.join(tmp, "fat.json"), {
        "blockMaxStack": 6, "blockVolumeRatio": 2.0, "defaultMinPF": POSITIVE_PF,
    })
    rec("blk-fat-share-6", abs(fat.effective_volume_ratio() - (1.0 / 6.0)) < 1e-12, str(fat.effective_volume_ratio()))
    rec("blk-fat-all-steps", all(fat.step_qty(12.0, n) > 1e-12 for n in range(1, 7))
        and abs(fat.formula(12.0, 6)["targetBlockQty"] - 24.0) < 1e-9)

    # parent close isolates sides + stores cost-net fraction
    b.on_parent_close("SOL-USDT", "LONG", 1.5, pnl_pct=0.0015)
    rec("blk-close-long-retires", (b.lanes["SOL-USDT:LONG"].active is False) and b.lanes["SOL-USDT:LONG"].base_qty == 0.0)
    rec("blk-close-short-alive", b.lanes["SOL-USDT:SHORT"].active is True and b.lanes["SOL-USDT:SHORT"].base_qty == 8.0)
    rec("blk-close-stores-net", abs(b.lanes["SOL-USDT:LONG"].parent_pf_ring[-1] - 0.0015) < 1e-9,
        str(b.lanes["SOL-USDT:LONG"].parent_pf_ring[-1:]))

    rec("blk-freeze-parent", b.register_parent("SOL-USDT", "SHORT", 99.0, 1.0).base_qty == 8.0)
    rec("blk-active-inc-default", abs(b.active_increment() - 0.25) < 1e-12)
    fat_inc = BlockBook(os.path.join(tmp, "act.json"), {"blockVolumeRatio": 2.0, "blockMaxStack": 6})
    rec("blk-active-inc-full-ratio", abs(fat_inc.active_increment() - 1.0) < 1e-12 and abs(fat_inc.specified_volume_ratio() - 2.0) < 1e-12)
    rec("blk-active-inc-ignores-count", abs(fat_inc.active_increment(6) - fat_inc.active_increment(1)) < 1e-12)

    rec("blk-eval-6", len([r for r in b.evaluate_counts(short, 1, 1.5) if r["kind"] == "regular"]) == 6)
    rec("blk-live-stack-3", all((r["requestedAddQty"] == 0 or r["blockCount"] <= 3) for r in b.evaluate_counts(short, 1, 1.5) if r["kind"] == "regular"))
    rec("blk-clamp-6", clamp_stack(9) == 6 and clamp_stack(0) == 6 and clamp_stack(1) == 1)
    rec("blk-clamp-book", BlockBook(os.path.join(tmp, "c6.json"), {"blockMaxStack": 12}).max_stack == 6)
    snap = b.snapshot()
    rec("blk-snap-6", int(snap.get("countN") or 0) == 6 and int(snap.get("evalN") or 0) == 6, str(snap.get("countN")))
    rec("blk-snap-live-flag", sum(1 for c in snap.get("allCounts") or [] if c.get("liveStack")) == 3)
    rec("blk-snap-configured-ratio", abs(float(snap.get("configuredVolumeRatio") or 0) - 0.25) < 1e-12)

    scale_lane = BlockLane(symbol="SC-USDT", side="LONG", base_qty=10.0, base_entry=100.0)
    scale_lane.pf_ring[1] = [-0.01] * 1
    b.count_tape[1] = [-0.01] * 1
    rec("blk-hold-n1-keep", abs(b.vol_factor(1, scale_lane) - 1.0) < 1e-9, str(b.vol_factor(1, scale_lane)))
    rec("blk-no-shrink", abs(b.step_qty(10.0, 1, scale_lane) - 2.5) < 1e-9, str(b.step_qty(10.0, 1, scale_lane)))
    scale_lane.pf_ring[3] = [-0.01] * 3
    b.count_tape[3] = [-0.01] * 3
    rec("blk-hold-n3-increased", abs(b.vol_factor(3, scale_lane) - 3.0) < 1e-9, str(b.vol_factor(3, scale_lane)))
    rec("blk-hold-n3-vs-base", abs(b.step_qty(10.0, 3, scale_lane) - 2.5) < 1e-9, str(b.step_qty(10.0, 3, scale_lane)))
    rec("blk-n2-indep-scale", abs(b.vol_factor(2, scale_lane) - 1.0) < 1e-9, str(b.vol_factor(2, scale_lane)))
    scale_lane.held_factor[3] = 1.0  # a positive settlement clears only this count
    scale_lane.pf_ring[3] = [0.004] * 3
    b.count_tape[3] = [0.004] * 3
    scale_lane.parent_pf_ring = [-0.01] * 8
    b.overall_tape = [-0.01] * 8
    rec("blk-count-pos-overall-neg-release-if-count-ok", abs(b.vol_factor(3, scale_lane) - 1.0) < 1e-9, str(b.vol_factor(3, scale_lane)))
    scale_lane.pf_ring[3] = [-0.01] * 3
    b.count_tape[3] = [-0.01] * 3
    rec("blk-hold-until-overall", abs(b.vol_factor(3, scale_lane) - 3.0) < 1e-9, str(b.vol_factor(3, scale_lane)))
    scale_lane.parent_pf_ring = [0.004] * 8
    b.overall_tape = [0.004] * 8
    rec("blk-other-results-cannot-release", abs(b.vol_factor(3, scale_lane) - 3.0) < 1e-9, str(b.vol_factor(3, scale_lane)))
    rec("blk-step-restore-base", abs(b.step_qty(10.0, 1, scale_lane) - 2.5) < 1e-9, str(b.step_qty(10.0, 1, scale_lane)))
    # Cont/count-pos stack cap: live emit only 1..cap, evals still 1..12
    cap_lane = BlockLane("CAP-USDT", "LONG", 10.0, 100.0)
    cap_rows = b.evaluate_counts(cap_lane, live_n=1, intern_pf=2.0, stack_cap=1)
    live_req = [r for r in cap_rows if r.get("kind") == "regular" and float(r.get("requestedAddQty") or 0) > 0]
    rec("blk-stack-cap-1", all(int(r["blockCount"]) == 1 for r in live_req) and len(live_req) >= 1, str([(r["blockCount"], r.get("requestedAddQty")) for r in live_req]))
    rec("blk-stack-cap-evals-6", sum(1 for r in cap_rows if r.get("kind") == "regular") == 6, str(sum(1 for r in cap_rows if r.get("kind") == "regular")))

    # Merge parent grows the sizing anchor, not confirmed adds.
    merged = BlockBook(os.path.join(tmp, "merge.json"), {"blockMaxStack": 3, "blockVolumeRatio": 0.25, "defaultMinPF": POSITIVE_PF})
    m_lane = merged.register_parent("MRG-USDT", "LONG", 10.0, 100.0)
    merged.merge_parent("MRG-USDT", "LONG", 5.0, 110.0)
    rec("blk-merge-qty", abs(m_lane.base_qty - 15.0) < 1e-12 and abs(m_lane.confirmed_add) < 1e-12)
    rec("blk-merge-entry", abs(m_lane.base_entry - ((100.0 * 10 + 110.0 * 5) / 15.0)) < 1e-9, str(m_lane.base_entry))
    rec("blk-merge-targets-scale", abs(merged.formula(m_lane.base_qty, 1)["targetAddQty"] - 3.75) < 1e-9)

    # minPF for vr>cap uses the capped increment, not the raw ratio.
    wide = BlockBook(os.path.join(tmp, "wide.json"), {
        "blockMaxStack": 1, "blockVolumeRatio": 2.0, "blockCounts": [1],
        "blockProfitFactorRatio": 1.1, "defaultMinPF": POSITIVE_PF,
    })
    rec("blk-wide-inc-capped", abs(wide.formula(10.0, 1)["volumeIncrement"] - 1.0) < 1e-12)
    rec("blk-wide-minpf-uses-inc", abs(wide.formula(10.0, 1)["blockMinPF"] - (1 + (POSITIVE_PF - 1) * 1.1 * 1.0)) < 1e-9,
        str(wide.formula(10.0, 1)["blockMinPF"]))
    rec("blk-wide-pf-matches-formula",
        abs(wide.pf_decision(BlockLane("W", "LONG", 10.0, 1.0), 1, intern_pf=1.5)["configuredMinimumProfitFactor"]
            - wide.formula(10.0, 1)["blockMinPF"]) < 1e-12)

    main = BlockBook(os.path.join(tmp, "main.json"), {
        "blockMaxStack": 6, "blockVolumeRatio": 0.25, "defaultMinPF": POSITIVE_PF,
        "blockEvalPosCount": 50,
    })
    rec("blk-eval-pos-50", main.eval_pos_count == 50, str(main.eval_pos_count))
    rec("blk-eval-pos-clamp", clamp_eval_pos_count(2) == 5 and clamp_eval_pos_count(99) == 75)
    cold_lane = BlockLane("MN-USDT", "LONG", 10.0, 100.0)
    cold = main.main_stage_eval(cold_lane, 3)
    rec("blk-main-insufficient-valid", cold["liveOk"] is True and cold["insufficientSample"] is True, str(cold))
    rec("blk-main-cold-emits", main.pf_decision(cold_lane, 3, intern_pf=1.5)["passesProfitFactor"] is True)
    rec("blk-main-cold-intern-floor", main.pf_decision(cold_lane, 3, intern_pf=1.0)["passesProfitFactor"] is False)
    win_lane = BlockLane("WN-USDT", "LONG", 10.0, 100.0)
    win_lane.pf_ring[3] = [0.004] * 50
    main.count_tape[3] = [0.004] * 50
    lose_lane = BlockLane("LS-USDT", "LONG", 10.0, 100.0)
    lose_lane.pf_ring[2] = [-0.01] * 50
    lose_lane.pf_ring[3] = [0.004] * 50
    main.count_tape[2] = [-0.01] * 50
    win = main.main_stage_eval(win_lane, 3)
    lose2 = main.main_stage_eval(lose_lane, 2)
    win3 = main.main_stage_eval(lose_lane, 3)
    rec("blk-main-win-live", win["liveOk"] is True and win["internOnly"] is False and win["observedProfitFactor"] > 1.1, str(win))
    rec("blk-main-lose-intern", lose2["liveOk"] is False and lose2["internOnly"] is True and lose2["internOk"] is True, str(lose2))
    rec("blk-main-count-indep", win3["liveOk"] is True and lose2["liveOk"] is False, f"c2={lose2['reason']} c3={win3['reason']}")
    lose_rows = main.evaluate_counts(lose_lane, live_n=1, intern_pf=1.5)
    lose_emit = [r for r in lose_rows if r.get("kind") == "regular" and float(r.get("requestedAddQty") or 0) > 0]
    rec("blk-main-no-live-n2", all(int(r["blockCount"]) != 2 for r in lose_emit), str([(r["blockCount"], r.get("internOnly"), r.get("requestedAddQty")) for r in lose_rows if r.get("kind")=="regular"]))
    rec("blk-main-snap-evaln", int((main.snapshot() or {}).get("evalPosCount") or 0) == 50)
    return out


if __name__ == "__main__":
    rows = self_test()
    bad = 0
    for name, ok, detail in rows:
        print(("PASS" if ok else "FAIL"), name, detail)
        bad += int(not ok)
    print("block_engine", "ok" if not bad else f"fail={bad}")
    raise SystemExit(1 if bad else 0)
