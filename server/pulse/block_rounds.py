"""Block rounds (owner definition, 2026-11-11): Block as volume control over rounds of N positions.

A procedure is scoped to its Set, lane, direction, relation, count and sizing policy. Its positions form consecutive blocks of N in opening order.
A block completes when all N positions close. Its recovery result uses the volume this procedure alone would have requested, subject to its ceiling.
Actual entry sizing can combine several procedures and has its own execution statistics.

  - A losing block raises the NEXT positions of the group: 1 + ratio x level of the ORIGINAL base volume (level 1 after the first loss).
  - The raise is kept while the group is losing: until the volume-weighted net since the losing block that started it is back at or above zero.
  - Step: while still losing, every further losing block raises the level by one more (up to maxLevel), each time by the ratio from the base volume.
  - Pause: after a positive block at base volume, and after a recovered raise, the next `pause` blocks run at base volume and start nothing.

Every block count 1..6 of every selected relation runs its own procedure on its own blocks, level, net and pause: none reads another's state. A
position's volume multiple is 1 + the sum of (ratio x level) over the procedures it belongs to whose count is switched on, at most maxMult. A count
that is off (or at ratio 0) still forms its blocks and keeps its record, so every count is visible in the statistics; it raises nothing.

The multiple is decided at the open from blocks completed before it: nothing that closes later is read. A block containing an unscored close
completes without changing recovery state; an unknown Auto result remains unknown until it leaves that procedure's Last-N window.

AUTO (owner 2026-11-15, "auto eval for each block count, set by last N 30 but minimal 15"): with `auto` on, every block count of every selected relation and Set lane
decides by itself whether it raises. Its evidence is the raise result of every position that opened while one of its procedures stood raised: level x the
position's net at volume 1 -- the same for a count that raises and for one that only observes, so a switched-off count is judged on paper exactly as a
switched-on one is judged on its real raises. The count raises while the PF of its newest `autoN` (30) results is at least `autoPf` (1.0); with fewer than
`autoMin` (15) results it keeps its manual switch. An unknown result blocks an Auto raise. A count that Auto switches on and has no ratio of its own raises by `autoRatio`.

The same book is driven by the engine (pulse_trader.place / close) and by scripts/block_rounds_study.py, so a measured figure is the engine's rule.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Dict, List, Optional, Tuple

RELATIONS = ("overall", "direction", "symbol", "indication", "type", "set", "step", "symbol+direction")
COUNTS = (1, 2, 3, 4, 5, 6)
RATIO_MAX, LEVEL_MAX, PAUSE_MAX, MULT_MAX = 2.0, 12, 6, 10.0
KEEP_BLOCKS = 12          # completed blocks kept per procedure for the statistics
AUTO_N, AUTO_MIN, AUTO_PF, AUTO_RATIO = 30, 15, 1.0, 1.0
AUTO_N_MAX = 200


@dataclass
class CountCfg:
    on: bool = False
    ratio: float = 0.0
    step: bool = False
    max_level: int = 1
    pause: int = 0


@dataclass
class RoundsCfg:
    enabled: bool = False
    relations: Tuple[str, ...] = ("step",)
    counts: Dict[int, CountCfg] = field(default_factory=lambda: {n: CountCfg() for n in COUNTS})
    max_mult: float = 2.0
    auto: bool = False
    auto_n: int = AUTO_N
    auto_min: int = AUTO_MIN
    auto_pf: float = AUTO_PF
    auto_ratio: float = AUTO_RATIO

    def to_json(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "relations": list(self.relations), "maxMult": self.max_mult, "auto": self.auto, "autoN": self.auto_n,
                "autoMin": self.auto_min, "autoPf": self.auto_pf, "autoRatio": self.auto_ratio,
                "counts": {str(n): {"on": c.on, "ratio": c.ratio, "step": c.step, "maxLevel": c.max_level, "pause": c.pause} for n, c in sorted(self.counts.items())}}


def _num(v: Any, default: float, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
        return default
    return min(hi, max(lo, float(v)))


def config_of(raw: Any) -> RoundsCfg:
    """The overlay's `blockRounds` object; anything unreadable is the default (off)."""
    cfg = RoundsCfg()
    if not isinstance(raw, dict):
        return cfg
    cfg.enabled = raw.get("enabled") is True
    rels = raw.get("relations")
    if isinstance(rels, list):
        picked = tuple(r for r in RELATIONS if r in rels)
        if picked:
            cfg.relations = picked
    cfg.max_mult = _num(raw.get("maxMult"), 2.0, 1.0, MULT_MAX)
    cfg.auto = raw.get("auto") is True
    cfg.auto_n = int(_num(raw.get("autoN"), AUTO_N, 1, AUTO_N_MAX))
    cfg.auto_min = int(min(cfg.auto_n, _num(raw.get("autoMin"), AUTO_MIN, 1, AUTO_N_MAX)))
    cfg.auto_pf = _num(raw.get("autoPf"), AUTO_PF, 0.5, 3.0)
    cfg.auto_ratio = _num(raw.get("autoRatio"), AUTO_RATIO, 0.05, RATIO_MAX)
    counts = raw.get("counts")
    if isinstance(counts, dict):
        for n in COUNTS:
            c = counts.get(str(n))
            if not isinstance(c, dict):
                continue
            cfg.counts[n] = CountCfg(on=c.get("on") is True, ratio=_num(c.get("ratio"), 0.0, 0.0, RATIO_MAX), step=c.get("step") is True,
                                     max_level=int(_num(c.get("maxLevel"), 1, 1, LEVEL_MAX)), pause=int(_num(c.get("pause"), 0, 0, PAUSE_MAX)))
    return cfg


