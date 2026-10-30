"""Independent Block evidence and orders for Set parents.

The parent keeps its original entry, stops and hold. Group n observes the first
price n PositionCosts in profit, then measures that add to the parent's exit.
Paper observations continue when an add is rejected; actual fills are a separate
sample. No inherited Set PF, parent PnL or other group's result can validate it.
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, Optional

from block_engine import calculate_block_minimum_profit_factor
from position_cost import cost_as_frac


def _pf(values):
    gp = sum(x for x in values if x > 0)
    gl = -sum(x for x in values if x < 0)
    return gp / gl if gl else (99.0 if gp else 0.0)


class SetBlockBook:
    def __init__(self, path: str) -> None:
        self.path = path
        self.lanes: Dict[str, Any] = {}
        self.error = ""
        self._prune_at = 0.0
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                if data.get("version") != 1 or not isinstance(data.get("lanes"), dict):
                    raise ValueError("unsupported Block state")
                self.lanes = data["lanes"]
            except (OSError, ValueError, TypeError, AttributeError):
                self.error = "Block state unreadable; adds disabled"

    @staticmethod
    def key(pos) -> str:
        return "|".join((pos.set_id, pos.lane or "pos", pos.symbol, pos.side))

    @staticmethod
    def parent_id(pos) -> str:
        return str(pos.client_id or f"{pos.opened_at:.6f}")

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"version": 1, "lanes": self.lanes}, f)
        os.replace(tmp, self.path)

    def observe(self, pos, price: float, now: float, block, cost_pct: float, lookback_s: float) -> Optional[Dict]:
        if self.error or not pos.set_id or not math.isfinite(price) or price <= 0 or pos.base_entry <= 0 or pos.base_qty <= 0:
            return None
        key, parent = self.key(pos), self.parent_id(pos)
        # Bound inactive history by the same lookback, while retaining open parents.
        if now >= self._prune_at:
            for old in [k for k, v in self.lanes.items() if not v.get("parent") and v.get("updated", 0) < now - lookback_s]:
                self.lanes.pop(old)
            self._prune_at = now + 60
        lane = self.lanes.setdefault(key, {"paper": {}, "executed": {}, "updated": now, "cost": cost_pct})
        if lane.get("cost") != cost_pct:
            lane = self.lanes[key] = {"paper": {}, "executed": lane.get("executed", {}), "updated": now, "cost": cost_pct}
        if lane.get("closed") == parent:
            return None
        dirty = False
        if lane.get("parent") != parent:
            lane.update(parent=parent, entries={})
            dirty = True
        cf = cost_as_frac(cost_pct)
        move = (price / pos.base_entry - 1) * (1 if pos.side == "LONG" else -1)
        # Unlimited walks only the next few groups, just as the ordinary Block book.
        limit = int(block.max_stack) if block.max_stack > 0 else max([int(x) for x in lane["entries"]] or [0]) + 4
        limit = min(limit, max(0, int((move + 1e-12) / cf))) if cf > 0 else 0
        for n in range(1, limit + 1):
            if move + 1e-12 >= n * cf and str(n) not in lane["entries"]:
                lane["entries"][str(n)] = [now, price]
                dirty = True
        lane["updated"] = now
        if dirty:
            self.save()
        return lane

    def stats(self, lane: Dict, n: int, source: str, now: float, block, cost_pct: float, lookback_s: float) -> Dict:
        records = lane[source].get(str(n), [])
        rows = [r for r in records if now - lookback_s <= r[0] <= now][-block.window:]
        values = [r[2] if source == "executed" and len(r) > 2 else r[1] - cost_as_frac(cost_pct) for r in rows]
        return {"pf": _pf(values), "n": len(values), "source": source}

    def candidate(self, pos, price: float, now: float, block, cost_pct: float, lookback_s: float) -> Optional[Dict]:
        lane = self.observe(pos, price, now, block, cost_pct, lookback_s)
        if lane is None:
            return None
        # One partial per group, including partial fills: never compound on grown size.
        filled = {}
        for item in pos.block_fills:
            filled[int(item["group"])] = filled.get(int(item["group"]), 0.0) + float(item["qty"])
        rank = len([q for q in filled.values() if q > 0])
        for n in sorted(int(x) for x in lane["entries"]):
            if block.max_stack > 0 and n > block.max_stack:
                continue
            target = block.volume_ratio * pos.base_qty
            remaining = target - filled.get(n, 0.0)
            if remaining <= max(1e-12, target * 1e-9):
                continue
            paper = self.stats(lane, n, "paper", now, block, cost_pct, lookback_s)
            live = self.stats(lane, n, "executed", now, block, cost_pct, lookback_s)
            threshold = max(block.default_min_pf, calculate_block_minimum_profit_factor(
                block.default_min_pf, block.pf_ratio, (rank + int(n not in filled)) * block.volume_ratio))
            # A cold group has no proof. Paused/rejected groups keep collecting paper
            # results on new parents, so their gate can recover without real adds.
            if paper["n"] < block.min_samples or paper["pf"] + 1e-9 < threshold:
                continue
            if live["n"] >= block.min_samples and live["pf"] + 1e-9 < threshold:
                continue
            recent = [r for r in lane["paper"].get(str(n), []) if now - lookback_s <= r[0] <= now]
            if block.pause_ratio and any(r[1] - cost_as_frac(cost_pct) < 0 for r in recent[-block.pause_ratio:]):
                continue
            # Current price must still satisfy the group's strength condition.
            move = (price / pos.base_entry - 1) * (1 if pos.side == "LONG" else -1)
            if move + 1e-12 < n * cost_as_frac(cost_pct):
                continue
            return {"group": n, "qty": remaining, "minPf": threshold, "paper": paper, "executed": live}
        return None

    def close(self, pos, price: float, now: float, block, cost_pct: float, score: bool = True) -> None:
        lane = self.lanes.get(self.key(pos))
        parent = self.parent_id(pos)
        if not lane or lane.get("parent") != parent or lane.get("closed") == parent:
            return
        side = 1 if pos.side == "LONG" else -1
        if score and math.isfinite(price) and price > 0:
            for n, (entry_t, entry) in lane["entries"].items():
                if entry_t >= now or entry <= 0:
                    continue
                rows = lane["paper"].setdefault(n, [])
                rows.append([now, (price / entry - 1) * side])
                del rows[:-block.window]
            groups = {}
            for item in pos.block_fills:
                groups.setdefault(str(item["group"]), []).append(item)
            for n, items in groups.items():
                notional = sum(float(x["qty"]) * float(x["price"]) for x in items)
                gross = sum(float(x["qty"]) * (price - float(x["price"])) * side for x in items)
                if notional > 0:
                    rows = lane["executed"].setdefault(n, [])
                    record = [now, gross / notional]
                    if all(x.get("fee_usdt") is not None for x in items) and pos.exit_fee_qty >= pos.qty > 0:
                        entry_fee = sum(float(x["fee_usdt"]) for x in items)
                        exit_fee = pos.exit_fee_usdt * sum(float(x["qty"]) for x in items) / pos.qty
                        record.append((gross - entry_fee - exit_fee) / notional)
                    rows.append(record)
                    del rows[:-block.window]
        lane.update(parent="", closed=parent, entries={}, updated=now)
        self.save()

    def snapshot(self, now: float, block, cost_pct: float, lookback_s: float) -> Dict:
        rows = []
        for key, lane in self.lanes.items():
            for n in sorted(set(lane["paper"]) | set(lane["executed"]), key=int):
                paper = self.stats(lane, int(n), "paper", now, block, cost_pct, lookback_s)
                live = self.stats(lane, int(n), "executed", now, block, cost_pct, lookback_s)
                if paper["n"] or live["n"]:
                    rows.append({"lane": key, "group": int(n), "paper": paper, "executed": live})
        return {"lanes": rows, "minSamples": block.min_samples, "window": block.window, "error": self.error}
