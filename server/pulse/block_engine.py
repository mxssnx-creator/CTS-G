"""CTS Block strategy — formulas and lifecycle as in BLOCK_STRATEGY_SYSTEM.md / block-count-state.ts.

Volume (additive, from the original base volume V0 of the group):
- A group k = 1..maxStack is one partial add of ratio r = volumeRatio, worth r x V0. V0 is fixed for the whole group; no
  partial is computed on the grown volume.
- Stacking is by the number of VALID groups (groups whose PF decision passes and that are not paused), not by their
  index: with groups 1 and 3 valid and group 2 not, the stack after group 3 is V0 x (1 + 2r), and group 3 adds one
  partial (r x V0), never the cumulative 3 x r x V0 in one order. The stack never exceeds V0 x (1 + maxStack x r).
- The k-th valid group must pass the PF threshold of the stack it would create (1 + (minPf - 1) x pfRatio x k x r).
- A stack that was increased stays increased: when the parent closes at a net loss the lane keeps its level (the number
  of partials) and the original V0, the next parent opens at V0 x (1 + level x r), and the group's net accumulates over
  its parents. When that net is back to equal (>= 0) the level resets to the base volume. The level never shrinks while
  the net is negative.
- Observed PF and pauses per group persist across parents (they are the lane's memory, not the parent's).
"""
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
BLOCK_PF_RATIO_MIN = 0.2
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
    # the stack that stays increased across parents until the group's net is back to equal
    carried: int = 0        # partials (valid groups) carried into the next parent
    top: int = 0            # the highest group index in the carried stack (those groups count as already valid)
    v0: float = 0.0         # the group's original base volume
    net: float = 0.0        # net result (USDT) of the group's parents since it first increased


