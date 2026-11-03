"""Independent Block evidence and orders for Set parents (Block v2, owner 2026-11-03).

The parent keeps its original entry, stops and hold. Count n observes the price n steps in profit (its own step from the count before; steps are in
PositionCosts), then measures that add to the parent's exit. Paper observations continue when an add is rejected; actual fills are a separate record. No
inherited Set PF, parent PnL or other count's result can validate a count. There is NO window: every record is the cumulative sum of all its observations
since the book started (block_rules). The rules themselves (counts, steps, ratios, Additive / Shared, relations, lifecycle) live in block_rules and are shared
with the simulator (scripts/stack_eval.py).

State file version 2: `parents` (the open parent's count entries per Set / lane / symbol / side) and `ledgers` (the cumulative records and the lifecycle per
relation key). Version 1 (per-lane windows of rows) migrates: its paper rows are summed into the "set" relation's ledger, its executed rows too.
"""
from __future__ import annotations

import json
import math
import os
import re
from typing import Any, Dict, List, Optional

import block_rules as br
from position_cost import cost_as_frac

STATE_VERSION = 2


def _pack_step(set_id: str):
    pack = str(set_id or "").split(":", 1)[0]
    m = re.search(r":st(\d+)", str(set_id or ""))
    return pack, int(m.group(1)) if m else 0


