"""Relation lanes: last-N lanes of the post-Base orders by relation, next to the ordinary per-configuration lanes.

A per-configuration lane (one Set: pack x direction x type x stop x TP step) stays what decides whether an entry opens. A relation
lane pools what the Sets of one relation delivered AFTER Base and judges that pool on its own last-N windows:

  overall      every Set of one direction, whatever its indication                   -- the overall lane
  indication   every Set of one indication (pack) and direction                      -- a major lane
  type         ... of one Set type of that indication (base / exit / trail)          -- a major lane
  range        ... of one named TP range (micro .. wide; "trail" for trailing)       -- a minor lane
  step         ... of one TP step, i.e. one target distance ("arm" for trailing)     -- a minor lane

(The names follow the lane policies' scopes: overall, indication, type; range and step are the distance values. A configured
indication entry's variants -- its Last-N profiles and symbols -- are one indication here: the lane pools them, one entry once.)

Each relation lane is a chain of new Sets built only from orders that were valid after Base:

  Relation Set (Main stage)   the member Sets' Main-Set orders (the orders each member opened while its own Base windows passed), ONE
                              observation per entry (symbol, entry minute, direction): the mean net of the member orders of that entry
                              that have closed. A pack's Sets trade the same signals and differ in their exits, so a pool of rows counts
                              one entry once per member (about 50 times); here it counts once.
  Relation Real Set           the entries opened while the Relation Set's Main window passed (its last mainN observations at or above
                              the floor, read from the closes strictly before that entry), judged on its own last realN observations.

A relation lane passes when its Main window and (with the Real stage on) its Real window pass now. Passing lanes raise the volume of
a positive-lane entry of a member Set: multiple = 1 + the sum of the passing relations' ratios, at most maxMult. A relation lane never
opens, blocks, closes or scores a position; a failing lane leaves the base volume. Volume weights results, it does not change a
unit-return PF.

Everything here is a pure function of the rows it is given (exit time, entry time, entry key, net), so the engine (SetBook) and the
walk-forward simulator (scripts/stage_eval.py) compute the same lanes from the same orders. An observation is ordered by its entry's
latest close seen so far, and the verdict at an entry reads only closes strictly before it: no result closed later can move it.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from itertools import islice
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from position_cost import MIN_PF_FLOOR, tp_range_of

RELATIONS = ("overall", "indication", "type", "range", "step")
ROLE = {"overall": "overall", "indication": "major", "type": "major", "range": "minor", "step": "minor"}
RATIO_MAX = 2.0                 # one relation adds at most 2 x the base volume
MULT_MAX = 5.0                  # the trader's own bound on any volume multiple (pulse_trader.VOLUME_MULT_MAX)
N_MIN, N_MAX = 1, 200
# Defaults: see docs/verification/relation-lanes-20261110.md for the measurement behind them.
DEFAULT_RATIOS = {"overall": 0.25, "indication": 0.25, "type": 0.25, "range": 0.0, "step": 0.0}
DEFAULT_MAIN_N = 25
DEFAULT_REAL_N = 15
DEFAULT_MIN_PF = 1.1
DEFAULT_MAX_MULT = 2.0
NEUTRAL_EPS = 1e-12


@dataclass(frozen=True)
class RelationConfig:
    enabled: bool = False
    on: Tuple[str, ...] = RELATIONS                  # the relations whose lanes are built
    ratios: Tuple[Tuple[str, float], ...] = tuple(DEFAULT_RATIOS.items())
    main_n: int = DEFAULT_MAIN_N
    real_n: int = DEFAULT_REAL_N                     # 0 = no Real stage: the Main window alone
    min_pf: float = DEFAULT_MIN_PF
    max_mult: float = DEFAULT_MAX_MULT
    full_window: bool = False                        # True: a window must hold its full N (the strict rule); False: judged on what it has (at least one)

    def ratio(self, relation: str) -> float:
        return dict(self.ratios).get(relation, 0.0)

    def as_dict(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "relations": {r: {"on": r in self.on, "ratio": self.ratio(r), "role": ROLE[r]} for r in RELATIONS},
                "mainN": self.main_n, "realN": self.real_n, "minPf": self.min_pf, "maxMult": self.max_mult, "fullWindow": self.full_window}


def _num(v: Any, lo: float, hi: float, default: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return default
    return min(hi, max(lo, float(v)))


def config_of(raw: Any) -> RelationConfig:
    """The overlay value relationLanes -> RelationConfig. Missing or unreadable parts take their defaults; nothing here can enable the
    lanes except an explicit "enabled": true. Bounds: N 1..200 (realN 0 = no Real stage), minPf at least MIN_PF_FLOOR, ratio 0..2 per
    relation, maxMult 1..5."""
    if not isinstance(raw, dict):
        return RelationConfig()
    rel = raw.get("relations") if isinstance(raw.get("relations"), dict) else {}
    on, ratios = [], []
    for r in RELATIONS:
        item = rel.get(r)
        if isinstance(item, dict):
            if item.get("on", True) is not False:
                on.append(r)
            ratios.append((r, round(_num(item.get("ratio"), 0.0, RATIO_MAX, DEFAULT_RATIOS[r]), 4)))
        elif item is False:
            ratios.append((r, 0.0))
        else:
            on.append(r)
            ratios.append((r, round(_num(item, 0.0, RATIO_MAX, DEFAULT_RATIOS[r]), 4) if item is not None and item is not True else DEFAULT_RATIOS[r]))
    real_n = int(_num(raw.get("realN"), 0, N_MAX, DEFAULT_REAL_N))
    return RelationConfig(enabled=raw.get("enabled") is True, on=tuple(on), ratios=tuple(ratios),
                          main_n=int(_num(raw.get("mainN"), N_MIN, N_MAX, DEFAULT_MAIN_N)), real_n=real_n,
                          min_pf=max(MIN_PF_FLOOR, _num(raw.get("minPf"), 1.0, 100.0, DEFAULT_MIN_PF)),
                          max_mult=_num(raw.get("maxMult"), 1.0, MULT_MAX, DEFAULT_MAX_MULT),
                          full_window=raw.get("fullWindow") is True)


def side_tag(side: Any) -> str:
    try:
        s = int(side)
    except (TypeError, ValueError):
        s = 0
    return "L" if s > 0 else "S" if s < 0 else "B"


def step_tag(kind: str, step: int, trail_arm: float = 0.0) -> str:
    """The distance value of a Set: its TP step, or for a trailing Set its arm (in 0.01 % units, so 0.3 % is arm30)."""
    if kind == "trail":
        return "arm" + str(int(round(float(trail_arm or 0.0) * 10000)))
    return "st" + str(int(step))


def range_tag(kind: str, step_bucket: int) -> str:
    return "trail" if kind == "trail" else tp_range_of(int(step_bucket))


def cell_of(pack: str, side: Any, kind: str, step: int, step_bucket: int, trail_arm: float = 0.0) -> Tuple[str, str, str, str, str]:
    """The finest relation cell of a Set: (indication, direction, type, range, step). Every relation key is a projection of it. The indication is the
    pack without a configured entry's own identity ("~cfg..."): the entry's Last-N profiles and symbols are one indication."""
    k = str(kind or "base")
    return (str(pack).split("~cfg", 1)[0], side_tag(side), k, range_tag(k, step_bucket), step_tag(k, step, trail_arm))


def keys_of(cell: Sequence[str]) -> Dict[str, str]:
    """relation -> lane id of a cell. The ids are their own namespace ("rel:..."), never a Set id."""
    pack, side, kind, rng, step = cell
    head = f"rel:{pack}:{side}"
    return {"overall": f"rel:*:{side}:overall", "indication": head + ":indication", "type": f"{head}:type:{kind}", "range": f"{head}:range:{rng}",
            "step": f"{head}:step:{step}"}


def _pf(g: float, l_: float, n: int) -> float:
    return g / l_ if l_ > 0 else (99.0 if g > 0 else (1.0 if n > 0 else 0.0))


def _tail(groups: "OrderedDict[Any, List[float]]", n: int) -> Tuple[int, float, float]:
    """(observations, gains, losses) of the newest n observations: each the mean net of its entry's closed member orders."""
    have, g, l_ = 0, 0.0, 0.0
    for s, c in islice(reversed(groups.values()), n):
        v = s / c
        have += 1
        if v > NEUTRAL_EPS:
            g += v
        elif v < -NEUTRAL_EPS:
            l_ -= v
    return have, g, l_