def keys_of(symbol: str, side: int, pack: str, kind: str, step: int, lane: str, set_id: str) -> Dict[str, str]:
    """Every relation preserves Set, lane and direction ownership."""
    if not isinstance(set_id, str) or not set_id or not isinstance(lane, str) or not lane:
        raise ValueError("Block rounds require an exact Set and lane")
    s = "L" if side > 0 else "S"
    raw = {"overall": "all", "direction": s, "symbol": symbol, "indication": f"{pack}|{s}", "type": f"{pack}|{kind}|{s}", "set": "own",
           "step": f"{pack}|{0 if kind == 'trail' else int(step)}|{lane}|{s}", "symbol+direction": f"{symbol}|{s}"}
    # JSON escapes delimiters and marks this as a new identity, so legacy
    # pooled groups cannot silently become evidence for an individual Set.
    return {relation: json.dumps([3, set_id, lane, s, value], separators=(",", ":")) for relation, value in raw.items()}


def key_details(key: str) -> Optional[Tuple[str, str, str, str]]:
    try:
        value = json.loads(key)
    except (TypeError, ValueError):
        return None
    if (not isinstance(value, list) or len(value) != 5 or value[0] != 3 or value[3] not in ("L", "S") or
            not all(isinstance(part, str) and part for part in value[1:])):
        return None
    return value[1], value[2], value[3], value[4]


def policy_id(cfg: RoundsCfg, count: CountCfg) -> str:
    """Hash the inputs that affect this count's decision, independently of sibling counts."""
    fields = [count.on, count.ratio, count.step, count.max_level, count.pause, cfg.max_mult, cfg.auto]
    if cfg.auto:
        fields.extend((cfg.auto_n, cfg.auto_min, cfg.auto_pf, cfg.auto_ratio if not count.ratio else 0.0))
    return sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()[:24]


def new_group() -> Dict[str, Any]:
    return {"level": 0, "net": 0.0, "pause": 0, "seq": 0, "open": [], "blocks": 0, "lost": 0, "raises": 0, "recovered": 0, "last": []}


def block_done(g: Dict[str, Any], block: Dict[str, Any], c: CountCfg) -> None:
    """A completed block's result into its procedure (the rule in the module comment)."""
    unit, weighted = float(block["unit"]), float(block["weighted"])
    g["blocks"] = int(g.get("blocks", 0)) + 1
    g["lost"] = int(g.get("lost", 0)) + (1 if unit < 0 else 0)
    last = g.setdefault("last", [])
    last.append([round(unit, 8), round(weighted, 8), int(g["level"])])
    del last[:-KEEP_BLOCKS]
    if g["pause"] > 0:
        g["pause"] -= 1
        return
    if g["level"] == 0:
        if unit < 0:
            g["level"], g["net"] = 1, weighted          # the losing block starts the deficit the raise has to earn back
            g["raises"] = int(g.get("raises", 0)) + 1
        elif c.pause:
            g["pause"] = c.pause
        return
    g["net"] += weighted
    if g["net"] >= 0:
        g["level"], g["net"], g["pause"] = 0, 0.0, c.pause
        g["recovered"] = int(g.get("recovered", 0)) + 1
    elif c.step and unit < 0:
        g["level"] = min(c.max_level, g["level"] + 1)


