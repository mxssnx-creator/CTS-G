"""Block rounds (owner definition, 2026-11-11): Block as volume control over rounds of N positions.

A PROCEDURE is one (relation, group key, block count N). Its group's positions form consecutive blocks of N in the order they open; a block is complete
when all of its N positions are closed, and its result is the net of them (unit = every position at volume 1, weighted = at the volume it had).

  - A losing block raises the NEXT positions of the group: 1 + ratio x level of the ORIGINAL base volume (level 1 after the first loss).
  - The raise is kept while the group is losing: until the volume-weighted net since the losing block that started it is back at or above zero.
  - Step: while still losing, every further losing block raises the level by one more (up to maxLevel), each time by the ratio from the base volume.
  - Pause: after a positive block at base volume, and after a recovered raise, the next `pause` blocks run at base volume and start nothing.

Every block count 1..6 of every selected relation runs its own procedure on its own blocks, level, net and pause: none reads another's state. A
position's volume multiple is 1 + the sum of (ratio x level) over the procedures it belongs to whose count is switched on, at most maxMult. A count
that is off (or at ratio 0) still forms its blocks and keeps its record, so every count is visible in the statistics; it raises nothing.

The multiple is decided at the open from blocks completed before it: nothing that closes later is read. A close the engine cannot score (unverified,
made while the engine was down) leaves its block without a result of its own (the block completes on its other positions).

The same book is driven by the engine (pulse_trader.place / close) and by scripts/block_rounds_study.py, so a measured figure is the engine's rule.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

RELATIONS = ("overall", "direction", "symbol", "indication", "type", "set", "step", "symbol+direction")
COUNTS = (1, 2, 3, 4, 5, 6)
RATIO_MAX, LEVEL_MAX, PAUSE_MAX, MULT_MAX = 2.0, 6, 6, 5.0
KEEP_BLOCKS = 12          # completed blocks kept per procedure for the statistics


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

    def to_json(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "relations": list(self.relations), "maxMult": self.max_mult,
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
    """A position's group key under every relation."""
    s = "L" if side > 0 else "S"
    return {"overall": "all", "direction": s, "symbol": symbol, "indication": f"{pack}|{s}", "type": f"{pack}|{kind}|{s}", "set": f"{set_id}|{lane}",
            "step": f"{pack}|{0 if kind == 'trail' else int(step)}|{lane}|{s}", "symbol+direction": f"{symbol}|{s}"}


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
        self.groups: Dict[str, Dict[str, Any]] = {}          # "<relation>\t<key>\t<N>" -> procedure
        self.members: Dict[str, Dict[str, Any]] = {}         # position id -> {"mult": m, "at": [[group id, block seq], ...]}
        self.error = ""
        self.load()

    @staticmethod
    def gid(relation: str, key: str, n: int) -> str:
        return f"{relation}\t{key}\t{n}"

    def _procedures(self, keys: Dict[str, str]):
        for rel in self.cfg.relations:
            key = keys.get(rel)
            if key is None:
                continue
            for n in COUNTS:
                yield rel, n, self.gid(rel, key, n)

    def ask(self, keys: Dict[str, str]) -> Tuple[float, List[str]]:
        """(volume multiple, the procedures behind it) for a position that would open now; reads only, registers nothing."""
        if not self.cfg.enabled or self.error:
            return 1.0, []
        add, why = 0.0, []
        for rel, n, gid in self._procedures(keys):
            c = self.cfg.counts.get(n)
            g = self.groups.get(gid)
            if c is None or not c.on or c.ratio <= 0 or g is None or g["level"] <= 0:
                continue
            add += c.ratio * g["level"]
            why.append(f"{rel}:N{n}:L{g['level']}")
        if add <= 0:
            return 1.0, []
        return min(self.cfg.max_mult, 1.0 + add), why

    def opened(self, pid: str, keys: Dict[str, str], mult: float = 1.0) -> None:
        """The position joins the newest block of each of its procedures at the multiple it really opened with."""
        if not self.cfg.enabled or not pid or pid in self.members:
            return
        at = []
        for _rel, n, gid in self._procedures(keys):
            g = self.groups.setdefault(gid, new_group())
            if not g["open"] or g["open"][-1]["n"] >= n:
                g["seq"] = int(g.get("seq", 0)) + 1
                g["open"].append({"id": g["seq"], "n": 0, "closed": 0, "unit": 0.0, "weighted": 0.0})
            g["open"][-1]["n"] += 1
            at.append([gid, g["open"][-1]["id"]])
        self.members[pid] = {"mult": float(mult) if mult and mult > 0 else 1.0, "at": at}

    def closed(self, pid: str, net: Optional[float], scored: bool = True) -> None:
        """The position's net result (at volume 1) into its blocks; blocks settle in the order they were formed. net None or scored False = no result."""
        m = self.members.pop(pid, None)
        if m is None:
            return
        counts = bool(scored) and isinstance(net, (int, float)) and not isinstance(net, bool) and net == net
        for gid, bid in m["at"]:
            g = self.groups.get(gid)
            if g is None:
                continue
            blk = next((b for b in g["open"] if b["id"] == bid), None)
            if blk is None:
                continue
            blk["closed"] += 1
            if counts:
                blk["unit"] += float(net)
                blk["weighted"] += float(net) * float(m["mult"])
                blk["scored"] = int(blk.get("scored", 0)) + 1
            n = int(gid.rsplit("\t", 1)[1])
            c = self.cfg.counts.get(n) or CountCfg()
            while g["open"] and g["open"][0]["n"] >= n and g["open"][0]["closed"] >= g["open"][0]["n"]:
                done = g["open"].pop(0)
                if int(done.get("scored", 0)) > 0:
                    block_done(g, done, c)

    def forget(self, live: Any) -> int:
        """Drop the membership of positions that no longer exist (a book restored without them); their blocks complete without a result of theirs."""
        gone = [p for p in self.members if p not in live]
        for p in gone:
            self.closed(p, None, scored=False)
        return len(gone)

    # ---- persistence ----
    def to_json(self) -> Dict[str, Any]:
        return {"version": 1, "groups": self.groups, "members": self.members}

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
            if not isinstance(d, dict) or d.get("version") != 1 or not isinstance(d.get("groups"), dict) or not isinstance(d.get("members"), dict):
                raise ValueError("shape")
            self.groups, self.members = d["groups"], d["members"]
        except Exception:
            self.groups, self.members = {}, {}
            self.error = "Block rounds state unreadable; raises disabled until the file is removed"

    # ---- statistics ----
    def snapshot(self, limit: int = 200) -> Dict[str, Any]:
        per: Dict[str, Dict[str, Any]] = {}
        rows = []
        for gid, g in self.groups.items():
            rel, key, n = gid.split("\t")
            c = self.cfg.counts.get(int(n)) or CountCfg()
            a = per.setdefault(n, {"count": int(n), "on": c.on, "ratio": c.ratio, "step": c.step, "maxLevel": c.max_level, "pause": c.pause,
                                   "groups": 0, "blocks": 0, "lost": 0, "raises": 0, "recovered": 0, "raisedNow": 0, "pausedNow": 0})
            a["groups"] += 1
            for k in ("blocks", "lost", "raises", "recovered"):
                a[k] += int(g.get(k, 0))
            a["raisedNow"] += 1 if g["level"] > 0 else 0
            a["pausedNow"] += 1 if g["pause"] > 0 else 0
            if g["level"] > 0 or g["pause"] > 0 or g["open"]:
                rows.append({"relation": rel, "key": key, "count": int(n), "level": g["level"], "net": round(g["net"], 6), "pause": g["pause"],
                             "blocks": int(g.get("blocks", 0)), "openBlocks": len(g["open"]), "last": g.get("last", [])[-3:]})
        rows.sort(key=lambda r: (-r["level"], r["relation"], r["key"], r["count"]))
        for n in COUNTS:
            c = self.cfg.counts.get(n) or CountCfg()
            per.setdefault(str(n), {"count": n, "on": c.on, "ratio": c.ratio, "step": c.step, "maxLevel": c.max_level, "pause": c.pause,
                                    "groups": 0, "blocks": 0, "lost": 0, "raises": 0, "recovered": 0, "raisedNow": 0, "pausedNow": 0})
        return {"config": self.cfg.to_json(), "error": self.error, "openPositions": len(self.members),
                "byCount": [per[str(n)] for n in COUNTS], "groups": rows[:limit], "groupsTotal": len(self.groups)}


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
    g2 = b.groups[b.gid("overall", "all", 2)]
    out.append(("a recovered raise returns to base and pauses", g2["level"] == 0 and g2["pause"] == 1 and abs(m6 - 2.25) < 1e-12))
    m7 = trade("g", -5.0)
    m8 = trade("h", -5.0)                                        # paused block lost: starts nothing
    out.append(("a paused block starts nothing", g2["level"] == 0 and g2["pause"] == 0 and b.ask(k)[0] == 1.0))
    out.append(("the multiple never exceeds the ceiling", max(ms + [m3, m4, m5, m6, m7, m8]) <= cfg.max_mult + 1e-12))
    b2 = RoundsBook(cfg=config_of({"enabled": True, "relations": ["overall"], "counts": {"1": {"on": True, "ratio": 1.0}}}))
    b2.opened("x", k, 1.0)
    b2.closed("x", -1.0, scored=False)
    out.append(("an unscored close teaches nothing", b2.ask(k)[0] == 1.0 and b2.groups[b2.gid("overall", "all", 1)]["blocks"] == 0))
    off = RoundsBook(cfg=config_of({"relations": ["overall"], "counts": {"1": {"on": True, "ratio": 1.0}}}))
    off.opened("x", k, 1.0)
    out.append(("off: nothing is tracked or raised", not off.groups and off.ask(k) == (1.0, [])))
    return out


if __name__ == "__main__":
    bad = [n for n, ok in _self_test() if not ok]
    print("block_rounds self-test:", "ok" if not bad else bad)
    raise SystemExit(1 if bad else 0)