def _passes(have: int, g: float, l_: float, n: int, floor: float, full: bool) -> bool:
    if have < (n if full else 1):
        return False
    return _pf(g, l_, have) + 1e-9 >= floor


def lane_state(rows: Iterable[Tuple[float, float, Any, float, int]], cfg: RelationConfig, trace: Optional[Dict[Any, bool]] = None) -> Dict[str, Any]:
    """One relation lane from its member rows (exit t, entry t, entry key, net sum, order count): rows of one entry key with one exit
    time may arrive pre-added (sum, count); a single order is (exit, entry, key, net, 1). Returns the lane's figures now:
      entries / orders             distinct entries and member orders in the Relation Set
      mainHave/Gp/Gl/Pf/Ok         its Main window (the newest mainN observations)
      realEntries, realHave/...    the Relation Real Set (entries opened while the Main window passed) and its window
      ok                           the lane passes: Main, and Real when the Real stage is on
    Events are walked in time order; at one instant an entry is judged before the closes stamped at that instant (strictly earlier),
    and closes of one instant are taken in the order of their entry key, so the result does not depend on the order the rows arrive in.
    trace (a dict) receives entry key -> the Main verdict that entry was opened under (the Relation Real Set's membership)."""
    events: List[Tuple[float, int, Any, float, int]] = []
    opened: Dict[Any, float] = {}
    orders = 0
    for exit_t, entry_t, key, net, count in rows:
        events.append((float(exit_t), 1, key, float(net), int(count)))
        orders += int(count)
        t0 = opened.get(key)
        if t0 is None or entry_t < t0:
            opened[key] = float(entry_t)
    events.extend((t, 0, key, 0.0, 0) for key, t in opened.items())
    events.sort()
    groups: "OrderedDict[Any, List[float]]" = OrderedDict()
    real: "OrderedDict[Any, List[float]]" = OrderedDict()
    flagged: Dict[Any, bool] = {}
    main_n, real_n, floor, full = cfg.main_n, cfg.real_n, cfg.min_pf, cfg.full_window
    verdict: Optional[bool] = None            # the Main verdict since the last close (an entry burst reads it once)
    for _t, what, key, net, count in events:
        if what == 0:
            if verdict is None:
                verdict = _passes(*_tail(groups, main_n), main_n, floor, full)
            flagged[key] = verdict
            continue
        g = groups.get(key)
        if g is None:
            groups[key] = [net, count]
        else:
            g[0] += net
            g[1] += count
            groups.move_to_end(key)
        if flagged.get(key):
            r = real.get(key)
            if r is None:
                real[key] = [net, count]
            else:
                r[0] += net
                r[1] += count
                real.move_to_end(key)
        verdict = None
    if trace is not None:
        trace.update(flagged)
    mh, mg, ml = _tail(groups, main_n)
    main_ok = _passes(mh, mg, ml, main_n, floor, full)
    out = {"entries": len(groups), "orders": orders, "mainHave": mh, "mainGp": mg, "mainGl": ml, "mainPf": round(_pf(mg, ml, mh), 4), "mainOk": main_ok,
           "realEntries": len(real), "realHave": 0, "realGp": 0.0, "realGl": 0.0, "realPf": 0.0, "realOk": True, "ok": main_ok}
    if real_n:
        rh, rg, rl = _tail(real, real_n)
        real_ok = _passes(rh, rg, rl, real_n, floor, full)
        out.update(realHave=rh, realGp=rg, realGl=rl, realPf=round(_pf(rg, rl, rh), 4), realOk=real_ok, ok=main_ok and real_ok)
    return out