class BlockBook:
    """Independent Block book: never opens without a same-side parent."""

    def __init__(self, path: str, cfg: Optional[Dict[str, Any]] = None) -> None:
        self.path = path
        cfg = cfg or {}
        self.enabled = bool(cfg.get("variantBlockEnabled", True))
        raw_stack = cfg.get("blockMaxStack", 6)
        try:
            stack_n = int(raw_stack if raw_stack is not None else 0)
        except Exception:
            stack_n = 0
        # default 6 groups, 0 = unlimited. Never clamp the book; evaluate() only walks the next few unsatisfied counts.
        self.max_stack = 0 if stack_n <= 0 else max(1, stack_n)
        self.volume_ratio = clamp(float(cfg.get("blockVolumeRatio", 1) or 1), 0.25, 3.0)
        self.pf_ratio = clamp(float(cfg.get("blockProfitFactorRatio", 0.8) or 0.8), 0.2, 5.0)
        self.pause_ratio = max(0, int(cfg.get("blockPauseCountRatio", 1) or 1))
        self.active_real = bool(cfg.get("blockActiveRealEnabled", True))
        self.active_live = bool(cfg.get("blockActiveLiveEnabled", True))
        self.default_min_pf = float(cfg.get("defaultMinPF", 1.2) or 1.2)
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
                carried=int(v.get("carried") or 0),
                top=int(v.get("top") or 0),
                v0=float(v.get("v0") or 0),
                net=float(v.get("net") or 0),
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
                "carried": lane.carried,
                "top": lane.top,
                "v0": lane.v0,
                "net": lane.net,
            }
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(blob, f)
        os.replace(tmp, self.path)

    def register_parent(self, symbol: str, side: str, qty: float, entry: float) -> BlockLane:
        """A parent opened. The lane object is reused so its observed PF, pauses and parent ring survive; only the
        parent's own state starts over. With a carried stack the parent already holds the carried partials: V0 stays the
        group's original volume and the carried part counts as confirmed."""
        k = self.key(symbol, side)
        lane = self.lanes.get(k)
        if lane and lane.base_qty > 0:
            lane.active = True
            return lane
        if lane is None:
            lane = BlockLane(symbol=symbol, side=side, base_qty=qty, base_entry=entry, active=True)
            self.lanes[k] = lane
        carried = lane.carried > 0 and lane.v0 > 0
        lane.base_qty = lane.v0 if carried else qty
        lane.v0 = lane.base_qty
        lane.base_entry = entry
        lane.confirmed_add = lane.carried * self.volume_ratio * lane.v0 if carried else 0.0
        if not carried:
            lane.carried, lane.top, lane.net = 0, 0, 0.0
        lane.legs = []
        lane.satisfied = {}
        lane.active = True
        self.save()
        return lane

    def parent_scale(self, symbol: str, side: str) -> float:
        """Multiplier of the base volume for the next parent of this lane: 1 + carried partials x ratio, at most the full
        stack. 1.0 when nothing is carried."""
        lane = self.lanes.get(self.key(symbol, side))
        if not self.enabled or lane is None or lane.carried <= 0:
            return 1.0
        n = lane.carried if self.unlimited() else min(lane.carried, int(self.max_stack))
        return 1.0 + n * self.volume_ratio

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

    def pf_decision(self, lane: BlockLane, count: int, intern_pf: float = 1.0, rank: Optional[int] = None) -> Dict[str, Any]:
        """The PF decision of group `count`. `rank` is the number of valid groups the stack would have with it (the k-th
        valid group); its threshold is that of the stack it creates. Without a rank the count itself is used."""
        inc = calculate_block_volume_increment_ratio(rank if rank is not None else count, self.volume_ratio)
        configured = calculate_block_minimum_profit_factor(self.default_min_pf, self.pf_ratio, inc)
        normal = self.normal_pf(lane)
        observed, n = self.observed_pf(lane, count)
        cold = n < self.min_samples
        intern = float(intern_pf or 1.0)
        if cold:
            observed = intern if intern > 0 else 1.0
            effective = configured if count > 1 else min(float(self.default_min_pf or 1.2), 1.12)
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

    def next_order_qty(self, lane: BlockLane, count: int) -> float:
        f = self.formula(lane.base_qty, count)
        return max(0.0, f["targetAddQty"] - lane.confirmed_add)

    def evaluate_counts(self, lane: BlockLane, live_n: int, intern_pf: float = 1.0) -> List[Dict[str, Any]]:
        """Evaluate every group 1..maxStack and the active overlay row. No emission here.

        Groups are walked in order. A group whose PF decision passes (and is not paused) is VALID and becomes the k-th
        valid group: its cumulative target is k x ratio x V0 and it is satisfied once the confirmed adds reach it, so it
        requests one partial (ratio x V0) after the previous valid group, however many groups before it were not valid.
        """
        rows: List[Dict[str, Any]] = []
        if not self.enabled or not lane.active or lane.base_qty <= 0:
            return rows
        now = time.time()
        v0 = lane.base_qty
        carried_top = lane.top if lane.carried > 0 else 0
        valid = lane.carried if carried_top else 0           # the carried partials are already valid groups
        for n in self._count_range(lane):
            if n <= carried_top:
                f = self.formula(v0, max(1, valid))
                rows.append({
                    "setKey": f"{lane.symbol}:{lane.side.lower()}#block:{n}", "blockCount": n, "kind": "regular",
                    "paused": False, "targetSatisfied": True, "requestedAddQty": 0.0, "validRank": 0, **f,
                    "volumeIncrement": self.volume_ratio, "cumulativeRatio": 0.0, "carried": True,
                    **self.pf_decision(lane, n, intern_pf=intern_pf, rank=max(1, valid)), "evaluated": 1, "emitted": 0,
                })
                continue
            rank = valid + 1
            f = self.formula(v0, rank)
            paused = lane.pause_remaining.get(n, 0) > 0 or now < lane.pause_until.get(n, 0)
            pf = self.pf_decision(lane, n, intern_pf=intern_pf, rank=rank)
            ok = bool(pf["passesProfitFactor"]) and not paused
            if ok:
                valid += 1
            sat = ok and lane.confirmed_add + 1e-12 >= f["targetAddQty"]
            # one order adds at most one partial (ratio x V0); the rest of a cumulative gap comes with the next order
            requested = min(max(0.0, f["targetAddQty"] - lane.confirmed_add), self.volume_ratio * v0) if ok and not sat else 0.0
            rows.append({
                "setKey": f"{lane.symbol}:{lane.side.lower()}#block:{n}",
                "blockCount": n,
                "kind": "regular",
                "paused": paused,
                "targetSatisfied": sat,
                "requestedAddQty": requested,
                "validRank": rank if ok else 0,
                **f,
                "volumeIncrement": self.volume_ratio,          # the partial this group adds (x V0)
                "cumulativeRatio": rank * self.volume_ratio if ok else 0.0,
                **pf,
                "evaluated": 1,
                "emitted": 0,
            })
        if self.active_real and self.active_live and live_n >= 1 and rows:
            n = max(1, live_n)
            if not self.unlimited():
                n = min(int(self.max_stack), n)
            src = next((r for r in rows if r["blockCount"] == n), None)
            if src is not None:
                rows.append({**src, "setKey": f"{lane.symbol}:{lane.side.lower()}#block:active:{n}", "kind": "active-live"})
        return rows

    def pick_emit(self, rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Independent counts; physical book is one non-compounding target.
        Emit the smallest unsatisfied passing count with remaining delta (CTS sequential remainder).
        """
        cand = [
            r for r in rows
            if r["requestedAddQty"] > 0 and r["passesProfitFactor"] and not r["paused"] and not r["targetSatisfied"]
        ]
        if not cand:
            return None
        cand.sort(key=lambda r: (0 if r["kind"] == "active-live" else 1, r["blockCount"]))
        return cand[0]

    def record_fill(self, lane: BlockLane, row: Dict[str, Any], filled: float, cid: str, oid: str) -> None:
        n = int(row["blockCount"])
        f = {"volumeIncrement": self.volume_ratio, "targetAddQty": float(row["targetAddQty"]),
             "targetBlockQty": float(row["targetBlockQty"])}
        before = lane.confirmed_add
        lane.confirmed_add += filled
        sat = lane.confirmed_add + 1e-12 >= f["targetAddQty"]
        lane.satisfied[n] = sat
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

    def on_parent_close(self, symbol: str, side: str, pnl: float) -> None:
        k = self.key(symbol, side)
        lane = self.lanes.get(k)
        if not lane:
            return
        lane.parent_pf_ring.append(pnl)
        lane.parent_pf_ring = lane.parent_pf_ring[-self.window :]
        # advance every existing pause once
        for n, rem in list(lane.pause_remaining.items()):
            if rem > 0:
                lane.pause_remaining[n] = rem - 1
        for n in self._count_range(lane):
            if any(leg.block_count == n for leg in lane.legs):
                lane.pf_ring.setdefault(n, []).append(pnl)
                lane.pf_ring[n] = lane.pf_ring[n][-self.window :]
                lane.pause_remaining[n] = self.pause_ratio
                lane.pause_until[n] = time.time() + 45 * max(1, self.pause_ratio)
        # the stack stays increased until the group's net is back to equal
        partials = lane.carried + len(lane.legs)
        if partials > 0:
            lane.net += float(pnl)
            if lane.net >= 0:
                lane.carried, lane.top, lane.v0, lane.net = 0, 0, 0.0, 0.0   # back to equal: the next parent opens at the base volume
            else:
                cap = partials if self.unlimited() else min(partials, int(self.max_stack))
                lane.carried = max(lane.carried, cap)             # never shrinks while the net is negative
                lane.top = max([lane.top] + [int(leg.block_count) for leg in lane.legs])
        lane.active = False
        lane.confirmed_add = 0.0
        lane.legs = []
        lane.satisfied = {}
        lane.base_qty = 0.0
        self.save()

    def snapshot(self) -> Dict[str, Any]:
        lanes = []
        for lane in self.lanes.values():
            if not lane.active and not lane.legs and lane.carried <= 0:
                continue
            rows = self.evaluate_counts(lane, live_n=1 if lane.active else 0, intern_pf=1.2)
            lanes.append({
                "symbol": lane.symbol,
                "side": lane.side,
                "baseQty": lane.base_qty,
                "carried": lane.carried,
                "groupNet": round(lane.net, 6),
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
                        "rank": r["validRank"],
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