class RoundsBook:
    """Every procedure's state, the open positions' block membership, and the file both survive a restart in."""

    def __init__(self, path: str = "", cfg: Optional[RoundsCfg] = None) -> None:
        self.path = path
        self.cfg = cfg or RoundsCfg()
        self.groups: Dict[str, Dict[str, Any]] = {}
        self.members: Dict[str, Dict[str, Any]] = {}
        self.evidence: Dict[str, List[Optional[float]]] = {} # exact procedure -> newest raise results; None remains unknown
        self.error = ""
        self.load()

    @property
    def cfg(self) -> RoundsCfg:
        return self._cfg

    @cfg.setter
    def cfg(self, value: RoundsCfg) -> None:
        # Copy through the normalized settings so reloading cannot mutate a policy
        # already bound to a book. The engine replaces cfg at each settings reload.
        self._cfg = config_of(value.to_json())
        self._policy_ids = {n: policy_id(self._cfg, c) for n, c in self._cfg.counts.items()}

    def gid(self, relation: str, key: str, n: int) -> str:
        return f"{relation}\t{key}\t{self._policy_ids[n]}\t{n}"

    def _procedures(self, keys: Dict[str, str]):
        for rel in self.cfg.relations:
            key = keys.get(rel)
            if key is None or key_details(key) is None:
                continue
            for n in COUNTS:
                yield rel, n, self.gid(rel, key, n)

    def eid(self, relation: str, key: str, n: int) -> str:
        return self.gid(relation, key, n)

    def auto_verdict(self, relation: str, key: str, n: int) -> Dict[str, Any]:
        """Auto verdict for this exact Set/lane/relation/key/count, never a sibling's history."""
        c = self.cfg.counts.get(n) or CountCfg()
        ev = self.evidence.get(self.eid(relation, key, n), [])[-self.cfg.auto_n:]
        if not self.cfg.auto:
            return {"raises": bool(c.on), "source": "manual", "ratio": c.ratio, "results": len(ev), "pf": None}
        if any(x is None or not math.isfinite(x) for x in ev):
            return {"raises": False, "source": "unknown", "ratio": c.ratio, "results": len(ev), "pf": None}
        gains = math.fsum(x for x in ev if x > 0)
        losses = -math.fsum(x for x in ev if x < 0)
        pf = gains / losses if losses > 0 else (99.0 if gains > 0 else 0.0)
        if len(ev) < self.cfg.auto_min:
            return {"raises": bool(c.on), "source": "manual", "ratio": c.ratio, "results": len(ev), "pf": round(pf, 4)}
        on = pf + 1e-12 >= self.cfg.auto_pf
        return {"raises": on, "source": "auto", "ratio": c.ratio if c.ratio > 0 else self.cfg.auto_ratio, "results": len(ev), "pf": round(pf, 4)}

    def requested_add(self, relation: str, key: str, n: int, level: int) -> float:
        if level <= 0:
            return 0.0
        verdict = self.auto_verdict(relation, key, n)
        return verdict["ratio"] * level if verdict["raises"] else 0.0

    def ask(self, keys: Dict[str, str]) -> Tuple[float, List[str]]:
        """(volume multiple, the procedures behind it) for a position that would open now; reads only, registers nothing."""
        if not self.cfg.enabled or self.error:
            return 1.0, []
        add, why = 0.0, []
        for rel, n, gid in self._procedures(keys):
            c = self.cfg.counts.get(n)
            g = self.groups.get(gid)
            if c is None or g is None or g["level"] <= 0:
                continue
            own = self.requested_add(rel, keys[rel], n, g["level"])
            if own <= 0:
                continue
            add += own
            why.append(f"{rel}:N{n}:L{g['level']}")
        if add <= 0:
            return 1.0, []
        return min(self.cfg.max_mult, 1.0 + add), why

    def opened(self, pid: str, keys: Dict[str, str], mult: float = 1.0) -> None:
        """Record the requested individual sizing and the actual aggregate multiple at entry."""
        if not self.cfg.enabled or not pid or pid in self.members:
            return
        try:
            multiple = float(mult)
        except (ValueError, TypeError):
            raise ValueError("Block rounds require a known positive multiple") from None
        if not math.isfinite(multiple) or multiple <= 0:
            raise ValueError("Block rounds require a known positive multiple")
        at = []
        for rel, n, gid in self._procedures(keys):
            g = self.groups.get(gid)
            if g is None:
                c = self.cfg.counts[n]
                g = self.groups[gid] = {**new_group(), "lifecycle": [c.step, c.max_level, c.pause]}
            if not g["open"] or g["open"][-1]["n"] >= n:
                g["seq"] = int(g.get("seq", 0)) + 1
                g["open"].append({"id": g["seq"], "n": 0, "closed": 0, "unit": 0.0, "weighted": 0.0})
            g["open"][-1]["n"] += 1
            level = int(g["level"])
            own = min(self.cfg.max_mult, 1.0 + self.requested_add(rel, keys[rel], n, level))
            at.append([gid, g["open"][-1]["id"], level, own])
        if at:
            self.members[pid] = {"mult": multiple, "at": at}

    def closed(self, pid: str, net: Optional[float], scored: bool = True) -> None:
        """The position's net result (at volume 1) into its blocks; blocks settle in the order they were formed. net None or scored False = no result."""
        m = self.members.pop(pid, None)
        if m is None:
            return
        counts = bool(scored) and isinstance(net, (int, float)) and not isinstance(net, bool) and math.isfinite(net)
        for item in m["at"]:
            gid, bid = item[0], item[1]
            level = int(item[2]) if len(item) > 2 else 0
            g = self.groups.get(gid)
            if g is None:
                continue
            lifecycle = g.get("lifecycle")
            policy_bound = len(item) == 4 and isinstance(lifecycle, list) and len(lifecycle) == 3
            if level > 0 and policy_bound:
                # Unknown outcomes occupy their own place in Last-N; dropping
                # them would bring an older observation into the window.
                ev = self.evidence.setdefault(gid, [])
                outcome = level * float(net) if counts else None
                ev.append(outcome if outcome is not None and math.isfinite(outcome) else None)
                del ev[:-AUTO_N_MAX]
            blk = next((b for b in g["open"] if b["id"] == bid), None)
            if blk is None:
                continue
            blk["closed"] += 1
            if counts and policy_bound:
                blk["unit"] += float(net)
                blk["weighted"] += float(net) * float(item[3])
                blk["scored"] = int(blk.get("scored", 0)) + 1
            n = int(gid.rsplit("\t", 1)[1])
            while g["open"] and g["open"][0]["n"] >= n and g["open"][0]["closed"] >= g["open"][0]["n"]:
                done = g["open"].pop(0)
                if policy_bound and int(done.get("scored", 0)) == int(done["n"]):
                    c = CountCfg(step=lifecycle[0], max_level=lifecycle[1], pause=lifecycle[2])
                    block_done(g, done, c)
                else:
                    g["unscoredBlocks"] = int(g.get("unscoredBlocks", 0)) + 1

    def forget(self, live: Any) -> int:
        """Drop the membership of positions that no longer exist (a book restored without them); their blocks complete without a result of theirs."""
        gone = [p for p in self.members if p not in live]
        for p in gone:
            self.closed(p, None, scored=False)
        return len(gone)

    # ---- persistence ----
    def to_json(self) -> Dict[str, Any]:
        return {"version": 3, "groups": self.groups, "members": self.members, "evidence": self.evidence}

    def save(self) -> None:
        if not self.path:
            return
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.to_json(), f)
            os.replace(tmp, self.path)
        except Exception:
            self.error = "Block rounds state could not be saved; raises disabled"

    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path) as f:
                d = json.load(f)
            if not isinstance(d, dict) or d.get("version") not in (1, 2, 3) or not isinstance(d.get("groups"), dict) or not isinstance(d.get("members"), dict):
                raise ValueError("shape")
            self.groups, self.members = d["groups"], d["members"]
            ev = d.get("evidence")
            self.evidence = {str(k): [float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None
                                      for x in v][-AUTO_N_MAX:] for k, v in ev.items() if isinstance(v, list)} if isinstance(ev, dict) else {}
        except Exception:
            self.groups, self.members = {}, {}
            self.error = "Block rounds state unreadable; raises disabled until the file is removed"

    # ---- statistics ----
    def snapshot(self, limit: int = 200) -> Dict[str, Any]:
        per: Dict[str, Dict[str, Any]] = {}
        rows, auto_active, auto_idle = [], [], []
        auto_total, legacy_groups, inactive_groups = 0, 0, 0
        for gid, g in self.groups.items():
            parts = gid.split("\t")
            identity = key_details(parts[1]) if len(parts) == 4 else None
            if identity is None:
                legacy_groups += 1
                continue
            rel, key, policy, n = parts
            if rel not in self.cfg.relations or not n.isdigit() or self._policy_ids.get(int(n)) != policy:
                inactive_groups += 1
                continue
            set_id, lane, side, relation_key = identity
            c = self.cfg.counts.get(int(n)) or CountCfg()
            a = per.setdefault(n, {"count": int(n), "on": c.on, "ratio": c.ratio, "step": c.step, "maxLevel": c.max_level, "pause": c.pause,
                                   "groups": 0, "blocks": 0, "lost": 0, "raises": 0, "recovered": 0, "raisedNow": 0, "pausedNow": 0})
            a["groups"] += 1
            for k in ("blocks", "lost", "raises", "recovered"):
                a[k] += int(g.get(k, 0))
            a["raisedNow"] += 1 if g["level"] > 0 else 0
            a["pausedNow"] += 1 if g["pause"] > 0 else 0
            auto_total += 1
            shown = auto_active if g["level"] > 0 else auto_idle
            if len(shown) < limit:
                shown.append({"set": set_id, "lane": lane, "side": side, "policy": policy, "relation": rel, "key": relation_key,
                              "count": int(n), **self.auto_verdict(rel, key, int(n))})
            if g["level"] > 0 or g["pause"] > 0 or g["open"]:
                rows.append({"set": set_id, "lane": lane, "side": side, "policy": policy, "relation": rel, "key": relation_key, "count": int(n), "level": g["level"], "net": round(g["net"], 6), "pause": g["pause"],
                             "blocks": int(g.get("blocks", 0)), "openBlocks": len(g["open"]), "last": g.get("last", [])[-3:]})
        rows.sort(key=lambda r: (-r["level"], r["relation"], r["key"], r["count"]))
        for n in COUNTS:
            c = self.cfg.counts.get(n) or CountCfg()
            per.setdefault(str(n), {"count": n, "on": c.on, "ratio": c.ratio, "step": c.step, "maxLevel": c.max_level, "pause": c.pause,
                                    "groups": 0, "blocks": 0, "lost": 0, "raises": 0, "recovered": 0, "raisedNow": 0, "pausedNow": 0})
        auto = (auto_active + auto_idle)[:limit]
        return {"config": self.cfg.to_json(), "error": self.error, "openPositions": len(self.members), "auto": auto,
                "autoTotal": auto_total, "legacyGroups": legacy_groups, "inactivePolicyGroups": inactive_groups,
                "byCount": [per[str(n)] for n in COUNTS], "groups": rows[:limit], "groupsTotal": auto_total}