class SetBlockBook:
    def __init__(self, path: str) -> None:
        self.path = path
        self.parents: Dict[str, Any] = {}       # "<set>|<lane>|<symbol>|<side>" -> {"parent", "entries": {n: [t, price]}, "closed", "updated"}
        self.ledgers: Dict[str, Any] = {}       # relation key -> {"paper": {n: [obs, gains, losses]}, "executed": {...}, "life": {...}, "updated": t}
        self.cfg: Optional[br.BlockCfg] = None   # set by Pulse from the overlay (clean_cfg); None = the legacy table built from the BlockBook's own numbers
        self.scope = "set"                       # kept for callers that still set it; the relations (cfg.relations) decide the evidence keys now
        self.error = ""
        self._prune_at = 0.0
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                self._load(data)
            except (OSError, ValueError, TypeError, AttributeError, KeyError):
                self.error = "Block state unreadable; adds disabled"

    # ---- state -------------------------------------------------------------------------------------------------------------------------------------
    def _load(self, data: Dict[str, Any]) -> None:
        ver = data.get("version")
        if ver == STATE_VERSION and isinstance(data.get("parents"), dict) and isinstance(data.get("ledgers"), dict):
            self.parents, self.ledgers = data["parents"], data["ledgers"]
            return
        if ver == 1 and isinstance(data.get("lanes"), dict):
            for key, lane in data["lanes"].items():
                self.parents[key] = {"parent": lane.get("parent", ""), "entries": lane.get("entries", {}), "closed": lane.get("closed", ""),
                                     "updated": lane.get("updated", 0.0)}
                set_id, lane_id, _symbol, side = (key.split("|") + ["", "", "", ""])[:4]
                led = self.ledgers.setdefault(f"set|{set_id}|{lane_id}|{side}", {"paper": {}, "executed": {}, "life": br.new_life(), "updated": lane.get("updated", 0.0)})
                cost = cost_as_frac(lane.get("cost", 0.12))
                for source, scale in (("paper", True), ("executed", False)):
                    for n, rows in (lane.get(source) or {}).items():
                        rec = led[source].setdefault(str(n), br.new_record())
                        for r in rows:
                            br.add_obs(rec, (float(r[1]) - cost) if scale else float(r[2] if len(r) > 2 else float(r[1]) - cost))
            return
        raise ValueError("unsupported Block state")

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"version": STATE_VERSION, "parents": self.parents, "ledgers": self.ledgers}, f)
        os.replace(tmp, self.path)

    # ---- configuration -----------------------------------------------------------------------------------------------------------------------------
    def config(self, block) -> br.BlockCfg:
        """The cfg the rules run on: the one Pulse set from the overlay, else a table built from the BlockBook's own numbers (max_stack counts of one step,
        volume_ratio each, min_samples observations) so a book without an overlay behaves as the earlier Block did, minus its window."""
        if self.cfg is not None:
            return self.cfg
        cfg = br.clean_cfg({}, max_stack=int(getattr(block, "max_stack", 0) or 3), volume_ratio=float(getattr(block, "volume_ratio", 0.5) or 0.5))
        cfg.min_obs = max(1, int(getattr(block, "min_samples", 3) or 3))
        cfg.pf_ratio = float(getattr(block, "pf_ratio", 0.8) or 0.8)
        return cfg

    # ---- keys --------------------------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def key(pos) -> str:
        return "|".join((pos.set_id, pos.lane or "pos", pos.symbol, pos.side))

    @staticmethod
    def parent_id(pos) -> str:
        return str(pos.client_id or f"{pos.opened_at:.6f}")

    def rel_keys(self, cfg: br.BlockCfg, pos) -> List[str]:
        pack, step = _pack_step(pos.set_id)
        return br.relation_keys(cfg, pos.set_id, pos.lane or "pos", pos.side, pos.symbol, pack, step)

    def ledger(self, rkey: str, create: bool = False) -> Optional[Dict[str, Any]]:
        got = self.ledgers.get(rkey)
        if got is None and create:
            got = self.ledgers[rkey] = {"paper": {}, "executed": {}, "life": br.new_life(), "updated": 0.0}
        return got

    # ---- observation -------------------------------------------------------------------------------------------------------------------------------
    def observe(self, pos, price: float, now: float, block, cost_pct: float, lookback_s: float) -> Optional[Dict]:
        if self.error or not pos.set_id or not math.isfinite(price) or price <= 0 or pos.base_entry <= 0 or pos.base_qty <= 0:
            return None
        cfg = self.config(block)
        key, parent = self.key(pos), self.parent_id(pos)
        if now >= self._prune_at:                 # only inactive per-parent entries are pruned; the cumulative ledgers are the evidence and stay
            for old in [k for k, v in self.parents.items() if not v.get("parent") and v.get("updated", 0) < now - max(lookback_s, 3600.0)]:
                self.parents.pop(old)
            self._prune_at = now + 60
        lane = self.parents.setdefault(key, {"parent": "", "entries": {}, "closed": "", "updated": now})
        if lane.get("closed") == parent:
            return None
        dirty = False
        if lane.get("parent") != parent:
            lane.update(parent=parent, entries={})
            dirty = True
        cf = cost_as_frac(cost_pct)
        move = (price / pos.base_entry - 1) * (1 if pos.side == "LONG" else -1)
        for n, level in enumerate(cfg.levels(), start=1):
            if cf > 0 and move + 1e-12 >= level * cf and str(n) not in lane["entries"]:
                lane["entries"][str(n)] = [now, price]
                dirty = True
        lane["updated"] = now
        if dirty:
            self.save()
        return lane

    @staticmethod
    def stats(rec: Optional[Any], *_a, **_k) -> Dict:
        """{pf, n} of one cumulative record ([obs, gains, losses]); empty when there is none."""
        if not rec:
            return {"pf": 0.0, "n": 0}
        return {"pf": br.record_pf(rec), "n": int(rec[0])}

    def candidate(self, pos, price: float, now: float, block, cost_pct: float, lookback_s: float, floor: Optional[float] = None) -> Optional[Dict]:
        lane = self.observe(pos, price, now, block, cost_pct, lookback_s)
        if lane is None:
            return None
        cfg = self.config(block)
        rkeys = self.rel_keys(cfg, pos)
        books = [self.ledger(k) or {"paper": {}, "executed": {}, "life": br.new_life()} for k in rkeys]
        if any(int(b["life"].get("pause", 0)) > 0 for b in books):        # a relation in its pause adds nothing; its evidence still collects
            return None
        filled: Dict[int, float] = {}
        for item in pos.block_fills:
            filled[int(item["group"])] = filled.get(int(item["group"]), 0.0) + float(item["qty"])
        fired_ratio = sum(cfg.target_ratio(n) for n, q in filled.items() if q > 0)
        base_pf = float(floor) if floor is not None else float(getattr(block, "default_min_pf", 1.1) or 1.1)
        cf = cost_as_frac(cost_pct)
        levels = cfg.levels()
        for n in sorted(int(x) for x in lane["entries"]):
            if not cfg.active(n):
                continue
            target = cfg.target_ratio(n) * pos.base_qty
            remaining = target - filled.get(n, 0.0)
            if remaining <= max(1e-12, target * 1e-9):
                continue
            move = (price / pos.base_entry - 1) * (1 if pos.side == "LONG" else -1)
            if move + 1e-12 < levels[n - 1] * cf:
                continue                       # the current price must still satisfy the count's own step
            recs = [b["paper"].get(str(n)) for b in books]
            execs = [b["executed"].get(str(n)) for b in books]
            if not br.count_passes(cfg, n, recs, execs, base_pf, fired_ratio):
                continue
            return {"group": n, "qty": remaining, "minPf": br.threshold(cfg, base_pf, fired_ratio + cfg.target_ratio(n)),
                    "paper": self.stats(recs[0]), "executed": self.stats(execs[0])}
        return None

    # ---- close -------------------------------------------------------------------------------------------------------------------------------------
    def close(self, pos, price: float, now: float, block, cost_pct: float, score: bool = True) -> None:
        lane = self.parents.get(self.key(pos))
        parent = self.parent_id(pos)
        if not lane or lane.get("parent") != parent or lane.get("closed") == parent:
            return
        cfg = self.config(block)
        side = 1 if pos.side == "LONG" else -1
        cf = cost_as_frac(cost_pct)
        rkeys = self.rel_keys(cfg, pos)
        books = [self.ledger(k, create=True) for k in rkeys]
        add_part, highest = 0.0, 0
        groups: Dict[str, List[Any]] = {}
        for item in pos.block_fills:
            groups.setdefault(str(item["group"]), []).append(item)
        if score and math.isfinite(price) and price > 0:
            for n, (entry_t, entry) in lane["entries"].items():
                if entry_t >= now or entry <= 0:
                    continue
                net = (price / entry - 1) * side - cf
                for b in books:
                    br.add_obs(b["paper"].setdefault(n, br.new_record()), net)
            for n, items in groups.items():
                notional = sum(float(x["qty"]) * float(x["price"]) for x in items)
                gross = sum(float(x["qty"]) * (price - float(x["price"])) * side for x in items)
                if notional > 0:
                    net_add = gross / notional - cf
                    if all(x.get("fee_usdt") is not None for x in items) and pos.exit_fee_qty >= pos.qty > 0:
                        entry_fee = sum(float(x["fee_usdt"]) for x in items)
                        exit_fee = pos.exit_fee_usdt * sum(float(x["qty"]) for x in items) / pos.qty
                        net_add = (gross - entry_fee - exit_fee) / notional
                    for b in books:
                        br.add_obs(b["executed"].setdefault(n, br.new_record()), net_add)
                    add_part += (sum(float(x["qty"]) for x in items) / pos.base_qty) * net_add
                    highest = max(highest, int(n))
        parent_net = ((price / pos.base_entry - 1) * side - cf if pos.base_entry > 0 and math.isfinite(price) and price > 0 else 0.0) + add_part
        for b in books:
            b["life"] = br.life_after_close(cfg, b["life"], parent_net, highest)
            b["updated"] = now
        lane.update(parent="", closed=parent, entries={}, updated=now)
        self.save()

    # ---- carried size ------------------------------------------------------------------------------------------------------------------------------
    def entry_scale(self, set_id: str, lane: str, side: str, symbol: str, block) -> float:
        """The multiple of V0 a new parent of this Set opens at: 1.0 unless the carry is on and a relation holds a raised level (the largest of them)."""
        cfg = self.config(block)
        if not cfg.carry or self.error:
            return 1.0
        pack, step = _pack_step(set_id)
        scale = 1.0
        for k in br.relation_keys(cfg, set_id, lane or "pos", side, symbol, pack, step):
            b = self.ledgers.get(k)
            if b:
                scale = max(scale, br.carried_scale(cfg, b["life"]))
        return scale

    # ---- snapshot ----------------------------------------------------------------------------------------------------------------------------------
    def snapshot(self, now: float, block, cost_pct: float, lookback_s: float) -> Dict:
        rows = []
        for key, led in self.ledgers.items():
            for n in sorted(set(led["paper"]) | set(led["executed"]), key=int):
                paper = self.stats(led["paper"].get(n))
                live = self.stats(led["executed"].get(n))
                if paper["n"] or live["n"]:
                    rows.append({"lane": key, "group": int(n), "paper": paper, "executed": live, "level": int(led["life"].get("level", 0)),
                                 "pause": int(led["life"].get("pause", 0)), "net": round(float(led["life"].get("net", 0.0)), 6)})
        cfg = self.config(block)
        return {"lanes": rows, "minObservations": cfg.min_obs, "stackType": cfg.stack_type, "relations": list(cfg.relations),
                "counts": [{"n": i + 1, "step": c["step"], "ratio": c["ratio"], "on": c.get("on", True)} for i, c in enumerate(cfg.counts)],
                "carry": cfg.carry, "pauseSteps": cfg.pause_steps, "skipFirst": cfg.skip_first, "error": self.error}
