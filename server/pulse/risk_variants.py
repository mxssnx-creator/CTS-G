#!/usr/bin/env python3
"""Independent SL:TP ratio sets and trailing recals.

SL is bound to TP on the discrete CTS grid 0.3–1.5 step 0.3.
Trailing arm/give is a separate book with its own optimal range and recals.
Neither book writes the other.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from position_cost import last_n_cost_pf, snap_ratio, SL_TP_RATIOS, SL_TP_STEP, SL_TP_MIN, SL_TP_MAX

TRAIL_VARIANTS = ("0.3:0.1", "0.6:0.2", "0.9:0.3", "1.2:0.4", "1.5:0.5")
TRAIL_ARM_MIN = 0.3
TRAIL_ARM_MAX = 1.5
TRAIL_GIVE_MIN = 0.1
TRAIL_GIVE_MAX = 0.5
TRAIL_GIVE_FACTOR = 1.0 / 3.0


def parse_trail(raw: Any) -> Tuple[float, float]:
    text = str(raw or "0.3:0.1")
    if ":" not in text:
        try:
            arm = float(text)
        except Exception:
            arm = 0.3
        return snap_ratio(arm), round(max(TRAIL_GIVE_MIN, min(TRAIL_GIVE_MAX, snap_ratio(arm) * TRAIL_GIVE_FACTOR)), 1)
    a, g = text.split(":", 1)
    try:
        arm = float(a)
    except Exception:
        arm = 0.3
    try:
        give = float(g)
    except Exception:
        give = arm * TRAIL_GIVE_FACTOR
    arm = snap_ratio(arm, TRAIL_ARM_MIN, TRAIL_ARM_MAX, SL_TP_STEP)
    give = round(max(TRAIL_GIVE_MIN, min(TRAIL_GIVE_MAX, give)), 2)
    return arm, give


def trail_key(arm: float, give: float) -> str:
    return f"{arm:.1f}:{give:.1f}" if abs(give * 10 - round(give * 10)) < 1e-9 else f"{arm:.1f}:{give:.2f}"


def give_from_arm(arm: float, factor: float, gmin: float, gmax: float) -> float:
    raw = float(arm) * float(factor)
    return round(max(gmin, min(gmax, raw)), 2)


def trail_candidates(
    arm_min: float,
    arm_max: float,
    give_min: float,
    give_max: float,
    factor: float,
    recalc_give: bool,
    variants: Sequence[str],
) -> List[Tuple[str, float, float]]:
    out: List[Tuple[str, float, float]] = []
    seen = set()
    lo = snap_ratio(arm_min, TRAIL_ARM_MIN, TRAIL_ARM_MAX, SL_TP_STEP)
    hi = snap_ratio(arm_max, TRAIL_ARM_MIN, TRAIL_ARM_MAX, SL_TP_STEP)
    if lo > hi:
        lo, hi = hi, lo
    source = list(variants) or list(TRAIL_VARIANTS)
    for raw in source:
        arm, give = parse_trail(raw)
        if arm + 1e-9 < lo or arm - 1e-9 > hi:
            continue
        if recalc_give:
            give = give_from_arm(arm, factor, give_min, give_max)
        if give + 1e-9 < give_min or give - 1e-9 > give_max:
            continue
        key = trail_key(arm, give)
        if key in seen:
            continue
        seen.add(key)
        out.append((key, arm, give))
    if not out:
        arm = snap_ratio((lo + hi) / 2)
        give = give_from_arm(arm, factor, give_min, give_max)
        out.append((trail_key(arm, give), arm, give))
    return out


@dataclass
class LaneScore:
    key: str
    n: int = 0
    wins: int = 0
    pf: float = 0.0
    ratio: float = 1.0
    expectancy: float = 0.0
    avg_hold: float = 0.0
    selected: bool = False
    in_range: bool = True


@dataclass
class VariantBook:
    """Two independent recals: SL:TP ratios and trailing arm/give."""

    sl_ratio: float = 0.6
    sl_auto: bool = True
    sl_recalc_n: int = 6
    sl_recalc_every: int = 8
    sl_ratios: List[float] = field(default_factory=lambda: list(SL_TP_RATIOS))
    trail_key: str = "0.3:0.1"
    trail_arm: float = 0.3
    trail_give: float = 0.1
    trail_auto: bool = True
    trail_recalc_n: int = 6
    trail_recalc_every: int = 8
    trail_arm_min: float = TRAIL_ARM_MIN
    trail_arm_max: float = TRAIL_ARM_MAX
    trail_give_min: float = TRAIL_GIVE_MIN
    trail_give_max: float = TRAIL_GIVE_MAX
    trail_give_factor: float = TRAIL_GIVE_FACTOR
    trail_recalc_give: bool = True
    trail_variants: List[str] = field(default_factory=lambda: list(TRAIL_VARIANTS))
    sl_closes: int = 0
    trail_closes: int = 0
    sl_last_recalc: float = 0.0
    trail_last_recalc: float = 0.0
    sl_rows: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    trail_rows: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    sl_scores: List[LaneScore] = field(default_factory=list)
    trail_scores: List[LaneScore] = field(default_factory=list)
    last_sl_pick: str = "default"
    last_trail_pick: str = "default"

    def load(self, ov: Dict[str, Any], cts: Optional[Dict[str, Any]] = None) -> None:
        cts = cts or {}
        coord = cts.get("coordination_settings") or cts.get("coordinationSettings") or {}
        raw_ratios = ov.get("slToTpRatios") or list(SL_TP_RATIOS)
        ratios: List[float] = []
        for x in raw_ratios:
            try:
                ratios.append(snap_ratio(float(x)))
            except Exception:
                continue
        self.sl_ratios = sorted(set(ratios)) or list(SL_TP_RATIOS)
        self.sl_ratio = snap_ratio(ov.get("slToTpRatio", self.sl_ratio))
        if self.sl_ratio not in self.sl_ratios:
            self.sl_ratio = min(self.sl_ratios, key=lambda r: abs(r - self.sl_ratio))
        self.sl_auto = bool(ov.get("slToTpAuto", True))
        self.sl_recalc_n = max(3, int(ov.get("slToTpRecalcN") or 6))
        self.sl_recalc_every = max(3, int(ov.get("slToTpRecalcEvery") or 8))

        raw_trails = ov.get("trailVariants") or coord.get("trailingVariants") or cts.get("strategyBaseTrailingVariants") or list(TRAIL_VARIANTS)
        if isinstance(raw_trails, str):
            raw_trails = [p.strip() for p in raw_trails.split(",") if p.strip()]
        self.trail_variants = [str(x) for x in raw_trails] or list(TRAIL_VARIANTS)
        self.trail_arm_min = snap_ratio(ov.get("trailArmMin", TRAIL_ARM_MIN), TRAIL_ARM_MIN, TRAIL_ARM_MAX, SL_TP_STEP)
        self.trail_arm_max = snap_ratio(ov.get("trailArmMax", TRAIL_ARM_MAX), TRAIL_ARM_MIN, TRAIL_ARM_MAX, SL_TP_STEP)
        self.trail_give_min = float(ov.get("trailGiveMin") or TRAIL_GIVE_MIN)
        self.trail_give_max = float(ov.get("trailGiveMax") or TRAIL_GIVE_MAX)
        self.trail_give_factor = float(ov.get("trailGiveFactor") or TRAIL_GIVE_FACTOR)
        self.trail_recalc_give = bool(ov.get("trailRecalcGive", True))
        self.trail_auto = bool(ov.get("trailAuto", True))
        self.trail_recalc_n = max(3, int(ov.get("trailRecalcN") or 6))
        self.trail_recalc_every = max(3, int(ov.get("trailRecalcEvery") or 8))
        arm = ov.get("trailArmPct")
        give = ov.get("trailGivePct")
        if arm is None:
            arm, give2 = parse_trail(self.trail_variants[0])
            give = give if give is not None else give2
        self.trail_arm = snap_ratio(arm, TRAIL_ARM_MIN, TRAIL_ARM_MAX, SL_TP_STEP)
        if give is None:
            self.trail_give = give_from_arm(self.trail_arm, self.trail_give_factor, self.trail_give_min, self.trail_give_max)
        else:
            self.trail_give = round(max(self.trail_give_min, min(self.trail_give_max, float(give))), 2)
        if self.trail_recalc_give:
            self.trail_give = give_from_arm(self.trail_arm, self.trail_give_factor, self.trail_give_min, self.trail_give_max)
        self.trail_key = trail_key(self.trail_arm, self.trail_give)
        self._refresh_scores()

    def current_sl(self) -> float:
        return self.sl_ratio

    def current_trail(self) -> Tuple[str, float, float]:
        return self.trail_key, self.trail_arm, self.trail_give

    def trail_frac(self) -> Tuple[float, float]:
        return self.trail_arm / 100.0, self.trail_give / 100.0

    def on_close(self, rec: Any) -> None:
        if isinstance(rec, dict):
            pnl = float(rec.get("pnl") or 0)
            pnl_pct = float(rec.get("pnl_pct") or 0)
            hold = float(rec.get("hold_s") or 0)
            sl_r = float(rec.get("sl_ratio") or rec.get("slRatio") or self.sl_ratio)
            t_key = str(rec.get("trail_key") or rec.get("trailKey") or self.trail_key)
        else:
            pnl = float(getattr(rec, "pnl", 0) or 0)
            pnl_pct = float(getattr(rec, "pnl_pct", 0) or 0)
            hold = float(getattr(rec, "hold_s", 0) or 0)
            sl_r = float(getattr(rec, "sl_ratio", 0) or self.sl_ratio)
            t_key = str(getattr(rec, "trail_key", "") or self.trail_key)
        sl_key = f"{snap_ratio(sl_r):.1f}"
        row = {"pnl": pnl, "pnl_pct": pnl_pct, "hold_s": hold}
        self.sl_rows.setdefault(sl_key, []).append(row)
        self.sl_rows[sl_key] = self.sl_rows[sl_key][-40:]
        self.trail_rows.setdefault(t_key, []).append(row)
        self.trail_rows[t_key] = self.trail_rows[t_key][-40:]
        self.sl_closes += 1
        self.trail_closes += 1
        self.maybe_recalc()

    def maybe_recalc(self, force: bool = False) -> None:
        now = time.time()
        if self.sl_auto and (force or self.sl_closes >= self.sl_recalc_every or now - self.sl_last_recalc > 180):
            self._recalc_sl()
            self.sl_closes = 0
            self.sl_last_recalc = now
        if self.trail_auto and (force or self.trail_closes >= self.trail_recalc_every or now - self.trail_last_recalc > 180):
            self._recalc_trail()
            self.trail_closes = 0
            self.trail_last_recalc = now
        self._refresh_scores()

    def seed_history(self, closed: Sequence[Any]) -> None:
        self.sl_rows = {}
        self.trail_rows = {}
        for rec in closed:
            self.on_close(rec)
        self.sl_closes = 0
        self.trail_closes = 0
        self.maybe_recalc(force=True)

    def _score_rows(self, rows: Sequence[Dict[str, Any]], cost_pct: float = 0.15) -> LaneScore:
        gp = sum(r["pnl"] for r in rows if r["pnl"] > 0)
        gl = abs(sum(r["pnl"] for r in rows if r["pnl"] < 0))
        pf = (gp / gl) if gl > 0 else (99.0 if gp > 0 else 0.0)
        wins = sum(1 for r in rows if r["pnl"] > 0)
        exp = (sum(r["pnl"] for r in rows) / len(rows)) if rows else 0.0
        hold = (sum(r.get("hold_s", 0) for r in rows) / len(rows)) if rows else 0.0
        cost = last_n_cost_pf(list(rows), max(len(rows), 1), cost_pct) if rows else {"ratio": 1.0}
        return LaneScore(
            key="",
            n=len(rows),
            wins=wins,
            pf=round(pf, 3),
            ratio=float(cost.get("ratio") or 1.0),
            expectancy=round(exp, 5),
            avg_hold=round(hold, 1),
        )

    def _pick(self, scores: List[LaneScore], min_n: int, current: str) -> Tuple[str, str]:
        ready = [s for s in scores if s.in_range and s.n >= min_n]
        if not ready:
            return current, "cold"
        ready.sort(key=lambda s: (s.ratio, s.expectancy, s.n), reverse=True)
        best = ready[0]
        if best.key == current:
            return current, "hold"
        return best.key, "recalc"

    def _recalc_sl(self) -> None:
        scores = []
        for r in self.sl_ratios:
            key = f"{r:.1f}"
            sc = self._score_rows(self.sl_rows.get(key) or [])
            sc.key = key
            sc.in_range = True
            scores.append(sc)
        pick, why = self._pick(scores, self.sl_recalc_n, f"{self.sl_ratio:.1f}")
        self.sl_ratio = snap_ratio(float(pick))
        self.last_sl_pick = why
        self.sl_scores = scores

    def _recalc_trail(self) -> None:
        cands = trail_candidates(
            self.trail_arm_min,
            self.trail_arm_max,
            self.trail_give_min,
            self.trail_give_max,
            self.trail_give_factor,
            self.trail_recalc_give,
            self.trail_variants,
        )
        scores = []
        for key, arm, give in cands:
            sc = self._score_rows(self.trail_rows.get(key) or [])
            sc.key = key
            sc.in_range = True
            scores.append(sc)
        pick, why = self._pick(scores, self.trail_recalc_n, self.trail_key)
        for key, arm, give in cands:
            if key == pick:
                self.trail_key = key
                self.trail_arm = arm
                self.trail_give = give
                break
        self.last_trail_pick = why
        self.trail_scores = scores

    def _refresh_scores(self) -> None:
        sl = []
        for r in self.sl_ratios:
            key = f"{r:.1f}"
            sc = self._score_rows(self.sl_rows.get(key) or [])
            sc.key = key
            sc.selected = abs(r - self.sl_ratio) < 1e-9
            sl.append(sc)
        self.sl_scores = sl
        trail = []
        for key, arm, give in trail_candidates(
            self.trail_arm_min,
            self.trail_arm_max,
            self.trail_give_min,
            self.trail_give_max,
            self.trail_give_factor,
            self.trail_recalc_give,
            self.trail_variants,
        ):
            sc = self._score_rows(self.trail_rows.get(key) or [])
            sc.key = key
            sc.selected = key == self.trail_key
            trail.append(sc)
        self.trail_scores = trail

    def snapshot(self) -> Dict[str, Any]:
        def pack(rows: List[LaneScore]) -> List[Dict[str, Any]]:
            return [
                {
                    "key": s.key,
                    "n": s.n,
                    "wins": s.wins,
                    "pf": s.pf,
                    "ratio": round(s.ratio, 3),
                    "expectancy": s.expectancy,
                    "avgHold": s.avg_hold,
                    "selected": s.selected,
                    "inRange": s.in_range,
                }
                for s in rows
            ]

        tp_grid = []
        # illustrative grid vs a 0.75% TP (overridden by live TP at place)
        sample_tp = 0.75
        for r in self.sl_ratios:
            tp_grid.append(
                {
                    "ratio": r,
                    "rr": round(1.0 / r, 3) if r else 0,
                    "slPct": round(sample_tp * r, 3),
                    "tpPct": sample_tp,
                    "selected": abs(r - self.sl_ratio) < 1e-9,
                }
            )
        return {
            "slRatio": self.sl_ratio,
            "slAuto": self.sl_auto,
            "slRecalcN": self.sl_recalc_n,
            "slRecalcEvery": self.sl_recalc_every,
            "slPick": self.last_sl_pick,
            "slLastRecalc": self.sl_last_recalc,
            "slGrid": tp_grid,
            "slScores": pack(self.sl_scores),
            "trailKey": self.trail_key,
            "trailArmPct": self.trail_arm,
            "trailGivePct": self.trail_give,
            "trailAuto": self.trail_auto,
            "trailRecalcN": self.trail_recalc_n,
            "trailRecalcEvery": self.trail_recalc_every,
            "trailArmMin": self.trail_arm_min,
            "trailArmMax": self.trail_arm_max,
            "trailGiveMin": self.trail_give_min,
            "trailGiveMax": self.trail_give_max,
            "trailGiveFactor": self.trail_give_factor,
            "trailRecalcGive": self.trail_recalc_give,
            "trailPick": self.last_trail_pick,
            "trailLastRecalc": self.trail_last_recalc,
            "trailScores": pack(self.trail_scores),
        }


def self_test() -> List[Tuple[str, bool, str]]:
    out: List[Tuple[str, bool, str]] = []
    ratios = [snap_ratio(x) for x in (0.3, 0.45, 0.64, 0.9, 1.05, 1.4, 1.5, 1.8)]
    expect = [0.3, 0.6, 0.6, 0.9, 1.2, 1.5, 1.5, 1.5]
    out.append(("var-snap-ratio", ratios == expect, f"{ratios}"))
    step = [round(SL_TP_MIN + i * SL_TP_STEP, 1) for i in range(5)]
    out.append(("var-sl-grid", step == list(SL_TP_RATIOS), f"{step}"))
    b = VariantBook()
    b.load({"slToTpRatio": 0.64, "trailArmPct": 0.3, "trailGivePct": 0.1, "trailRecalcGive": True, "trailGiveFactor": 0.333})
    out.append(("var-sl-default", abs(b.sl_ratio - 0.6) < 1e-9, f"sl={b.sl_ratio}"))
    out.append(("var-trail-give", abs(b.trail_give - 0.1) < 0.02, f"arm={b.trail_arm} give={b.trail_give}"))
    # independent recals: SL book does not move trail
    wins = [{"pnl": 1.0, "pnl_pct": 0.004, "hold_s": 20, "sl_ratio": 0.9, "trail_key": "0.3:0.1"}] * 8
    loss = [{"pnl": -0.4, "pnl_pct": -0.003, "hold_s": 18, "sl_ratio": 0.3, "trail_key": "1.5:0.5"}] * 8
    for r in wins + loss:
        b.on_close(r)
    b.maybe_recalc(force=True)
    out.append(("var-sl-recalc", abs(b.sl_ratio - 0.9) < 1e-9, f"sl={b.sl_ratio} why={b.last_sl_pick}"))
    out.append(("var-trail-indep", b.trail_arm <= 0.6, f"trail={b.trail_key} why={b.last_trail_pick}"))
    cands = trail_candidates(0.3, 0.9, 0.1, 0.3, 0.333, True, TRAIL_VARIANTS)
    arms = [a for _, a, _ in cands]
    out.append(("var-trail-range", arms == [0.3, 0.6, 0.9], f"{cands}"))
    # ratio 1.5 allowed (SL > TP)
    out.append(("var-ratio-wide", 1.5 in SL_TP_RATIOS, "1.5"))
    return out


if __name__ == "__main__":
    for name, ok, detail in self_test():
        print(("PASS" if ok else "FAIL"), name, detail)
    if not all(ok for _, ok, _ in self_test()):
        raise SystemExit(1)
    print("risk_variants ok")
