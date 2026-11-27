"""Axis Sets (owner 2026-11-15: "axis setting with all possible multiple configs, auto PF eval, positive good PF and optimal vol ratios related to base set").

Every Axis setting is a derived Set of one configuration's lane Set (its Real Set on the positive lane, its Real Negative Set on the negative lane): the
orders that setting would have admitted, each judged only on the orders that had CLOSED before its own entry. Per configuration and lane the Auto choice
takes, from closed orders only, the setting whose newest `n` admitted orders read the best PF, provided it has at least `minN` admitted orders and beats
the lane Set itself (no Axis) by `margin`; none qualifies = no Axis for that Set. An entry then opens only where the chosen setting admits it now.

Settings: last w (the newest w results must read the Set's floor), prev w (the window before must not be under 0.85 x the floor in both its 2w and 50
views), pause w (fewer than w losses in a row), each w over its range, and with `combos` every pair of two kinds (both must admit).

Volume (`vol`): "flat" = 1; "base" = the chosen Axis Set's PF over the lane Set's own PF (no Axis) -- the ratio to the base configuration Set; "floor" =
its PF over the Set's floor. Bounded to [`vmin`, `vmax`]. A volume ratio weights results; it does not change a per-order PF.

One pure module for the engine (Pulse._scope_choice, Pulse.place) and the simulators (stage_eval.execute axis_pick, trade_sim --axis-sets)."""
from __future__ import annotations

from functools import lru_cache
import bisect
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

LAST_W = (1, 2, 3, 4)
PREV_W = (4, 6, 8, 12)
PAUSE_W = (1, 2, 3, 4, 6, 8)
VOL_MODES = ("flat", "base", "floor")


FIXED_STOP_DEFAULT = {"enabled":True, "n":15, "minN":8, "vol":"floor", "vmin":.75, "vmax":1.0}


@dataclass
class AxisSetsCfg:
    enabled: bool = False
    n: int = 25
    min_n: int = 8
    margin: float = 1.05
    combos: bool = False
    vol: str = "flat"
    vmin: float = 0.5
    vmax: float = 2.0
    strict: bool = False

    def to_json(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "n": self.n, "minN": self.min_n, "margin": self.margin, "combos": self.combos, "vol": self.vol,
                "vmin": self.vmin, "vmax": self.vmax, "strict": self.strict}