class Accumulator:
    """Collects the member Sets' Main-Set orders per relation cell, pre-adding the orders of one entry that close at the same instant
    (the stops of one TP step's Sets mostly share their target bar), so a lane walks far fewer events than rows."""

    def __init__(self) -> None:
        self.cells: Dict[Tuple[str, str, str, str, str], Dict[Tuple[Any, float, float], List[float]]] = {}
        self.members: Dict[Tuple[str, str, str, str, str], int] = {}

    def add(self, cell: Tuple[str, str, str, str, str], rows: Iterable[Tuple[float, float, Any, float]]) -> None:
        """rows: (exit t, entry t, symbol, net) of ONE member Set's Main-Set orders."""
        self.members[cell] = self.members.get(cell, 0) + 1
        acc = self.cells.setdefault(cell, {})
        for exit_t, entry_t, symbol, net in rows:
            k = (symbol, entry_t, exit_t)
            a = acc.get(k)
            if a is None:
                acc[k] = [net, 1]
            else:
                a[0] += net
                a[1] += 1

    def lanes(self, cfg: RelationConfig) -> Dict[str, Dict[str, Any]]:
        """lane id -> lane_state (+ relation, role, key parts, member Sets) for every relation switched on."""
        by: Dict[str, Dict[str, Any]] = {}
        for cell, acc in self.cells.items():
            ids = keys_of(cell)
            for relation in cfg.on:
                lane = by.setdefault(ids[relation], {"relation": relation, "role": ROLE[relation], "pack": "*" if relation == "overall" else cell[0], "side": cell[1],
                                                     "value": {"overall": "", "indication": "", "type": cell[2], "range": cell[3], "step": cell[4]}[relation],
                                                     "sets": 0, "cells": []})
                lane["sets"] += self.members.get(cell, 0)
                lane["cells"].append(acc)
        out: Dict[str, Dict[str, Any]] = {}
        for lane_id, lane in by.items():
            cells = lane.pop("cells")
            rows = ((exit_t, entry_t, (symbol, entry_t), v[0], v[1]) for acc in cells for (symbol, entry_t, exit_t), v in acc.items())
            lane.update(lane_state(rows, cfg))
            out[lane_id] = lane
        return out


