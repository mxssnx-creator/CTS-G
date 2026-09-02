"""CTS Block strategy — formulas and lifecycle as in BLOCK_STRATEGY_SYSTEM.md / block-count-state.ts."""
from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

BLOCK_COUNT_MIN = 1
BLOCK_COUNT_PREVIEW = 12
BLOCK_VOL_RATIO_MIN = 0.25
BLOCK_VOL_RATIO_MAX = 3.0
# Base-1 PF coordination (position_cost): 1.00=neutral, 0.10=1×PositionCost.
# Floor moved 0.2 -> 0.5 in the same +0.3 relation as the 0.8 -> 1.1 default.
BLOCK_PF_RATIO_MIN = 0.5
BLOCK_PF_RATIO_MAX = 5.0


def clamp(n: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, n))


def parse_block_count(set_key: str) -> Optional[int]:
    import re
    m = re.search(r"#block:(?:(?:active|set):)?(\d+)(?:$|[#:_-])", str(set_key or ""), re.I)
    if not m:
        return None
    c = int(m.group(1))
    return c if c >= BLOCK_COUNT_MIN else None


def calculate_block_volume_increment_ratio(block_count: int, volume_ratio: float) -> float:
    if block_count <= 0 or volume_ratio <= 0:
        return 0.0
    return int(block_count) * volume_ratio


def calculate_block_volume_multiplier(block_count: int, volume_ratio: float) -> float:
    if block_count <= 0 or volume_ratio <= 0:
        return 0.0
    return 1 + int(block_count) * volume_ratio


def calculate_block_max_additional_ratio(max_stack: int, volume_ratio: float) -> float:
    """Sequential remainder: max additional = stack × volume_ratio, not 1+2+…+N."""
    n = max(0, int(max_stack or 0))
    vr = max(0.0, float(volume_ratio or 0))
    return n * vr


def calculate_block_minimum_profit_factor(
    default_min_pf: float, block_pf_ratio: float, volume_increment: float
) -> float:
    if min(default_min_pf, block_pf_ratio, volume_increment) <= 0:
        return 0.0
    bounded = clamp(block_pf_ratio, BLOCK_PF_RATIO_MIN, BLOCK_PF_RATIO_MAX)
    return 1 + max(0.0, default_min_pf - 1) * bounded * volume_increment