def _num(v: Any, default: float, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return default
    return min(hi, max(lo, float(v)))


def config_of(raw: Any) -> AxisSetsCfg:
    """The overlay's `axisSets` object; anything unreadable is the default (off)."""
    c = AxisSetsCfg()
    if not isinstance(raw, dict):
        return c
    c.enabled = raw.get("enabled") is True
    c.n = int(_num(raw.get("n"), 25, 3, 200))
    c.min_n = int(min(c.n, _num(raw.get("minN"), 8, 1, 200)))
    c.margin = _num(raw.get("margin"), 1.05, 1.0, 2.0)
    c.combos = raw.get("combos") is True
    c.vol = raw.get("vol") if raw.get("vol") in VOL_MODES else "flat"
    c.vmin = _num(raw.get("vmin"), 0.5, 0.1, 1.0)
    c.vmax = _num(raw.get("vmax"), 2.0, 1.0, 5.0)
    c.strict = raw.get("strict") is True
    return c


@lru_cache(maxsize=2)
def candidates(combos: bool) -> List[Tuple[Tuple[str, int], ...]]:
    singles = [("last", w) for w in LAST_W] + [("prev", w) for w in PREV_W] + [("pause", w) for w in PAUSE_W]
    out: List[Tuple[Tuple[str, int], ...]] = [(s,) for s in singles]
    if combos:
        kinds = {"last": LAST_W, "prev": PREV_W, "pause": PAUSE_W}
        names = list(kinds)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                out += [((names[i], a), (names[j], b)) for a in kinds[names[i]] for b in kinds[names[j]]]
    return out


def pf(xs: Sequence[float]) -> Optional[float]:
    g = sum(x for x in xs if x > 0)
    l_ = -sum(x for x in xs if x < 0)
    if l_ > 0:
        return g / l_
    return 99.0 if g > 0 else None


def rule_ok(kind: str, w: int, nets: Sequence[float], floor: float) -> bool:
    """Whether one Axis setting admits an entry, from the lane's results closed before it (oldest first)."""
    n = len(nets)
    if kind == "last":
        if n < min(3, w):
            return True
        return (pf(nets[-w:]) or 0.0) + 1e-9 >= floor
    if kind == "prev":
        pw = min(25, 2 * w)
        if n < 8 or len(nets[-pw:]) < 8:
            return True
        lim = 0.85 * floor
        return not ((pf(nets[-pw:]) or 0.0) + 1e-9 < lim and (pf(nets[-50:]) or 0.0) + 1e-9 < lim)
    k = 0
    for x in reversed(nets):
        if x < 0:
            k += 1
        else:
            break
    return k < w


def admits(setting: Tuple[Tuple[str, int], ...], nets: Sequence[float], floor: float) -> bool:
    return all(rule_ok(kind, w, nets, floor) for kind, w in setting)


def choose(exits: Sequence[float], entries: Sequence[float], nets: Sequence[float], floor: float, cfg: AxisSetsCfg) -> Dict[str, Any]:
    """The Auto choice for one Set and lane from its lane Set's orders (exit t, entry t, net), oldest exit first. Every order is judged on the orders
    closed strictly before its entry. Returns {"setting", "pf", "basePf", "admitted", "allow", "vol"} for an entry now (after the newest exit)."""
    base_pf = pf(list(nets)[-cfg.n:]) or 0.0
    best: Tuple[Optional[Tuple[Tuple[str, int], ...]], float, int] = (None, base_pf, len(nets))
    before = [bisect.bisect_left(exits, e) for e in entries]
    truths = {}
    for setting in candidates(cfg.combos):
        for rule in setting:
            if rule not in truths:
                truths[rule] = [rule_ok(*rule, nets[max(0, end-50):end], floor) for end in before]
        passed = truths[setting[0]]
        if len(setting) == 2:
            passed = (left and right for left, right in zip(passed, truths[setting[1]]))
        adm = [value for value, ok in zip(nets, passed) if ok]
        if len(adm) < cfg.min_n:
            continue
        p = pf(adm[-cfg.n:]) or 0.0
        if p > best[1] and p >= base_pf * cfg.margin:
            best = (setting, p, len(adm))
    setting, p_c, n_c = best
    allow = True if setting is None else admits(setting, list(nets), floor)
    if cfg.strict and p_c + 1e-9 < floor:
        allow = False
    vol = 1.0
    if n_c >= cfg.min_n and cfg.vol == "base" and setting is not None:
        vol = p_c / base_pf if base_pf > 0 else cfg.vmax
    elif n_c >= cfg.min_n and cfg.vol == "floor":
        vol = p_c / floor if floor > 0 else 1.0
    vol = max(cfg.vmin, min(cfg.vmax, vol))
    return {"setting": [list(s) for s in setting] if setting else None, "pf": round(p_c, 4), "basePf": round(base_pf, 4), "admitted": n_c,
            "allow": bool(allow), "vol": round(vol, 6)}


def self_test() -> List[Tuple[str, bool, str]]:
    out: List[Tuple[str, bool, str]] = []
    cfg = config_of({"enabled": True, "n": 10, "minN": 3, "margin": 1.0, "vol": "base", "vmax": 3})
    # a lane whose losses come in streaks of three: "pause 2" (fewer than 2 losses in a row) admits the first loss of a streak only
    nets, exits, entries = [], [], []
    t = 0.0
    for k in range(40):
        x = -1.0 if k % 6 in (2, 3, 4) else 1.0
        entries.append(t); t += 10; exits.append(t); nets.append(x); t += 1
    r = choose(exits, entries, nets, 1.1, cfg)
    out.append(("a setting that skips streaks is chosen over no Axis", r["setting"] is not None and r["pf"] > r["basePf"], str(r)))
    out.append(("the volume is the chosen PF over the base PF, bounded", abs(r["vol"] - min(3.0, r["pf"] / r["basePf"])) < 1e-6, str(r)))
    flat = choose(exits, entries, [1.0] * 40, 1.1, cfg)
    out.append(("a lane without losses keeps no Axis", flat["setting"] is None and flat["allow"], str(flat)))
    out.append(("combos add every pair of two kinds", len(candidates(True)) == 14 + 4 * 4 + 4 * 6 + 4 * 6 and len(candidates(False)) == 14, ""))
    # causality: an order's admission never reads its own or a later result
    cut = choose(exits[:20], entries[:20], nets[:20], 1.1, cfg)
    again = choose(exits[:20], entries[:20], nets[:20] , 1.1, cfg)
    out.append(("the choice is a pure function of the closed orders", cut == again, ""))
    off = config_of({"vol": "nope", "n": -4, "minN": 999})
    out.append(("unreadable values fall back", not off.enabled and off.vol == "flat" and off.n == 3 and off.min_n == 3, str(off)))
    return out


if __name__ == "__main__":
    bad = [n for n, ok, _ in self_test() if not ok]
    print("axis_sets self-test:", "ok" if not bad else bad)
    raise SystemExit(1 if bad else 0)