def multiple(cell: Sequence[str], lane: str, lanes: Dict[str, Dict[str, Any]], cfg: RelationConfig) -> Tuple[float, List[str]]:
    """(volume multiple, the relations that raised it) of a member Set's entry: 1 + the ratios of its passing relation lanes, at most
    maxMult. Only the positive lane is raised: the Real Negative lane trades against the recent Main results these lanes are built
    from. A relation without a lane (no post-Base order yet) or with ratio 0 adds nothing."""
    if not cfg.enabled or lane != "pos" or not lanes:
        return 1.0, []
    ids = keys_of(cell)
    add, why = 0.0, []
    for relation in cfg.on:
        ratio = cfg.ratio(relation)
        state = lanes.get(ids[relation])
        if ratio > 0 and state is not None and state.get("ok"):
            add += ratio
            why.append(relation)
    return min(cfg.max_mult, 1.0 + add), why


def summary(lanes: Dict[str, Dict[str, Any]], cfg: RelationConfig) -> Dict[str, Any]:
    """Per relation: lanes built, lanes passing Main / Real / both, and the gains over the losses of ALL the observations their windows
    read (pooled, never an average of lane PFs), with the observation count behind it."""
    out: Dict[str, Any] = {}
    for relation in RELATIONS:
        mine = [v for v in lanes.values() if v["relation"] == relation]
        mg, ml, mh = sum(v["mainGp"] for v in mine), sum(v["mainGl"] for v in mine), sum(v["mainHave"] for v in mine)
        ok = [v for v in mine if v["ok"]]
        rg, rl, rh = sum(v["realGp"] for v in ok), sum(v["realGl"] for v in ok), sum(v["realHave"] for v in ok)
        out[relation] = {"role": ROLE[relation], "on": relation in cfg.on, "ratio": cfg.ratio(relation), "lanes": len(mine),
                         "mainOk": sum(1 for v in mine if v["mainOk"]), "ok": len(ok),
                         "entries": sum(v["entries"] for v in mine), "orders": sum(v["orders"] for v in mine),
                         "mainPf": round(_pf(mg, ml, mh), 4), "mainObs": mh,
                         "passPf": round(_pf(rg, rl, rh), 4), "passObs": rh}
    return out