def calculate_block_effective_minimum_profit_factor(configured: float, normal: float) -> float:
    return max(configured if configured > 0 else 0.0, normal if normal > 0 else 0.0)


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
    active: bool = True


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
        # 0 = default stack of 3. Never unbounded pyramiding on a single parent.
        self.max_stack = 3 if stack_n <= 0 else max(1, stack_n)
        self.volume_ratio = clamp(float(cfg.get("blockVolumeRatio", 1) or 1), 0.25, 3.0)
        self.pf_ratio = clamp(float(cfg.get("blockProfitFactorRatio", 1.1) or 1.1), BLOCK_PF_RATIO_MIN, BLOCK_PF_RATIO_MAX)
        self.pause_ratio = max(0, int(cfg.get("blockPauseCountRatio", 1) or 1))
        self.active_real = bool(cfg.get("blockActiveRealEnabled", True))
        self.active_live = bool(cfg.get("blockActiveLiveEnabled", True))
        self.default_min_pf = float(cfg.get("defaultMinPF", 1.1) or 1.1)
        self.min_samples = max(1, int(cfg.get("prevPosMinCount", 5) or 5))
        self.window = max(self.min_samples, int(cfg.get("prevPosWindow", 25) or 25))
        self.lanes: Dict[str, BlockLane] = {}
        self.load()

    def key(self, symbol: str, side: str) -> str:
        return f"{symbol}:{side}"

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            raw = json.load(open(self.path))
        except Exception:
            return
        for k, v in (raw.get("lanes") or {}).items():
            legs = [BlockLeg(**leg) for leg in v.get("legs") or []]
            lane = BlockLane(
                symbol=v["symbol"],
                side=v["side"],
                base_qty=float(v.get("base_qty") or 0),
                base_entry=float(v.get("base_entry") or 0),
                confirmed_add=float(v.get("confirmed_add") or 0),
                legs=legs,
                pause_remaining={int(a): int(b) for a, b in (v.get("pause_remaining") or {}).items()},
                pause_until={int(a): float(b) for a, b in (v.get("pause_until") or {}).items()},
                pf_ring={int(a): list(b) for a, b in (v.get("pf_ring") or {}).items()},
                parent_pf_ring=list(v.get("parent_pf_ring") or []),
                satisfied={int(a): bool(b) for a, b in (v.get("satisfied") or {}).items()},
                active=bool(v.get("active", True)),
            )
            self.lanes[k] = lane

    def save(self) -> None:
        blob = {
            "cfg": {
                "variantBlockEnabled": self.enabled,
                "blockMaxStack": self.max_stack,
                "blockVolumeRatio": self.volume_ratio,
                "blockProfitFactorRatio": self.pf_ratio,
                "blockPauseCountRatio": self.pause_ratio,
                "blockActiveRealEnabled": self.active_real,
                "blockActiveLiveEnabled": self.active_live,
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
                "active": lane.active,
            }
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(blob, f)
        os.replace(tmp, self.path)

    def register_parent(self, symbol: str, side: str, qty: float, entry: float) -> BlockLane:
        k = self.key(symbol, side)
        lane = self.lanes.get(k)
        if lane and lane.base_qty > 0:
            lane.active = True
            return lane
        lane = BlockLane(symbol=symbol, side=side, base_qty=qty, base_entry=entry, active=True)
        self.lanes[k] = lane
        self.save()
        return lane

    def formula(self, base_qty: float, count: int) -> Dict[str, float]:
        inc = calculate_block_volume_increment_ratio(count, self.volume_ratio)
        target_add = base_qty * inc
        target_block = base_qty + target_add
        min_pf = calculate_block_minimum_profit_factor(self.default_min_pf, self.pf_ratio, inc)
        return {
            "volumeIncrement": inc,
            "targetAddQty": target_add,
            "targetBlockQty": target_block,
            "blockMinPF": min_pf,
        }

    def normal_pf(self, lane: BlockLane) -> float:
        ring = [x for x in lane.parent_pf_ring if x is not None][-self.window :]
        if len(ring) < 1:
            # Parent is already live/qualified; inherit stage coordinate (CTS cold start).
            return self.default_min_pf
        # PositionCost-style: wins/losses ratio of +pnl vs -pnl magnitudes
        gp = sum(x for x in ring if x > 0)
        gl = abs(sum(x for x in ring if x < 0))
        if gl <= 0:
            return 2.0 if gp > 0 else 1.0
        return gp / gl

    def observed_pf(self, lane: BlockLane, count: int) -> Tuple[float, int]:
        ring = (lane.pf_ring.get(count) or [])[-self.window :]
        if not ring:
            return self.normal_pf(lane), 0
        gp = sum(x for x in ring if x > 0)
        gl = abs(sum(x for x in ring if x < 0))
        pf = (gp / gl) if gl > 0 else (2.0 if gp > 0 else 1.0)
        return pf, len(ring)

    def pf_decision(self, lane: BlockLane, count: int, intern_pf: float = 1.0) -> Dict[str, Any]:
        inc = calculate_block_volume_increment_ratio(count, self.volume_ratio)
        configured = calculate_block_minimum_profit_factor(self.default_min_pf, self.pf_ratio, inc)
        normal = self.normal_pf(lane)
        observed, n = self.observed_pf(lane, count)
        cold = n < self.min_samples
        intern = float(intern_pf or 1.0)
        if cold:
            observed = intern if intern > 0 else 1.0
            effective = configured if count > 1 else min(float(self.default_min_pf or 1.1), 1.12)
            passes = observed + 1e-9 >= effective
        else:
            effective = calculate_block_effective_minimum_profit_factor(configured, normal)
            passes = observed + 1e-9 >= effective
        return {
            "coldStart": cold,
            "sampleCount": n,
            "observedProfitFactor": observed,
            "normalProfitFactor": normal,
            "configuredMinimumProfitFactor": configured,
            "effectiveMinimumProfitFactor": effective,
            "passesProfitFactor": passes,
            "comparisonAvailable": not cold,
            "internPf": round(intern, 4),
        }

    def unlimited(self) -> bool:
        return int(self.max_stack or 0) <= 0

    def _count_range(self, lane: Optional[BlockLane] = None) -> range:
        """Walk 1..N, or the next few unsatisfied counts when the book is unlimited."""
        if not self.unlimited():
            return range(1, int(self.max_stack) + 1)
        nxt = 1
        if lane is not None:
            sat = max([n for n, ok in (lane.satisfied or {}).items() if ok] or [0])
            nxt = max(1, int(sat) + 1, len(lane.legs) + 1)
        return range(1, nxt + 4)

    def next_unsatisfied(self, lane: BlockLane) -> Optional[int]:
        """Next sequential count (1..maxStack). Never skip a failed/paused rung."""
        if not lane or lane.base_qty <= 0:
            return None
        for n in self._count_range(lane):
            f = self.formula(lane.base_qty, n)
            sat = bool(lane.satisfied.get(n)) or lane.confirmed_add + 1e-12 >= f["targetAddQty"]
            if not sat:
                return int(n)
        return None

    def next_order_qty(self, lane: BlockLane, count: int) -> float:
        f = self.formula(lane.base_qty, count)
        return max(0.0, f["targetAddQty"] - lane.confirmed_add)

    def evaluate_counts(self, lane: BlockLane, live_n: int, intern_pf: float = 1.0) -> List[Dict[str, Any]]:
        """Evaluate every 1..maxStack independently + active overlay. No emission here."""
        rows = []
        if not self.enabled or not lane.active or lane.base_qty <= 0:
            return rows
        now = time.time()
        nxt = self.next_unsatisfied(lane)
        for n in self._count_range(lane):
            f = self.formula(lane.base_qty, n)
            paused = lane.pause_remaining.get(n, 0) > 0 or now < lane.pause_until.get(n, 0)
            sat = bool(lane.satisfied.get(n)) or lane.confirmed_add + 1e-12 >= f["targetAddQty"]
            pf = self.pf_decision(lane, n, intern_pf=intern_pf)
            requested = 0.0 if sat or paused or not pf["passesProfitFactor"] else max(0.0, f["targetAddQty"] - lane.confirmed_add)
            rows.append({
                "setKey": f"{lane.symbol}:{lane.side.lower()}#block:{n}",
                "blockCount": n,
                "kind": "regular",
                "paused": paused,
                "targetSatisfied": sat,
                "requestedAddQty": requested,
                "sequential": nxt == n,
                **f,
                **pf,
                "evaluated": 1,
                "emitted": 0,
            })
        if self.active_real and self.active_live and live_n >= 1 and nxt is not None:
            # Clip to the next sequential count so live overlay never jumps
            # (live_n=3 must not request 3× parent while n=1 is still open).
            n = int(nxt)
            f = self.formula(lane.base_qty, n)
            pf = self.pf_decision(lane, n, intern_pf=intern_pf)
            paused = lane.pause_remaining.get(n, 0) > 0 or now < lane.pause_until.get(n, 0)
            sat = bool(lane.satisfied.get(n)) or lane.confirmed_add + 1e-12 >= f["targetAddQty"]
            requested = 0.0 if sat or paused or not pf["passesProfitFactor"] else max(0.0, f["targetAddQty"] - lane.confirmed_add)
            rows.append({
                "setKey": f"{lane.symbol}:{lane.side.lower()}#block:active:{n}",
                "blockCount": n,
                "kind": "active-live",
                "paused": paused,
                "targetSatisfied": sat,
                "requestedAddQty": requested,
                "sequential": True,
                **f,
                **pf,
                "evaluated": 1,
                "emitted": 0,
            })
        return rows

    def pick_emit(self, rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Emit only the next sequential unsatisfied count.

        Physical book is one non-compounding remainder. Never skip a failed
        rung, never let active-live jump to a larger count.
        """
        regular = [r for r in rows if r.get("kind") == "regular"]
        unsat = [r for r in regular if not r.get("targetSatisfied")]
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
        f = self.formula(lane.base_qty, n)
        before = lane.confirmed_add
        lane.confirmed_add += filled
        sat = lane.confirmed_add + 1e-12 >= f["targetAddQty"]
        lane.satisfied[n] = sat
        # lower counts already covered
        for c in range(1, n):
            fc = self.formula(lane.base_qty, c)
            if lane.confirmed_add + 1e-12 >= fc["targetAddQty"]:
                lane.satisfied[c] = True
        lane.legs.append(
            BlockLeg(
                set_key=row["setKey"],
                block_count=n,
                quantity=filled,
                base_quantity=lane.base_qty,
                volume_ratio=self.volume_ratio,
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

    def on_parent_close(self, symbol: str, side: str, pnl: float, pnl_pct: Optional[float] = None) -> None:
        k = self.key(symbol, side)
        lane = self.lanes.get(k)
        if not lane:
            return
        # Prefer cost-net fraction so PF is size-independent and PositionCost-aware.
        sample = float(pnl_pct) if pnl_pct is not None else float(pnl)
        lane.parent_pf_ring.append(sample)
        lane.parent_pf_ring = lane.parent_pf_ring[-self.window :]
        # advance every existing pause once
        for n, rem in list(lane.pause_remaining.items()):
            if rem > 0:
                lane.pause_remaining[n] = rem - 1
        for n in self._count_range(lane):
            if any(leg.block_count == n for leg in lane.legs):
                lane.pf_ring.setdefault(n, []).append(sample)
                lane.pf_ring[n] = lane.pf_ring[n][-self.window :]
                lane.pause_remaining[n] = self.pause_ratio
                lane.pause_until[n] = time.time() + 45 * max(1, self.pause_ratio)
        lane.active = False
        lane.confirmed_add = 0.0
        lane.legs = []
        lane.satisfied = {}
        lane.base_qty = 0.0
        self.save()

    def snapshot(self) -> Dict[str, Any]:
        lanes = []
        for lane in self.lanes.values():
            if not lane.active and not lane.legs:
                continue
            rows = self.evaluate_counts(lane, live_n=1 if lane.active else 0, intern_pf=1.2)
            lanes.append({
                "symbol": lane.symbol,
                "side": lane.side,
                "baseQty": lane.base_qty,
                "confirmedAdd": round(lane.confirmed_add, 8),
                "aggregate": round(lane.base_qty + lane.confirmed_add, 8),
                "legs": [asdict(x) for x in lane.legs[-8:]],
                "counts": [
                    {
                        "n": r["blockCount"],
                        "kind": r["kind"],
                        "inc": r["volumeIncrement"],
                        "targetAdd": round(r["targetAddQty"], 8),
                        "requested": round(r["requestedAddQty"], 8),
                        "minPF": round(r["blockMinPF"], 4),
                        "obsPF": round(r["observedProfitFactor"], 4),
                        "pass": r["passesProfitFactor"],
                        "paused": r["paused"],
                        "satisfied": r["targetSatisfied"],
                        "cold": r["coldStart"],
                    }
                    for r in rows if r["kind"] == "regular"
                ],
            })
        catalog = []
        show_n = BLOCK_COUNT_PREVIEW if self.unlimited() else max(1, int(self.max_stack))
        show_n = min(show_n, 32)
        for n in range(1, show_n + 1):
            f = self.formula(1.0, n)
            catalog.append({
                "n": n,
                "inc": f["volumeIncrement"],
                "targetAdd": round(f["targetAddQty"], 8),
                "targetBlock": round(f["targetBlockQty"], 8),
                "minPF": round(f["blockMinPF"], 4),
            })
        return {
            "enabled": self.enabled,
            "maxStack": self.max_stack,
            "countN": len(catalog),
            "allCounts": catalog,
            "volumeRatio": self.volume_ratio,
            "profitFactorRatio": self.pf_ratio,
            "pauseCountRatio": self.pause_ratio,
            "activeLive": self.active_live,
            "activeReal": self.active_real,
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
    rec("blk-max-add-sequential", calculate_block_max_additional_ratio(3, 1.0) == 3.0, "not 1+2+3=6")
    rec("blk-max-add-vr", abs(calculate_block_max_additional_ratio(3, 1.5) - 4.5) < 1e-9)
    rec("blk-pf-n1", abs(calculate_block_minimum_profit_factor(1.1, 1.1, 1.0) - 1.11) < 1e-9,
        str(calculate_block_minimum_profit_factor(1.1, 1.1, 1.0)))
    rec("blk-pf-n3", abs(calculate_block_minimum_profit_factor(1.1, 1.1, 3.0) - 1.33) < 1e-9,
        str(calculate_block_minimum_profit_factor(1.1, 1.1, 3.0)))
    rec("blk-parse-active", parse_block_count("sol-usdt:long#block:active:2") == 2)
    rec("blk-parse-set", parse_block_count("xrp-usdt:short#block:set:3") == 3)
    rec("blk-parse-plain", parse_block_count("aaa:long#block:1") == 1)
    rec("blk-parse-none", parse_block_count("general:1m:sl0.6") is None)

    b = BlockBook(os.path.join(tmp, "main.json"), {
        "variantBlockEnabled": True, "blockMaxStack": 3, "blockVolumeRatio": 1.0,
        "blockProfitFactorRatio": 1.1, "defaultMinPF": 1.1,
        "blockActiveRealEnabled": True, "blockActiveLiveEnabled": True,
    })
    rec("blk-zero-remap", BlockBook(os.path.join(tmp, "z.json"), {"blockMaxStack": 0}).max_stack == 3)
    rec("blk-not-unlimited", not b.unlimited())

    long = b.register_parent("SOL-USDT", "LONG", 10.0, 100.0)
    short = b.register_parent("SOL-USDT", "SHORT", 8.0, 100.0)
    rec("blk-lanes-independent", b.key("SOL-USDT", "LONG") != b.key("SOL-USDT", "SHORT")
        and long.base_qty == 10.0 and short.base_qty == 8.0, f"L={long.base_qty} S={short.base_qty}")

    pick = b.pick_emit(b.evaluate_counts(long, live_n=3, intern_pf=1.5))
    rec("blk-no-jump-liven3", pick is not None and pick["blockCount"] == 1
        and abs(pick["requestedAddQty"] - 10.0) < 1e-9,
        f"n={pick and pick.get('blockCount')} qty={pick and pick.get('requestedAddQty')}")
    rec("blk-next-unsat-1", b.next_unsatisfied(long) == 1)

    rows = b.evaluate_counts(long, live_n=3, intern_pf=1.5)
    act = [r for r in rows if r["kind"] == "active-live"]
    rec("blk-active-clipped", len(act) == 1 and act[0]["blockCount"] == 1
        and abs(act[0]["requestedAddQty"] - 10.0) < 1e-9,
        f"act={act[0] if act else None}")

    # remainder: fill n=1 then next is n=2 requesting 1× parent
    b.record_fill(long, pick, 10.0, "c1", "o1")
    rec("blk-n1-satisfied", bool(long.satisfied.get(1)) and abs(long.confirmed_add - 10.0) < 1e-9)
    pick2 = b.pick_emit(b.evaluate_counts(long, live_n=1, intern_pf=1.5))
    rec("blk-n2-remainder-1x", pick2 is not None and pick2["blockCount"] == 2
        and abs(pick2["requestedAddQty"] - 10.0) < 1e-9,
        f"n={pick2 and pick2.get('blockCount')} qty={pick2 and pick2.get('requestedAddQty')}")
    b.record_fill(long, pick2, 10.0, "c2", "o2")
    rec("blk-n1-stays-sat-after-n2", bool(long.satisfied.get(1)) and bool(long.satisfied.get(2)))
    pick3 = b.pick_emit(b.evaluate_counts(long, live_n=1, intern_pf=1.5))
    rec("blk-n3-remainder-1x", pick3 is not None and pick3["blockCount"] == 3
        and abs(pick3["requestedAddQty"] - 10.0) < 1e-9,
        f"n={pick3 and pick3.get('blockCount')} qty={pick3 and pick3.get('requestedAddQty')}")
    rec("blk-agg-before-n3", abs(long.base_qty + long.confirmed_add - 30.0) < 1e-9, str(long.base_qty + long.confirmed_add))
    b.record_fill(long, pick3, 10.0, "c3", "o3")
    rec("blk-stack-full", b.next_unsatisfied(long) is None and b.pick_emit(b.evaluate_counts(long, live_n=1, intern_pf=1.5)) is None)
    rec("blk-agg-4x", abs(long.base_qty + long.confirmed_add - 40.0) < 1e-9, str(long.base_qty + long.confirmed_add))

    # SHORT lane is untouched by LONG fills
    rec("blk-short-untouched", short.confirmed_add == 0.0 and b.next_unsatisfied(short) == 1)
    sp = b.pick_emit(b.evaluate_counts(short, live_n=1, intern_pf=1.5))
    rec("blk-short-n1-own-base", sp is not None and abs(sp["requestedAddQty"] - 8.0) < 1e-9,
        f"qty={sp and sp.get('requestedAddQty')}")

    # intern too low for n=2 must NOT skip to n=3
    lane2 = BlockLane(symbol="AAA-USDT", side="LONG", base_qty=10.0, base_entry=100.0, confirmed_add=10.0, satisfied={1: True})
    # n=2 gate = 1.22, intern 1.15 fails; n=3 gate 1.33 also fails
    rec("blk-no-skip-failed-rung", b.pick_emit(b.evaluate_counts(lane2, live_n=1, intern_pf=1.15)) is None)

    # pause blocks the next count only
    lane3 = BlockLane(symbol="BBB-USDT", side="SHORT", base_qty=5.0, base_entry=50.0)
    b.pause_count(lane3, 1, 600)
    rec("blk-pause-blocks", b.pick_emit(b.evaluate_counts(lane3, live_n=1, intern_pf=1.5)) is None)

    rec("blk-disabled", BlockBook(os.path.join(tmp, "off.json"), {"variantBlockEnabled": False}).evaluate_counts(long, 1, 1.5) == [])
    rec("blk-no-base", b.evaluate_counts(BlockLane("X", "LONG", 0.0, 1.0), 1, 1.5) == [])

    # cost-net PF: losing ring (net frac) blocks, winning ring passes
    lane4 = BlockLane(symbol="PF-USDT", side="LONG", base_qty=1.0, base_entry=100.0, confirmed_add=1.0, satisfied={1: True})
    lane4.pf_ring[2] = [-0.0045] * 8  # 8 losing samples, warm
    lane4.parent_pf_ring = [-0.0045] * 8
    d_loss = b.pf_decision(lane4, 2, intern_pf=1.5)
    rec("blk-warm-loss-blocks", d_loss["passesProfitFactor"] is False and d_loss["coldStart"] is False,
        str(d_loss))
    lane4.pf_ring[2] = [0.003] * 8
    lane4.parent_pf_ring = [0.003] * 8
    d_win = b.pf_decision(lane4, 2, intern_pf=1.0)
    rec("blk-warm-win-passes", d_win["passesProfitFactor"] is True and d_win["observedProfitFactor"] >= 1.0,
        str(d_win))

    # parent close isolates sides + stores cost-net fraction
    b.on_parent_close("SOL-USDT", "LONG", 1.5, pnl_pct=0.0015)
    rec("blk-close-long-retires", (b.lanes["SOL-USDT:LONG"].active is False) and b.lanes["SOL-USDT:LONG"].base_qty == 0.0)
    rec("blk-close-short-alive", b.lanes["SOL-USDT:SHORT"].active is True and b.lanes["SOL-USDT:SHORT"].base_qty == 8.0)
    rec("blk-close-stores-net", abs(b.lanes["SOL-USDT:LONG"].parent_pf_ring[-1] - 0.0015) < 1e-9,
        str(b.lanes["SOL-USDT:LONG"].parent_pf_ring[-1:]))

    rec("blk-freeze-parent", b.register_parent("SOL-USDT", "SHORT", 99.0, 1.0).base_qty == 8.0)
    return out


if __name__ == "__main__":
    rows = self_test()
    bad = 0
    for name, ok, detail in rows:
        print(("PASS" if ok else "FAIL"), name, detail)
        bad += int(not ok)
    print("block_engine", "ok" if not bad else f"fail={bad}")
    raise SystemExit(1 if bad else 0)

