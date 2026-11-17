"""Independent Block evidence and orders for Set parents (Block v2, owner 2026-11-03).

The parent keeps its original entry, stops and hold. Count n observes the price n steps in profit (its own step from the count before; steps are in
PositionCosts), then measures that add to the parent's exit. Paper observations continue when an add is rejected; actual fills are a separate record. No
inherited Set PF, parent PnL or other count's result can validate a count. Evidence is cumulative or uses the optional add Auto last-N window.
The rules themselves (counts, steps, ratios, Additive / Shared, relations, lifecycle) live in block_rules and are shared
with the simulator (scripts/stack_eval.py).

State version 4 pins the policy of each observed parent and uses per-count carry.
Records without a policy are archived. An unattributable pooled deficit remains a hold when the guard is enabled.
"""
from __future__ import annotations

import json
import math
import os
import re
from typing import Any, Dict, List, Optional

import block_rules as br
from position_cost import cost_as_frac

STATE_VERSION = 4


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
        self.guards: Dict[str, Dict[str, float]] = {}   # relation x count -> executed and shadow net
        self.legacy_guard: Dict[str, float] = br.new_guard()
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
        if ver in (2, 3, STATE_VERSION) and isinstance(data.get("parents"), dict) and isinstance(data.get("ledgers"), dict):
            self.parents, self.ledgers = data["parents"], data["ledgers"]
            def read_guard(g):
                if not isinstance(g, dict):
                    raise ValueError("unreadable Block guard")
                try:
                    net, shadow = float(g.get("net", 0.0)), float(g.get("shadow", 0.0))
                except (TypeError, ValueError):
                    raise ValueError("unreadable Block guard")
                if not (math.isfinite(net) and math.isfinite(shadow)):
                    raise ValueError("unreadable Block guard")
                return {"net": net, "shadow": shadow}
            if ver == 2:
                self.legacy_guard = read_guard(data.get("guard", br.new_guard()))
            else:
                raw_guards = data.get("guards", {})
                if not isinstance(raw_guards, dict):
                    raise ValueError("unreadable Block guards")
                self.guards = {str(k): read_guard(g) for k, g in raw_guards.items()}
                self.legacy_guard = read_guard(data.get("legacyGuard", br.new_guard()))
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
            json.dump({"version": STATE_VERSION, "parents": self.parents, "ledgers": self.ledgers,
                       "guards": self.guards, "legacyGuard": self.legacy_guard}, f)
        os.replace(tmp, self.path)

    # ---- configuration -----------------------------------------------------------------------------------------------------------------------------
    def config(self, block) -> br.BlockCfg:
        """The cfg the rules run on: the one Pulse set from the overlay, else a table built from the BlockBook's own numbers (max_stack counts of one step,
        volume_ratio each, min_samples observations) so a book without an overlay behaves as the earlier Block did, minus its window."""
        if self.cfg is not None:
            return self.cfg
        return br.clean_cfg(
            {"blockRelations": ["set"], "blockSkipFirst": 0, "blockMinObservations": getattr(block, "min_samples", 3) or 3,
             "blockProfitFactorRatio": getattr(block, "pf_ratio", 0.8) or 0.8},
            max_stack=getattr(block, "max_stack", 0) or 3, volume_ratio=getattr(block, "volume_ratio", 0.5) or 0.5,
        )

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
            lane.update(parent=parent, entries={}, shadow={}, policy=cfg.policy, settings=cfg.settings(), cost=cost_pct)
            dirty = True
        elif lane.get("policy") != cfg.policy:
            # An older policy keeps its captured trades through close. Stop
            # issuing additional orders for that policy after a settings edit.
            return None
        cf = cost_as_frac(lane["cost"])
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
        return {"pf": br.record_pf(rec) if all(math.isfinite(v) for v in rec) else None, "n": int(rec[0])}

    def candidate(self, pos, price: float, now: float, block, cost_pct: float, lookback_s: float, floor: Optional[float] = None, *, honor_tp: bool = False) -> Optional[Dict]:
        lane = self.observe(pos, price, now, block, cost_pct, lookback_s)
        if lane is None:
            return None
        cost_pct = lane["cost"]
        if honor_tp and not br.target_covers_cost(price, pos.tp, 1 if pos.side == "LONG" else -1, cost_as_frac(cost_pct)):
            return None
        cfg = self.config(block)
        if not br.kind_allowed(cfg, br.kind_of_set(pos.set_id)):           # this parent's Set type receives no adds; its observations still collect
            return None
        rkeys = self.rel_keys(cfg, pos)
        books = [self.ledger(k) or {"paper": {}, "executed": {}, "life": br.new_life()} for k in rkeys]
        shadow = lane.setdefault("shadow", {})
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
            count = str(n)
            if cfg.carry and any(br.count_life(b, count)["pause"] > 0 for b in books):
                continue
            target = cfg.target_ratio(n) * pos.base_qty
            remaining = target - filled.get(n, 0.0)
            if remaining <= max(1e-12, target * 1e-9):
                continue
            move = (price / pos.base_entry - 1) * (1 if pos.side == "LONG" else -1)
            if move + 1e-12 < levels[n - 1] * cf:
                continue                       # the current price must still satisfy the count's own step
            recs = [br.evidence_record(cfg, b, count, paper=True) for b in books]
            execs = [br.evidence_record(cfg, b, count, paper=False) for b in books]
            # A partially filled count is already in fired_ratio. Its remainder
            # completes that same target; count_passes adds the target once.
            other_ratio = fired_ratio - (cfg.target_ratio(n) if filled.get(n, 0.0) > 0 else 0.0)
            legacy_holds = br.guard_holds(cfg, self.legacy_guard)
            held_keys = {k for k in rkeys if br.guard_holds(cfg, self.guards.get(br.guard_key(k, n)))}
            held = bool(held_keys)
            # While a guard holds, evaluate hypothetical recovery against the
            # paper evidence. Requiring the losing executed record here would
            # prevent any shadow observation and make recovery impossible.
            available_execs = [rec for key, rec in zip(rkeys, execs) if key not in held_keys]
            passed = br.count_passes(cfg, n, recs, available_execs, base_pf, other_ratio)
            shadow_retry = bool(cfg.last_n) and not passed and br.count_passes(cfg, n, recs, (), base_pf, other_ratio)
            if not passed and not shadow_retry:
                continue
            if honor_tp and not br.share_allows(cfg, pos.base_entry, price, pos.tp, 1 if pos.side == "LONG" else -1):
                continue                       # past its share of the way to the target: little room left, the whole way back below
            # A legacy pooled deficit cannot be attributed safely to an individual
            # Set/count. Keep it as a hold until it can be reconciled explicitly.
            if legacy_holds or held or shadow_retry:
                # Follow a scoped deficit on paper, but do not use a pooled legacy
                # deficit to teach or clear any new individual guard.
                if legacy_holds:
                    continue
                if count not in shadow and filled.get(n, 0.0) <= 0:
                    shadow[count] = [now, price, sorted(held_keys)]
                    self.save()
                continue
            if str(n) in shadow:
                continue                       # this count of this parent is already followed on paper: one result per count, never both
            return {"group": n, "qty": remaining, "minPf": br.threshold(cfg, base_pf, other_ratio + cfg.target_ratio(n)),
                    "paper": self.stats(recs[0]), "executed": self.stats(books[0]["executed"].get(count))}
        return None

    # ---- close -------------------------------------------------------------------------------------------------------------------------------------
    def close(self, pos, price: float, now: float, block, cost_pct: float, score: bool = True) -> None:
        lane = self.parents.get(self.key(pos))
        parent = self.parent_id(pos)
        if not lane or lane.get("parent") != parent or lane.get("closed") == parent:
            return
        cfg = self.config(block)
        if lane.get("policy") != cfg.policy:
            former = lane.get("settings")
            cfg = br.clean_cfg(former) if isinstance(former, dict) else None
            if cfg is None or cfg.policy != lane.get("policy"):
                # A pre-policy parent cannot contribute to the current book.
                lane.update(parent="", closed=parent, entries={}, shadow={}, updated=now)
                lane.pop("settings", None)
                self.save()
                return
        if not score or not math.isfinite(price) or price <= 0:
            # An unknown result cannot reduce a deficit or consume a pause.
            # In Auto it occupies its place in the recorded price/decision tape.
            if cfg.last_n:
                for key in self.rel_keys(cfg, pos):
                    book = self.ledger(key, create=True)
                    for count in lane["entries"]:
                        br.add_observation(cfg, book, count, None, "paper")
                    decision_counts = set(lane.get("shadow", {})) | {str(item["group"]) for item in pos.block_fills}
                    for count in decision_counts:
                        br.add_observation(cfg, book, count, None, "shadow")
            lane.update(parent="", closed=parent, entries={}, shadow={}, updated=now)
            lane.pop("settings", None)
            self.save()
            return
        side = 1 if pos.side == "LONG" else -1
        cf = cost_as_frac(lane.get("cost", cost_pct))
        rkeys = self.rel_keys(cfg, pos)
        books = [self.ledger(k, create=True) for k in rkeys]
        shadow_parts: Dict[str, Dict[str, float]] = {}
        add_parts: Dict[str, float] = {}
        for n, item in (lane.get("shadow") or {}).items():
            try:
                s_t, s_px = float(item[0]), float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if s_t < now and s_px > 0 and pos.base_entry > 0:
                net = (price / s_px - 1) * side - cf
                part = br.base_return_contribution(net, s_px, cfg.target_ratio(int(n)), pos.base_entry)
                for key in item[2]:
                    shadow_parts.setdefault(key, {})[str(n)] = part
                for b in books:
                    br.add_observation(cfg, b, str(n), net, "shadow")
        groups: Dict[str, List[Any]] = {}
        for item in pos.block_fills:
            groups.setdefault(str(item["group"]), []).append(item)
        for n, (entry_t, entry) in lane["entries"].items():
            if entry_t >= now or entry <= 0:
                continue
            net = (price / entry - 1) * side - cf
            for b in books:
                br.add_observation(cfg, b, n, net, "paper")
        for n, items in groups.items():
            notional = sum(float(x["qty"]) * float(x["price"]) for x in items)
            quantity = sum(float(x["qty"]) for x in items)
            gross = sum(float(x["qty"]) * (price - float(x["price"])) * side for x in items)
            if notional <= 0 or quantity <= 0:
                continue
            net_add = gross / notional - cf
            if all(x.get("fee_usdt") is not None for x in items) and pos.exit_fee_qty >= pos.qty > 0:
                entry_fee = sum(float(x["fee_usdt"]) for x in items)
                exit_fee = pos.exit_fee_usdt * quantity / pos.qty
                net_add = (gross - entry_fee - exit_fee) / notional
            for b in books:
                br.add_observation(cfg, b, n, net_add, "executed")
            add_parts[n] = br.base_return_contribution(net_add, notional / quantity, quantity / pos.base_qty, pos.base_entry)
        base_net = (price / pos.base_entry - 1) * side - cf
        if pos.base_entry_fee_usdt is not None and pos.exit_fee_qty >= pos.qty > 0 and pos.base_qty > 0 and pos.base_entry > 0:
            base_fees = pos.base_entry_fee_usdt + pos.exit_fee_usdt * pos.base_qty / pos.qty
            base_net = (price / pos.base_entry - 1) * side - base_fees / (pos.base_qty * pos.base_entry)
        for b in books:
            br.update_count_lives(cfg, b, base_net, add_parts)
            b["updated"] = now
        for k in rkeys:
            shadow = shadow_parts.get(k, {})
            for n in add_parts.keys() | shadow.keys():
                gk = br.guard_key(k, int(n))
                was_held = br.guard_holds(cfg, self.guards.get(gk))
                self.guards[gk] = br.guard_after_close(cfg, self.guards.get(gk), add_parts.get(n, 0.0), shadow.get(n, 0.0))
                if was_held and not br.guard_holds(cfg, self.guards[gk]):
                    # Keep all-time actual execution statistics; start a new
                    # qualification epoch after a paper-recovered deficit.
                    self.ledger(k, create=True).setdefault("qualification", {})[n] = br.new_record()
        lane.update(parent="", closed=parent, entries={}, shadow={}, updated=now)
        lane.pop("settings", None)
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
                scale = max(scale, br.relation_scale(cfg, b))
        return scale

    # ---- snapshot ----------------------------------------------------------------------------------------------------------------------------------
    def snapshot(self, now: float, block, cost_pct: float, lookback_s: float) -> Dict:
        cfg = self.config(block)
        rows = []
        archived = 0
        for key, led in self.ledgers.items():
            if not self.current_key(key, cfg):
                archived += 1
                continue
            for n in sorted(set(led["paper"]) | set(led["executed"]), key=int):
                paper = self.stats(led["paper"].get(n))
                live = self.stats(led["executed"].get(n))
                if paper["n"] or live["n"]:
                    life = br.count_life(led, n)
                    rows.append({"lane": key, "group": int(n), "paper": paper, "executed": live, "level": int(life["level"]),
                                 "pause": int(life["pause"]), "net": round(float(life["net"]), 6)})
        active_guards, idle_guards = [], []
        current_guards, any_held = 0, False
        for gk, guard in self.guards.items():
            try:
                rkey, count = json.loads(gk)
                version, sid, lane, side, policy, relation, *group = json.loads(rkey)
                if version != STATE_VERSION or policy != cfg.policy:
                    continue
            except (ValueError, TypeError):
                continue
            holding = br.guard_holds(cfg, guard)
            current_guards += 1
            any_held |= holding
            dest = active_guards if holding else idle_guards
            if len(dest) < 200:
                dest.append({"set": sid, "lane": lane, "side": side, "relation": relation, "group": group,
                             "count": count, "holding": holding, "net": round(guard["net"], 6),
                             "shadow": round(guard["shadow"], 6)})
        return {"lanes": rows, "minObservations": cfg.min_obs, "stackType": cfg.stack_type, "relations": list(cfg.relations),
                "policy": cfg.policy, "archivedLedgers": archived, "addLastN": cfg.last_n, "addAutoMin": cfg.auto_min,
                "counts": [{"n": i + 1, "step": c["step"], "ratio": c["ratio"], "on": c.get("on", True)} for i, c in enumerate(cfg.counts)],
                "carry": cfg.carry, "pauseSteps": cfg.pause_steps, "skipFirst": cfg.skip_first, "error": self.error,
                "guard": {"on": cfg.guard, "holding": br.guard_holds(cfg, self.legacy_guard) or any_held,
                          "legacyBlocked": br.guard_holds(cfg, self.legacy_guard), "scopes": current_guards,
                          "legacyNet": round(self.legacy_guard["net"], 6), "byScope": (active_guards + idle_guards)[:200]},
                "targetShare": cfg.target_share, "kinds": list(cfg.kinds) or list(br.KINDS)}

    @staticmethod
    def current_key(key: str, cfg: br.BlockCfg) -> bool:
        try:
            parts = json.loads(key)
            return isinstance(parts, list) and len(parts) >= 6 and parts[0] == STATE_VERSION and parts[4] == cfg.policy
        except (ValueError, TypeError):
            return False
