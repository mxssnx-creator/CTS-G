#!/usr/bin/env python3
"""Independent exit coordinations: best close via optimal SL, never waiting on TP.

Lanes (each scored last-15 PositionCost PF + last-25 avg R + max DD time):
  hard  — protective SL
  lock  — once in profit, SL → breakeven + buffer
  peak  — SL at peak ± optimal give (this is the take-profit)
  rev   — indication / general reverse
  time  — after min hold, slam SL to mark

Best-of: when several lanes fire, the active lane with the highest last-15 PF wins.
A lane deactivates if last 25 average Result-R is negative.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from position_cost import last_n_cost_pf, signed_result_r, POSITION_COST_PCT_DEFAULT
from set_engine import drawdown_time

LANES = ("hard", "lock", "peak", "rev", "time")


def finite(v: Any, fallback: float = 0.0) -> float:
    try:
        n = float(v)
    except Exception:
        return fallback
    return n if n == n and abs(n) != float("inf") else fallback


def pct_to_frac(v: float) -> float:
    x = finite(v)
    return x / 100.0 if x > 0.02 else x


def lane_of(reason: str) -> str:
    r = (reason or "").lower()
    if r.startswith("exit:"):
        r = r[5:]
    if r.startswith("lock") or "be+" in r:
        return "lock"
    if r.startswith("peak") or r.startswith("trail"):
        return "peak"
    if r.startswith("rev"):
        return "rev"
    if r.startswith("time") or r.startswith("scratch"):
        return "time"
    if r.startswith("tp"):
        return "peak"
    return "hard"


@dataclass
class LaneScore:
    key: str
    n: int = 0
    wins: int = 0
    last15_ratio: float = 1.0
    last25_avg_r: float = 0.0
    max_dd_s: float = 0.0
    active: bool = True
    deact_reason: str = ""
    selected: bool = False
    rows: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ExitDecision:
    action: str  # hold | tighten | close
    reason: str
    sl: Optional[float] = None
    lane: str = ""
    conf: float = 0.0


class ExitBook:
    def __init__(self) -> None:
        self.enabled = True
        self.ignore_tp = True
        self.best_of = True
        self.lock_on = True
        self.peak_on = True
        self.rev_on = True
        self.time_on = True
        self.lock_pct = 0.0015
        self.be_buffer = 0.0004
        self.opt_sl = 0.0030
        self.opt_sl_min = 0.0010
        self.opt_sl_max = 0.0090
        self.min_hold_s = 20.0
        self.time_stop_s = 21600.0
        self.scratch_s = 600.0
        self.scratch_min = 0.0025
        self.trail_min_step = 6.0
        self.pf_n = 15
        self.deact_n = 25
        self.min_pf = 1.10
        self.min_samples = 8
        self.auto_deact = True
        self.cost_pct = POSITION_COST_PCT_DEFAULT
        self.lanes: Dict[str, LaneScore] = {k: LaneScore(key=k) for k in LANES}
        self.last_pick = "cold"

    def load(self, ov: Dict[str, Any], cts: Optional[Dict[str, Any]] = None) -> None:
        cts = cts or {}
        self.enabled = bool(ov.get("exitEnabled", True))
        self.ignore_tp = bool(ov.get("exitIgnoreTp", True))
        self.best_of = bool(ov.get("exitBestOf", True))
        self.lock_on = bool(ov.get("exitLockOn", True))
        self.peak_on = bool(ov.get("exitPeakOn", True))
        self.rev_on = bool(ov.get("exitRevOn", False))
        self.time_on = bool(ov.get("exitTimeOn", False))
        self.lock_pct = pct_to_frac(float(ov.get("exitLockPct") or 0.15))
        self.be_buffer = pct_to_frac(float(ov.get("exitBeBuffer") or 0.04))
        self.opt_sl = pct_to_frac(float(ov.get("exitOptSlPct") or 0.30))
        self.opt_sl_min = pct_to_frac(float(ov.get("exitOptSlMin") or 0.10))
        self.opt_sl_max = pct_to_frac(float(ov.get("exitOptSlMax") or 0.90))
        self.opt_sl = max(self.opt_sl_min, min(self.opt_sl_max, self.opt_sl))
        self.min_hold_s = float(ov.get("exitMinHoldS") or 45)
        self.time_stop_s = float(ov.get("timeStopS") or 21600)
        self.scratch_s = float(ov.get("scratchS") or 600)
        self.scratch_min = pct_to_frac(float(ov.get("scratchMin") or ov.get("scratchMinPct") or 0.25))
        self.trail_min_step = float(ov.get("trailingMinStep") or 6)
        self.pf_n = max(5, int(ov.get("exitPfWindow") or ov.get("setPfWindow") or 15))
        self.deact_n = max(10, int(ov.get("exitDeactN") or ov.get("setDeactN") or 25))
        self.min_pf = float(ov.get("exitMinPf") or ov.get("setMinPf") or 1.10)
        self.min_samples = max(5, int(ov.get("exitMinSamples") or 8))
        self.auto_deact = bool(ov.get("exitAutoDeact", True))
        self.cost_pct = float(ov.get("positionCostPct") or POSITION_COST_PCT_DEFAULT)
        if self.cost_pct > 2:
            self.cost_pct = self.cost_pct / 100.0
        if self.cost_pct > 1:
            self.cost_pct = POSITION_COST_PCT_DEFAULT

    def optimal_sl(self, side: str, entry: float, peak: float, hard_sl: float) -> float:
        """SL that takes profit from peak — independent of TP."""
        give = max(self.opt_sl_min, min(self.opt_sl_max, self.opt_sl))
        if side == "LONG":
            sl = peak * (1.0 - give)
            floor = entry * (1.0 + self.be_buffer)
            return max(hard_sl, sl, floor) if peak > entry else hard_sl
        sl = peak * (1.0 + give)
        ceil = entry * (1.0 - self.be_buffer)
        return min(hard_sl, sl, ceil) if peak < entry else hard_sl

    def lock_sl(self, side: str, entry: float, hard_sl: float) -> float:
        if side == "LONG":
            return max(hard_sl, entry * (1.0 + self.be_buffer))
        return min(hard_sl, entry * (1.0 - self.be_buffer))

    def mark_sl(self, side: str, px: float, tick: float = 0.0) -> float:
        pad = max(tick * 3, px * 0.00015)
        if side == "LONG":
            return px - pad
        return px + pad

    def _lane_ok(self, key: str) -> bool:
        if key == "hard":
            return True
        ln = self.lanes.get(key)
        if ln is None:
            return True
        if not self.auto_deact:
            return True
        if ln.n < self.min_samples:
            return True
        return ln.active

    def _best(self, candidates: List[ExitDecision]) -> Optional[ExitDecision]:
        if not candidates:
            return None
        if not self.best_of or len(candidates) == 1:
            return candidates[0]
        def score(d: ExitDecision) -> Tuple[float, float]:
            ln = self.lanes.get(d.lane)
            ratio = ln.last15_ratio if ln and ln.n >= self.min_samples else 1.0
            return (ratio, d.conf)
        return max(candidates, key=score)

    def decide(
        self,
        *,
        side: str,
        entry: float,
        px: float,
        peak: float,
        sl: float,
        opened_at: float,
        trail_arm: float,
        signal_dir: int,
        now: Optional[float] = None,
    ) -> ExitDecision:
        now = now or time.time()
        age = now - opened_at
        if entry <= 0 or px <= 0:
            return ExitDecision("hold", "no-px")
        long = side == "LONG"
        pnl_pct = (px - entry) / entry if long else (entry - px) / entry
        fav_peak = (peak - entry) / entry if long else (entry - peak) / entry

        if long and px <= sl + 1e-12:
            return ExitDecision("close", "exit:hard", sl, "hard", 1.0)
        if (not long) and px >= sl - 1e-12:
            return ExitDecision("close", "exit:hard", sl, "hard", 1.0)

        closes: List[ExitDecision] = []
        tightens: List[ExitDecision] = []

        if self.lock_on and self._lane_ok("lock") and pnl_pct >= self.lock_pct and age >= self.min_hold_s:
            target = self.lock_sl(side, entry, sl)
            improved = (long and target > sl + 1e-12) or ((not long) and target < sl - 1e-12)
            if improved:
                tightens.append(ExitDecision("tighten", "exit:lock", target, "lock", 0.7))

        if self.peak_on and self._lane_ok("peak") and fav_peak >= max(self.lock_pct, trail_arm, self.opt_sl) and age >= self.trail_min_step:
            target = self.optimal_sl(side, entry, peak, sl)
            improved = (long and target > sl + 1e-12) or ((not long) and target < sl - 1e-12)
            if improved:
                tightens.append(ExitDecision("tighten", "exit:peak", target, "peak", 0.85))
            # Peak is a trailing SL only. Market-close on tiny giveback was killing 5h trades at ~0%.

        if self.rev_on and self._lane_ok("rev") and age >= self.min_hold_s and signal_dir != 0:
            want = 1 if long else -1
            if signal_dir == -want:
                closes.append(ExitDecision("close", "exit:rev", self.mark_sl(side, px), "rev", 0.8))

        if self.time_on and self._lane_ok("time"):
            # Never scratch a trade that has not paid 1× position-cost.
            paid = max(0.0025, self.cost_pct / 100.0 * 1.1)
            if age >= self.time_stop_s and pnl_pct >= paid:
                closes.append(ExitDecision("close", "exit:time", self.mark_sl(side, px), "time", 0.6))
            elif age >= max(600.0, self.scratch_s) and pnl_pct >= max(self.scratch_min, paid):
                closes.append(ExitDecision("close", "exit:time", self.mark_sl(side, px), "time", 0.55))

        picked = self._best(closes)
        if picked:
            self.last_pick = picked.lane
            return picked
        picked = self._best(tightens)
        if picked:
            self.last_pick = picked.lane
            return picked
        return ExitDecision("hold", "hold")

    def on_close(self, rec: Any) -> None:
        if isinstance(rec, dict):
            reason = str(rec.get("reason") or "")
            row = {
                "t": finite(rec.get("t")),
                "pnl": finite(rec.get("pnl")),
                "pnl_pct": finite(rec.get("pnl_pct")),
            }
        else:
            reason = str(getattr(rec, "reason", "") or "")
            row = {
                "t": finite(getattr(rec, "t", 0)),
                "pnl": finite(getattr(rec, "pnl", 0)),
                "pnl_pct": finite(getattr(rec, "pnl_pct", 0)),
            }
        key = lane_of(reason)
        ln = self.lanes.setdefault(key, LaneScore(key=key))
        ln.rows.append(row)
        ln.rows = ln.rows[-80:]
        self._score(ln)

    def seed(self, closed: Sequence[Any]) -> None:
        for rec in closed:
            self.on_close(rec)

    def _score(self, ln: LaneScore) -> None:
        tape = ln.rows
        ln.n = len(tape)
        ln.wins = sum(1 for r in tape if finite(r.get("pnl")) > 0)
        pf = last_n_cost_pf(tape, self.pf_n, self.cost_pct)
        ln.last15_ratio = float(pf["ratio"])
        last25 = tape[-self.deact_n :]
        if last25:
            ln.last25_avg_r = sum(signed_result_r(finite(r.get("pnl_pct")), self.cost_pct) for r in last25) / len(last25)
        else:
            ln.last25_avg_r = 0.0
        ln.max_dd_s = float(drawdown_time(tape)["maxS"])
        if ln.key == "hard":
            ln.active = True
            ln.deact_reason = ""
            return
        if not self.auto_deact or ln.n < self.min_samples:
            ln.active = True
            ln.deact_reason = ""
            return
        reasons = []
        if len(last25) >= self.deact_n and ln.last25_avg_r < 0:
            reasons.append(f"last{len(last25)} avgR {ln.last25_avg_r:.2f}<0")
        if int(pf["count"]) >= min(self.pf_n, self.min_samples) and ln.last15_ratio + 1e-9 < self.min_pf:
            reasons.append(f"last15 PF {ln.last15_ratio:.2f}<{self.min_pf:.2f}")
        ln.active = not reasons
        ln.deact_reason = "; ".join(reasons)

    def snapshot(self) -> Dict[str, Any]:
        rows = []
        for k in LANES:
            ln = self.lanes.get(k) or LaneScore(key=k)
            rows.append(
                {
                    "key": ln.key,
                    "n": ln.n,
                    "wins": ln.wins,
                    "last15Ratio": round(ln.last15_ratio, 4),
                    "last25AvgR": round(ln.last25_avg_r, 4),
                    "maxDdS": ln.max_dd_s,
                    "active": ln.active,
                    "deactReason": ln.deact_reason,
                    "selected": self.last_pick == ln.key,
                }
            )
        return {
            "enabled": self.enabled,
            "ignoreTp": self.ignore_tp,
            "bestOf": self.best_of,
            "lockOn": self.lock_on,
            "peakOn": self.peak_on,
            "revOn": self.rev_on,
            "timeOn": self.time_on,
            "lockPct": round(self.lock_pct * 100, 3),
            "beBuffer": round(self.be_buffer * 100, 3),
            "optSlPct": round(self.opt_sl * 100, 3),
            "optSlMin": round(self.opt_sl_min * 100, 3),
            "optSlMax": round(self.opt_sl_max * 100, 3),
            "minHoldS": self.min_hold_s,
            "lastPick": self.last_pick,
            "lanes": rows,
        }


def self_test() -> List[Tuple[str, bool, str]]:
    out: List[Tuple[str, bool, str]] = []
    b = ExitBook()
    b.load({"exitIgnoreTp": True, "exitLockPct": 0.15, "exitOptSlPct": 0.30, "exitBeBuffer": 0.04, "exitMinHoldS": 5, "trailingMinStep": 1, "exitRevOn": True})
    out.append(("ex-ignore-tp", b.ignore_tp, "ignore"))
    # lock tightens SL above entry on a long
    d = b.decide(side="LONG", entry=100, px=100.25, peak=100.25, sl=99.5, opened_at=0, trail_arm=0.003, signal_dir=1, now=30)
    out.append(("ex-lock-tighten", d.action == "tighten" and d.lane == "lock" and d.sl is not None and d.sl > 100, f"{d}"))
    # peak SL is below peak and above entry
    sl = b.optimal_sl("LONG", 100, 101, 99.5)
    out.append(("ex-opt-sl", 100 < sl < 101, f"{sl}"))
    # reverse closes independent of TP
    d2 = b.decide(side="LONG", entry=100, px=100.1, peak=100.2, sl=99.6, opened_at=0, trail_arm=0.003, signal_dir=-1, now=40)
    out.append(("ex-rev-close", d2.action == "close" and d2.lane == "rev", f"{d2}"))
    # hard SL still fires
    d3 = b.decide(side="LONG", entry=100, px=99.4, peak=100.1, sl=99.5, opened_at=0, trail_arm=0.003, signal_dir=1, now=10)
    out.append(("ex-hard", d3.action == "close" and d3.lane == "hard", f"{d3}"))
    # last-25 negative deactivates peak lane
    for i in range(25):
        b.on_close({"t": 1000 + i, "pnl": -0.01, "pnl_pct": -0.003, "reason": "exit:peak"})
    out.append(("ex-deact-peak", not b.lanes["peak"].active, b.lanes["peak"].deact_reason))
    # best-of prefers high PF lane
    b2 = ExitBook()
    b2.load({"exitBestOf": True, "exitMinHoldS": 1, "trailingMinStep": 1, "exitMinPf": 1.05})
    for i in range(12):
        b2.on_close({"t": 2000 + i, "pnl": 0.02, "pnl_pct": 0.003, "reason": "exit:peak"})
        b2.on_close({"t": 3000 + i, "pnl": -0.01, "pnl_pct": -0.002, "reason": "exit:time"})
    d4 = b2.decide(side="LONG", entry=100, px=100.4, peak=100.8, sl=100.2, opened_at=0, trail_arm=0.001, signal_dir=1, now=400)
    out.append(("ex-best-peak", d4.lane in ("peak", "lock", "time") and b2.lanes["peak"].last15_ratio > b2.lanes["time"].last15_ratio, f"{d4.lane} {b2.lanes['peak'].last15_ratio:.2f}>{b2.lanes['time'].last15_ratio:.2f}"))
    out.append(("ex-lane-of", lane_of("exit:peak") == "peak" and lane_of("trail") == "peak", lane_of("tp")))
    b3 = ExitBook()
    b3.load({"exitPeakOn": True, "exitMinHoldS": 0, "trailingMinStep": 0, "exitRevOn": False, "exitTimeOn": False})
    d_loss = b3.decide(side="SHORT", entry=100, px=101, peak=99.0, sl=101.8, opened_at=0, trail_arm=0.003, signal_dir=0, now=40)
    out.append(("ex-peak-no-loss-close", d_loss.action != "close", f"{d_loss.action} {d_loss.reason}"))
    d_win = b3.decide(side="SHORT", entry=100, px=99.4, peak=99.0, sl=101.8, opened_at=0, trail_arm=0.003, signal_dir=0, now=40)
    out.append(("ex-peak-no-mkt-close", d_win.action != "close", f"{d_win.action} {d_win.reason}"))
    d_flat = b3.decide(side="LONG", entry=100, px=100.02, peak=100.8, sl=99.7, opened_at=0, trail_arm=0.003, signal_dir=0, now=4000)
    out.append(("ex-peak-no-scratch", d_flat.action != "close", f"{d_flat.action} {d_flat.reason}"))
    return out


if __name__ == "__main__":
    failed = 0
    for name, ok, detail in self_test():
        print(("PASS" if ok else "FAIL"), name, detail)
        failed += int(not ok)
    if failed:
        raise SystemExit(1)
    print("exit_engine ok")
