"""Block v2 rules, one pure module for the engine (set_block.SetBlockBook) and the simulator (scripts/stack_eval.py), so the two cannot drift apart.

Owner 2026-11-03 ("make Block fully work ... dont use windows ... each block count independent ... Shared, Additive"):

- No window. A relation's evidence for count n is the CUMULATIVE record of every observation since the book started: {obs, gains, losses} of the
  observations' net results (one PositionCost each). PF = gains / losses (99 with gains and no loss, 1.0 neutral when every result is exactly zero, 0 empty).
- Each block COUNT n (1..N) is independent: its own trigger step (in PositionCosts in the parent's favour, measured from the previous count's level: steps
  2 + 2 + 2 = three counts at 2, 4 and 6 PositionCosts), its own volume ratio (0.1 .. 1.0 of the parent's ORIGINAL volume V0), its own evidence record and its
  own on / off. One count's record never moves another's.
- Stack type: "additive" - count n adds ratio_n x V0 when it fires (the stack is V0 x (1 + sum of the fired ratios)); "shared" - the parent's whole add budget
  is one number R x V0 split equally over the ENABLED counts (each adds R x V0 / k), so a longer table never raises the total.
- A count adds only when every enabled RELATION agrees: a relation is a key the evidence is kept under, independent of the others -
  "symbol" (the symbol and side), "indication" (the pack, lane and side), "set" (the strategy configuration: Set, lane, side; the ordinary Block on Main /
  Real Set parents) and "step" (the pack, the TP step, lane and side; the overall Block at stage Real). Each relation holds its own ledgers and lifecycle.
- Lifecycle per relation (opt-in carry): a parent that closes net negative keeps the highest count it fired as the relation's `level` (the next parent is sized
  at V0 x (1 + the ratios up to that level)); when the relation's net is back at or above zero the level resets and the relation pauses `pause_steps` parents
  before it starts again. `skip_first` counts are never added (they still collect evidence). Without carry nothing is inherited between parents.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from block_engine import calculate_block_minimum_profit_factor

RATIO_MIN, RATIO_MAX = 0.1, 1.0
STEP_MIN, STEP_MAX = 0.5, 20.0
COUNTS_MAX = 12
RELATIONS = ("symbol", "indication", "set", "step")
STACK_TYPES = ("additive", "shared")
PF_RATIO_MIN, PF_RATIO_MAX = 0.2, 5.0


@dataclass
class BlockCfg:
    counts: List[Dict[str, Any]] = field(default_factory=lambda: [{"step": 3.0, "ratio": 0.5, "on": True} for _ in range(3)])
    stack_type: str = "additive"
    shared_total: float = 1.0           # R of the shared type: the whole add budget in V0
    min_obs: int = 1                    # observations a count's record needs before it can add (a count, never a window; owner 2026-11-04: no minimum-orders gate)
    pf_ratio: float = 0.8
    relations: Tuple[str, ...] = ("symbol",)
    carry: bool = False
    pause_steps: int = 1
    skip_first: int = 1

    def levels(self) -> List[float]:
        """The cumulative trigger of every count in PositionCosts (the sum of the steps up to it)."""
        out, acc = [], 0.0
        for c in self.counts:
            acc += float(c["step"])
            out.append(acc)
        return out

    def target_ratio(self, n: int) -> float:
        """The volume count n adds, in V0 (n is 1-based): its own ratio (additive) or an equal share of the shared budget."""
        if not 1 <= n <= len(self.counts):
            return 0.0
        c = self.counts[n - 1]
        if not c.get("on", True):
            return 0.0
        if self.stack_type == "shared":
            k = sum(1 for x in self.counts if x.get("on", True))
            return self.shared_total / k if k else 0.0
        return float(c["ratio"])

    def active(self, n: int) -> bool:
        """Count n may add: it is on and not among the first `skip_first` counts."""
        return 1 <= n <= len(self.counts) and bool(self.counts[n - 1].get("on", True)) and n > self.skip_first


def clean_cfg(raw: Any = None, **legacy: Any) -> BlockCfg:
    """A BlockCfg from the overlay's blockCounts (list of {step, ratio, on}) and keys, every value bounded; legacy blockMaxStack / blockVolumeRatio build
    the table when blockCounts is absent. An unreadable entry is dropped, never read as zero."""
    ov = raw if isinstance(raw, dict) else {}
    counts: List[Dict[str, Any]] = []
    for item in (ov.get("blockCounts") or []):
        try:
            step = float(item.get("step", 2.0))
            ratio = float(item.get("ratio", 0.5))
        except (AttributeError, TypeError, ValueError):
            continue
        if not (math.isfinite(step) and math.isfinite(ratio)):
            continue
        counts.append({"step": max(STEP_MIN, min(STEP_MAX, step)), "ratio": round(max(RATIO_MIN, min(RATIO_MAX, ratio)), 4), "on": bool(item.get("on", True))})
    counts = counts[:COUNTS_MAX]
    if not counts:
        n = int(legacy.get("max_stack") or 3)
        r = float(legacy.get("volume_ratio") or 0.5)
        counts = [{"step": 1.0, "ratio": round(max(RATIO_MIN, min(RATIO_MAX, r)), 4), "on": True} for _ in range(max(1, min(COUNTS_MAX, n)))]
    rel = tuple(x for x in dict.fromkeys(str(r).lower() for r in (ov.get("blockRelations") or ("symbol",))) if x in RELATIONS) or ("symbol",)
    st = str(ov.get("blockStackType") or "additive").lower()

    def num(key: str, default: float, lo: float, hi: float) -> float:
        try:
            v = float(ov.get(key, default))
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v)) if math.isfinite(v) else default
    return BlockCfg(counts=counts, stack_type=st if st in STACK_TYPES else "additive", shared_total=round(num("blockSharedTotal", 1.0, 0.1, 3.0), 4),
                    min_obs=int(num("blockMinObservations", 1, 1, 200)), pf_ratio=num("blockProfitFactorRatio", 0.8, PF_RATIO_MIN, PF_RATIO_MAX),
                    relations=rel, carry=bool(ov.get("blockCarry", False)), pause_steps=int(num("blockPauseSteps", 1, 0, 20)),
                    skip_first=int(num("blockSkipFirst", 1, 0, COUNTS_MAX)))


# ---- evidence ---------------------------------------------------------------------------------------------------------------------------------------

def new_record() -> List[float]:
    """[observations, gains, losses] - the cumulative evidence of one (relation, count)."""
    return [0, 0.0, 0.0]


def add_obs(rec: List[float], net: float) -> None:
    if not math.isfinite(net):
        return
    rec[0] += 1
    if net > 0:
        rec[1] += net
    elif net < 0:
        rec[2] += -net


def record_pf(rec: Sequence[float]) -> float:
    """gains / losses of the whole record: 99 without a loss but a gain, 1.0 (neutral) when every result was exactly zero, 0 when empty."""
    n, g, l_ = int(rec[0]), float(rec[1]), float(rec[2])
    if l_ > 0:
        return g / l_
    return 99.0 if g > 0 else (1.0 if n > 0 else 0.0)


def threshold(cfg: BlockCfg, floor: float, fired_ratio: float) -> float:
    """The PF the stack this count would create must clear: the parent Set's floor, raised by the volume the stack carries (block_engine's formula)."""
    return max(float(floor), calculate_block_minimum_profit_factor(float(floor), max(PF_RATIO_MIN, min(PF_RATIO_MAX, cfg.pf_ratio)), fired_ratio))


def count_passes(cfg: BlockCfg, n: int, records: Iterable[Optional[Sequence[float]]], executed: Iterable[Optional[Sequence[float]]], floor: float,
                 fired_ratio: float) -> bool:
    """Count n may add when EVERY relation's paper record has at least min_obs observations at a PF >= the threshold, and no relation's executed record
    (when it has min_obs adds) is under it."""
    thr = threshold(cfg, floor, fired_ratio + cfg.target_ratio(n))
    seen = False
    for rec in records:
        seen = True
        if rec is None or int(rec[0]) < cfg.min_obs or record_pf(rec) + 1e-9 < thr:
            return False
    for rec in executed:
        if rec is not None and int(rec[0]) >= cfg.min_obs and record_pf(rec) + 1e-9 < thr:
            return False
    return seen


# ---- relations --------------------------------------------------------------------------------------------------------------------------------------

def relation_keys(cfg: BlockCfg, set_id: str, lane: str, side: str, symbol: str, pack: str, step: int) -> List[str]:
    """The evidence key of every enabled relation for one parent."""
    out = []
    for r in cfg.relations:
        if r == "symbol":
            out.append(f"symbol|{symbol}|{side}")
        elif r == "indication":
            out.append(f"indication|{pack}|{lane}|{side}")
        elif r == "set":
            out.append(f"set|{set_id}|{lane}|{side}")
        elif r == "step":
            out.append(f"step|{pack}|{int(step)}|{lane}|{side}")
    return out


# ---- lifecycle --------------------------------------------------------------------------------------------------------------------------------------

def new_life() -> Dict[str, Any]:
    return {"level": 0, "net": 0.0, "pause": 0}


def carried_scale(cfg: BlockCfg, life: Dict[str, Any]) -> float:
    """The multiple of V0 the next parent of a relation opens at: 1 + the ratios of the counts up to its carried level (1.0 without carry or level)."""
    if not cfg.carry:
        return 1.0
    return 1.0 + sum(cfg.target_ratio(n) for n in range(1, int(life.get("level", 0)) + 1))


def life_after_close(cfg: BlockCfg, life: Dict[str, Any], parent_net: float, highest_fired: int) -> Dict[str, Any]:
    """Update a relation's lifecycle at a parent's close. parent_net is the whole parent's net result in V0 (the base position plus its adds' ratios times their
    nets). A net loss raises the carried level to the highest count the parent fired; the relation's net accumulates; at or above zero the level resets
    and `pause_steps` parents are paused. A paused relation counts a parent down."""
    out = dict(life)
    if not cfg.carry:
        # without carry nothing is inherited between parents and nothing pauses; the net is only kept for the statistics
        out.update(level=0, pause=0, net=float(out.get("net", 0.0)) + parent_net)
        return out
    if int(out.get("pause", 0)) > 0:
        out["pause"] = int(out["pause"]) - 1
    out["net"] = float(out.get("net", 0.0)) + parent_net
    if parent_net < 0 and highest_fired > int(out.get("level", 0)):
        out["level"] = int(highest_fired)
    if out["net"] >= 0 and int(out.get("level", 0)) > 0:
        out["level"] = 0
        out["net"] = 0.0
        out["pause"] = int(cfg.pause_steps)
    return out