def self_test() -> List[Tuple[str, bool, str]]:
    return [(name, ok, "") for name, ok in _self_test()]


def _self_test() -> List[Tuple[str, bool]]:
    out: List[Tuple[str, bool]] = []
    cfg = config_of({"enabled": True, "relations": ["overall"], "maxMult": 5,
                     "counts": {"2": {"on": True, "ratio": 0.5, "step": True, "maxLevel": 3, "pause": 1}, "3": {"on": True, "ratio": 0.25}}})
    b = RoundsBook(cfg=cfg)
    k = keys_of("AUSDT", 1, "general", "base", 5, "pos", "s1")

    def trade(pid: str, net: float) -> float:
        m, _ = b.ask(k)
        b.opened(pid, k, m)
        b.closed(pid, net)
        return m

    ms = [trade("a", -1.0), trade("b", -1.0)]                    # block of 2 lost (-2): N2 level 1
    out.append(("no raise before a block has lost", ms == [1.0, 1.0]))
    m3 = trade("c", -1.0)                                        # N2 raised 0.5; N3 block (a, b, c) completes lost afterwards
    out.append(("a losing block of 2 raises the next position by its ratio", abs(m3 - 1.5) < 1e-12))
    m4 = trade("d", -1.0)                                        # N2 0.5 + N3 0.25; N2's second block (c, d) lost: step to level 2
    out.append(("every count adds its own ratio", abs(m4 - 1.75) < 1e-12))
    m5 = trade("e", 9.0)                                         # N2 level 2 -> 1.0, N3 0.25
    out.append(("step raises one more level from the base volume", abs(m5 - 2.25) < 1e-12))
    m6 = trade("f", 9.0)                                         # N2's block (e, f) wins: net back above zero -> level 0, pause 1
    g2 = b.groups[b.gid("overall", k["overall"], 2)]
    out.append(("a recovered raise returns to base and pauses", g2["level"] == 0 and g2["pause"] == 1 and abs(m6 - 2.25) < 1e-12))
    m7 = trade("g", -5.0)
    m8 = trade("h", -5.0)                                        # paused block lost: starts nothing
    out.append(("a paused block starts nothing", g2["level"] == 0 and g2["pause"] == 0 and b.ask(k)[0] == 1.0))
    out.append(("the multiple never exceeds the ceiling", max(ms + [m3, m4, m5, m6, m7, m8]) <= cfg.max_mult + 1e-12))
    b2 = RoundsBook(cfg=config_of({"enabled": True, "relations": ["overall"], "counts": {"1": {"on": True, "ratio": 1.0}}}))
    b2.opened("x", k, 1.0)
    b2.closed("x", -1.0, scored=False)
    out.append(("an unscored close teaches nothing", b2.ask(k)[0] == 1.0 and b2.groups[b2.gid("overall", k["overall"], 1)]["blocks"] == 0))
    off = RoundsBook(cfg=config_of({"relations": ["overall"], "counts": {"1": {"on": True, "ratio": 1.0}}}))
    off.opened("x", k, 1.0)
    out.append(("off: nothing is tracked or raised", not off.groups and off.ask(k) == (1.0, [])))
    # Auto: a count raises while its newest autoN raise results (level x net) read PF >= autoPf, with at least autoMin of them; else its manual switch
    acfg = config_of({"enabled": True, "relations": ["overall"], "maxMult": 5, "auto": True, "autoN": 4, "autoMin": 3, "autoPf": 1.0,
                      "counts": {"1": {"on": False, "ratio": 0.0}}})
    ab = RoundsBook(cfg=acfg)
    out.append(("auto: fewer than autoMin results keep the manual switch", ab.auto_verdict("overall", k["overall"], 1) == {"raises": False, "source": "manual", "ratio": 0.0, "results": 0, "pf": 0.0}))
    ab.evidence[ab.eid("overall", k["overall"], 1)] = [1.0, 1.0, -0.5]
    v = ab.auto_verdict("overall", k["overall"], 1)
    out.append(("auto: a paying count raises at the auto ratio", v["raises"] and v["source"] == "auto" and v["ratio"] == AUTO_RATIO and abs(v["pf"] - 4.0) < 1e-9))
    ab.evidence[ab.eid("overall", k["overall"], 1)] = [9.0, -1.0, -1.0, -1.0, -1.0]                 # only the newest 4 are read: PF 0
    out.append(("auto: only the newest autoN results are read", not ab.auto_verdict("overall", k["overall"], 1)["raises"]))
    ab.evidence.clear()
    for i, net in enumerate([-1.0, 2.0, 2.0, 2.0]):                                  # N1: the loss raises; the three raised positions win
        m, _ = ab.ask(k)
        ab.opened(f"p{i}", k, m)
        ab.closed(f"p{i}", net)
    ev = ab.evidence.get(ab.eid("overall", k["overall"], 1), [])
    out.append(("auto: an observing count records level x net of its raised positions", ev[:1] == [2.0] and len(ev) >= 1))
    return out


if __name__ == "__main__":
    bad = [n for n, ok in _self_test() if not ok]
    print("block_rounds self-test:", "ok" if not bad else bad)
    raise SystemExit(1 if bad else 0)