def self_test() -> List[Tuple[str, bool, str]]:
    out: List[Tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        out.append((name, bool(ok), detail))

    cfg = config_of({"enabled": True, "mainN": 3, "realN": 2, "minPf": 1.1})
    add("default off", not config_of(None).enabled and not config_of({"mainN": 5}).enabled)
    add("bounds", config_of({"enabled": True, "minPf": 0.2, "maxMult": 50, "mainN": 0, "relations": {"type": {"ratio": 9}}}).as_dict()
        == {**config_of({"enabled": True}).as_dict(), "minPf": MIN_PF_FLOOR, "maxMult": MULT_MAX, "mainN": 1,
            "relations": {**config_of({"enabled": True}).as_dict()["relations"], "type": {"on": True, "ratio": RATIO_MAX, "role": "major"}}})
    # one entry taken by three members counts once, at the mean of what has closed
    rows = [(10.0, 1.0, "e1", 0.03, 1), (11.0, 1.0, "e1", -0.01, 1), (12.0, 1.0, "e1", 0.01, 1)]
    s = lane_state(rows, cfg)
    add("one observation per entry", s["entries"] == 1 and s["orders"] == 3 and abs(s["mainGp"] - 0.01) < 1e-12 and s["mainGl"] == 0.0, str(s))
    # the Real Set: e2 opens while Main (e1 +) passes, e3 opens after e2 lost and Main fails; a close stamped at the entry instant is not read
    rows = [(10.0, 1.0, "e1", 0.02, 1), (20.0, 11.0, "e2", -0.05, 1), (30.0, 21.0, "e3", 0.04, 1), (40.0, 30.0, "e4", 0.04, 1)]
    s = lane_state(rows, cfg)
    add("real set is causal", s["realEntries"] == 1 and s["entries"] == 4 and abs(s["realGl"] - 0.05) < 1e-12 and not s["realOk"] and s["mainOk"] and not s["ok"], str(s))
    later = lane_state(rows + [(50.0, 45.0, "e5", 0.01, 1)], cfg)
    add("later close keeps earlier membership", later["realEntries"] == 2, str(later))
    acc = Accumulator()
    c1, c2 = cell_of("p~cfg1", 1, "base", 4, 4), cell_of("p~cfg2", 1, "exit", 4, 4)
    acc.add(c1, [(10.0, 1.0, "X", 0.02), (20.0, 11.0, "X", 0.03)])
    acc.add(c1, [(10.0, 1.0, "X", 0.04)])
    acc.add(c2, [(12.0, 1.0, "X", -0.03)])
    lanes = acc.lanes(cfg)
    ov = lanes["rel:p:L:indication"]
    add("cells merge by relation", ov["entries"] == 2 and ov["orders"] == 4 and ov["sets"] == 3 and abs(ov["mainGp"] - (0.01 + 0.03)) < 1e-12
        and lanes["rel:p:L:type:exit"]["entries"] == 1 and not lanes["rel:p:L:type:exit"]["ok"] and len(lanes) == 6
        and lanes["rel:*:L:overall"]["entries"] == 2 and lanes["rel:*:L:overall"]["pack"] == "*", str(sorted(lanes)))
    m, why = multiple(c1, "pos", lanes, cfg)
    add("multiple adds passing ratios", abs(m - 1.75) < 1e-12 and why == ["overall", "indication", "type"] and multiple(c2, "pos", lanes, cfg) == (1.5, ["overall", "indication"])
        and multiple(c1, "neg", lanes, cfg) == (1.0, []) and multiple(c1, "pos", lanes, config_of({"mainN": 3})) == (1.0, []), f"{m} {why}")
    capped = config_of({"enabled": True, "mainN": 3, "realN": 2, "maxMult": 1.2})
    add("multiple capped", multiple(c1, "pos", lanes, capped)[0] == 1.2)
    sm = summary(lanes, cfg)
    add("summary pools observations", sm["type"]["lanes"] == 2 and sm["type"]["ok"] == 1 and sm["indication"]["entries"] == 2 and sm["overall"]["lanes"] == 1, str(sm["type"]))
    return out


if __name__ == "__main__":
    bad = [r for r in self_test() if not r[1]]
    for name, ok, detail in self_test():
        print("PASS" if ok else "FAIL", name, detail if not ok else "")
    raise SystemExit(1 if bad else 0)
