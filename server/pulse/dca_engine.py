#!/usr/bin/env python3
"""CTS DCA — independent of Block, Indications, and the parent entry pack.

Steps fire on adverse move from average entry. Each step has its own distance
and volume multiplier. Last-15 PF / last-25 R score this book alone; average
loss on last 25 deactivates further adds.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# DCA is an opt-in add-on lane: the overlay switch `dcaEnabled` (off by default) turns it on. The hard-off constant stays
# as a build-time kill switch (True: never enabled, whatever the overlay says).
DCA_HARD_OFF = False

# Auto plan (dcaAuto, on by default): rungs as fractions of the position's own stop distance, with a size multiple of the
# base size, chosen from scripts/dca_eval.py (walk-forward on executed Base positions): one late small rung is the least
# harmful shape on every window measured (more rungs and larger multiples only scale the losing trades), the stop stays at
# the original level, and the lane's own add-on PF switches it off when the adds do not pay (score()).
AUTO_RUNG_FRACS = [0.4]
AUTO_RUNG_MULT = 0.5
AUTO_MAX_EXPOSURE_X = 2.0       # base size + all adds never above this multiple of the base size

from position_cost import GATE_MIN_PF_DEFAULT, POSITION_COST_PCT_DEFAULT, gate_min_pf, last_n_cost_pf, normalize_cost_pct, signed_result_r

DEFAULT_DIST = [0.5, 1.0, 1.5, 2.0]
DEFAULT_MULT = [1.5, 2.0, 2.3, 2.5]


def _pct_list(raw: Any, fallback: List[float]) -> List[float]:
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.replace("[", "").replace("]", "").split(",") if x.strip()]
    out: List[float] = []
    if isinstance(raw, (list, tuple)):
        for x in raw:
            try:
                n = float(x)
            except Exception:
                continue
            out.append(max(0.0005, min(0.08, n / 100.0)))   # distances are percent, always
    return out or list(fallback)


def _mult_list(raw: Any, fallback: List[float]) -> List[float]:
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.replace("[", "").replace("]", "").split(",") if x.strip()]
    out: List[float] = []
    if isinstance(raw, (list, tuple)):
        for x in raw:
            try:
                n = float(x)
            except Exception:
                continue
            out.append(max(0.25, min(8.0, n)))
    return out or list(fallback)


def adverse_pct(side: str, entry: float, px: float) -> float:
    if entry <= 0 or px <= 0:
        return 0.0
    if side == "LONG":
        return (entry - px) / entry
    return (px - entry) / entry


@dataclass
class DcaStep:
    n: int
    distance_pct: float
    mult: float
    filled: bool = False
    qty: float = 0.0
    px: float = 0.0
    t: float = 0.0
    cid: str = ""
    paused: bool = False


@dataclass
class DcaLane:
    symbol: str
    side: str
    parent_qty: float
    avg_entry: float
    steps: List[DcaStep] = field(default_factory=list)
    last_add: float = 0.0
    filled_n: int = 0
    ref_entry: float = 0.0       # the entry the position opened at (auto plan measures its rungs from here, not the average)


class DcaBook:
    def __init__(self) -> None:
        self.enabled = False  # DCA off by default — desk overlay can enable it
        self.auto = True               # dcaAuto: the rung plan is calculated (AUTO_*), else the static lists below
        self.on_sets = False           # dcaOnSets: Set positions may be added to (the add-on is scored on its own fills)
        self.on_axis = False           # dcaOnAxis: Set positions admitted through an enabled Axis may be added to
        self.max_expo = AUTO_MAX_EXPOSURE_X
        self.max_steps = 0
        self.distances = [d / 100.0 for d in DEFAULT_DIST]
        self.mults = list(DEFAULT_MULT)
        self.tp_mode = "average"
        self.be_pct = 0.002
        self.cooldown_s = 30.0
        self.pf_n = 15
        self.deact_n = 25
        self.min_pf = GATE_MIN_PF_DEFAULT
        self.auto_deact = True
        self.cost_pct = POSITION_COST_PCT_DEFAULT
        self.active = True
        self.deact_reason = ""
        self.lanes: Dict[str, DcaLane] = {}
        self.closes: List[Dict[str, Any]] = []
        self.last_pick = ""
        self.emits = 0
        self.skips = 0

    def key(self, symbol: str, side: str) -> str:
        return f"{symbol}:{side}"

    def load(self, ov: Dict[str, Any], cts: Optional[Dict[str, Any]] = None) -> None:
        cts = cts or {}
        coord = cts.get("coordination_settings") or cts.get("coordinationSettings") or {}
        self.enabled = bool(ov.get("dcaEnabled", False)) and not DCA_HARD_OFF
        self.auto = bool(ov.get("dcaAuto", True))
        self.on_sets = bool(ov.get("dcaOnSets", False))
        self.on_axis = bool(ov.get("dcaOnAxis", False))
        try:
            self.max_expo = max(1.0, min(8.0, float(ov.get("dcaMaxExposureX") or AUTO_MAX_EXPOSURE_X)))
        except Exception:
            self.max_expo = AUTO_MAX_EXPOSURE_X
        try:
            raw_steps = ov.get("dcaMaxSteps")
            if raw_steps is None:
                raw_steps = coord.get("dcaMaxSteps")
            if raw_steps is None:
                raw_steps = cts.get("dcaMaxSteps")
            step_n = int(raw_steps if raw_steps is not None else 0)
        except Exception:
            step_n = 0
        # 0 = unlimited. Seed from configured distances; due() grows steps on demand.
        self.max_steps = 0 if step_n <= 0 else max(1, step_n)
        dist = ov.get("dcaStepDistancesPct") or coord.get("dcaStepDistancesPct") or cts.get("dcaStepDistancesPct") or DEFAULT_DIST
        self.distances = _pct_list(dist, [d / 100.0 for d in DEFAULT_DIST])
        if self.max_steps > 0:
            while len(self.distances) < self.max_steps:
                self.distances.append(self.distances[-1] + 0.005)
            self.distances = self.distances[: self.max_steps]
        mult = ov.get("dcaStepVolumeMultipliers") or coord.get("dcaStepVolumeMultipliers") or cts.get("dcaStepVolumeMultipliers") or DEFAULT_MULT
        self.mults = _mult_list(mult, DEFAULT_MULT)
        if self.max_steps > 0:
            while len(self.mults) < self.max_steps:
                self.mults.append(self.mults[-1])
            self.mults = self.mults[: self.max_steps]
        self.tp_mode = str(ov.get("dcaTakeProfitMode") or coord.get("dcaTakeProfitMode") or cts.get("dcaTakeProfitMode") or "average")
        be_raw = ov.get("dcaBreakevenProfitPct", coord.get("dcaBreakevenProfitPct", cts.get("dcaBreakevenProfitPct", 0.2)))
        be = float(be_raw if be_raw is not None else 0.2)
        self.be_pct = be / 100.0   # dcaBreakevenProfitPct is percent
        cd_raw = ov.get("dcaCooldownSeconds", coord.get("dcaCooldownSeconds", cts.get("dcaCooldownSeconds", 30)))
        self.cooldown_s = float(cd_raw if cd_raw is not None else 30)
        self.pf_n = max(5, int(ov.get("dcaPfWindow") or ov.get("setPfWindow") or 15))
        self.deact_n = max(10, int(ov.get("dcaDeactN") or ov.get("setDeactN") or 25))
        self.min_pf = gate_min_pf(ov, "dcaMinPf", "minPf")
        self.auto_deact = bool(ov.get("dcaAutoDeact", True))
        self.cost_pct = normalize_cost_pct(ov.get("positionCostPct"))

    def unlimited(self) -> bool:
        return int(self.max_steps or 0) <= 0

    def _dist_at(self, i: int) -> float:
        if i < len(self.distances):
            return self.distances[i]
        last = self.distances[-1] if self.distances else 0.005
        return min(0.08, last + 0.005 * (i - len(self.distances) + 1))

    def _mult_at(self, i: int) -> float:
        if i < len(self.mults):
            return self.mults[i]
        return self.mults[-1] if self.mults else 1.5

    def _seed_n(self) -> int:
        if self.max_steps > 0:
            return self.max_steps
        return max(len(self.distances), len(self.mults), 4)

    def auto_steps(self, sl_frac: float) -> List[DcaStep]:
        """The calculated rungs for a position whose stop sits sl_frac (a fraction of the entry) away: each rung a fraction
        of that distance, never nearer than one PositionCost and never at the stop."""
        cf = self.cost_pct / 100.0
        sl = float(sl_frac or 0.0)
        if sl <= 0:
            sl = 4 * cf                 # no stop known: a nominal four PositionCosts
        out = []
        for i, f in enumerate(AUTO_RUNG_FRACS):
            d = min(max(f * sl, cf), 0.9 * sl)
            out.append(DcaStep(n=i + 1, distance_pct=d, mult=AUTO_RUNG_MULT))
        return out

    def attach(self, symbol: str, side: str, qty: float, entry: float, sl_frac: float = 0.0) -> DcaLane:
        k = self.key(symbol, side)
        lane = self.lanes.get(k)
        if lane is None:
            if self.auto:
                steps = self.auto_steps(sl_frac)
            else:
                n = self._seed_n()
                steps = [
                    DcaStep(n=i + 1, distance_pct=self._dist_at(i), mult=self._mult_at(i))
                    for i in range(n)
                ]
            lane = DcaLane(symbol=symbol, side=side, parent_qty=qty, avg_entry=entry, steps=steps, ref_entry=entry)
            self.lanes[k] = lane
        else:
            if lane.parent_qty <= 0 and qty > 0:
                lane.parent_qty = qty
        return lane

    def drop(self, symbol: str, side: str) -> None:
        self.lanes.pop(self.key(symbol, side), None)

    def score(self) -> Dict[str, Any]:
        pc = last_n_cost_pf(self.closes, self.pf_n, self.cost_pct)
        last25 = [r for r in self.closes if r.get("pnl_pct") is not None][-self.deact_n :]   # a missing value is not a loss
        avg_r = 0.0
        if last25:
            avg_r = sum(signed_result_r(float(r.get("pnl_pct") or 0), self.cost_pct) for r in last25) / len(last25)
        if self.auto_deact and len(last25) >= self.deact_n and avg_r < 0:
            self.active = False
            self.deact_reason = f"last{len(last25)} avgR {avg_r:.2f}<0"
        elif pc["count"] >= min(8, self.pf_n) and pc["pf"] + 1e-9 < self.min_pf:
            self.active = False
            self.deact_reason = f"last15 PF {pc['pf']:.2f}<{self.min_pf:.2f}"
        else:
            if not self.active and avg_r >= 0 and (pc["count"] < 8 or pc["pf"] >= self.min_pf):
                self.active = True
                self.deact_reason = ""
        pc["last25AvgR"] = round(avg_r, 4)
        pc["active"] = self.active
        pc["deactReason"] = self.deact_reason
        return pc

    def due(self, symbol: str, side: str, qty: float, entry: float, px: float, now: Optional[float] = None, sl_frac: float = 0.0) -> Optional[Dict[str, Any]]:
        if DCA_HARD_OFF or not self.enabled:
            return None
        self.score()
        if not self.active:
            self.skips += 1
            return None
        now = time.time() if now is None else float(now)
        lane = self.attach(symbol, side, qty, entry, sl_frac)
        if self.cooldown_s > 0 and now - lane.last_add < self.cooldown_s:
            return None
        adv = adverse_pct(side, (lane.ref_entry or entry) if self.auto else (lane.avg_entry or entry), px)
        nxt = None
        for st in lane.steps:
            if st.filled or st.paused:
                continue
            nxt = st
            break
        if nxt is None:
            if self.unlimited() and not self.auto:
                i = len(lane.steps)
                nxt = DcaStep(n=i + 1, distance_pct=self._dist_at(i), mult=self._mult_at(i))
                lane.steps.append(nxt)
            else:
                return None
        if adv + 1e-12 < nxt.distance_pct:
            return None
        add_qty = max(0.0, (lane.parent_qty or qty) * nxt.mult)
        added = sum(x.qty for x in lane.steps if x.filled)
        if self.auto:      # the calculated plan keeps the whole position under max_expo x the base size
            add_qty = min(add_qty, max(0.0, (lane.parent_qty or qty) * (self.max_expo - 1.0) - added))
        if add_qty <= 0:
            return None
        self.last_pick = f"{symbol}#{nxt.n}"
        return {
            "n": nxt.n,
            "distancePct": nxt.distance_pct,
            "mult": nxt.mult,
            "qty": add_qty,
            "adversePct": adv,
            "avgEntry": lane.avg_entry or entry,
            "lane": lane,
            "step": nxt,
        }

    def record_fill(self, lane: DcaLane, step: DcaStep, qty: float, px: float, cid: str, now: Optional[float] = None) -> None:
        """Stamp a fill on the clock due() compares against: the caller's bar or trade time, else the live clock."""
        t = time.time() if now is None else float(now)
        step.filled = True
        step.qty = qty
        step.px = px
        step.t = t
        step.cid = cid
        prev_q = lane.parent_qty + sum(s.qty for s in lane.steps if s.filled and s is not step)
        tot = prev_q + qty
        if tot > 0:
            lane.avg_entry = ((lane.avg_entry * prev_q) + px * qty) / tot
        lane.last_add = t
        lane.filled_n += 1
        self.emits += 1

    def on_close(self, rec: Dict[str, Any]) -> None:
        why = str(rec.get("reason") or "")
        cid = str(rec.get("client_id") or rec.get("clientId") or "")
        if not rec.get("dca_n") and "dca" not in why.lower() and not (len(cid) > 4 and cid[4:5] == "d"):
            return
        self.closes.append(rec)
        if len(self.closes) > 80:
            self.closes = self.closes[-80:]
        self.drop(str(rec.get("symbol") or ""), str(rec.get("side") or ""))
        self.score()

    def snapshot(self) -> Dict[str, Any]:
        pc = self.score()
        lanes = []
        for lane in self.lanes.values():
            lanes.append({
                "symbol": lane.symbol,
                "side": lane.side,
                "parentQty": lane.parent_qty,
                "avgEntry": lane.avg_entry,
                "filledN": lane.filled_n,
                "steps": [
                    {
                        "n": s.n,
                        "distancePct": round(s.distance_pct * 100, 3),
                        "mult": s.mult,
                        "filled": s.filled,
                        "qty": s.qty,
                        "paused": s.paused,
                    }
                    for s in lane.steps
                ],
            })
        return {
            "enabled": self.enabled,
            "auto": self.auto,
            "onSets": self.on_sets,
            "onAxis": self.on_axis,
            "maxExposureX": self.max_expo,
            "active": self.active,
            "deactReason": self.deact_reason,
            "maxSteps": self.max_steps,
            "distancesPct": [round(d * 100, 3) for d in self.distances],
            "mults": self.mults,
            "tpMode": self.tp_mode,
            "bePct": round(self.be_pct * 100, 3),
            "cooldownS": self.cooldown_s,
            "lastPick": self.last_pick,
            "emits": self.emits,
            "skips": self.skips,
            "pf15": pc.get("pf"),
            "ratio": pc.get("ratio"),
            "last25AvgR": pc.get("last25AvgR"),
            "last15N": pc.get("count"),
            "lanes": lanes,
        }


def _math_self_test() -> List[Tuple[str, bool, str]]:
    b = DcaBook()
    b.load({"dcaEnabled": True, "dcaAuto": False, "dcaMaxSteps": 4, "dcaStepDistancesPct": [0.5, 1, 1.5, 2], "dcaStepVolumeMultipliers": [1.5, 2, 2.3, 2.5], "dcaCooldownSeconds": 0})
    t0 = time.time()
    # no add at entry
    r = b.due("AAA-USDT", "LONG", 1.0, 100.0, 100.0, now=t0)
    t1 = (r is None, f"flat={r}")
    # 0.4% adverse < 0.5% step1
    r = b.due("AAA-USDT", "LONG", 1.0, 100.0, 99.6, now=t0)
    t2 = (r is None, "below")
    # 0.6% adverse → step 1, qty 1.5
    r = b.due("AAA-USDT", "LONG", 1.0, 100.0, 99.4, now=t0)
    t3 = (r is not None and r["n"] == 1 and abs(r["qty"] - 1.5) < 1e-9, f"{r}")
    assert r is not None
    b.record_fill(r["lane"], r["step"], r["qty"], 99.4, "Gx02dtest1", now=t0)
    # cooldown 0, step2 needs 1%
    r2 = b.due("AAA-USDT", "LONG", 1.0, 100.0, 99.4, now=t0)
    t4 = (r2 is None and r["step"].t == t0 and r["lane"].last_add == t0, "need 1pct; fill stamped at the injected clock")
    r2 = b.due("AAA-USDT", "LONG", 1.0, 100.0, 98.6, now=t0)
    t5 = (r2 is not None and r2["n"] == 2 and abs(r2["qty"] - 2.0) < 1e-9, f"{r2}")
    # short side
    rs = b.due("BBB-USDT", "SHORT", 2.0, 50.0, 50.4, now=t0)  # +0.8% against short, step1=0.5%
    t6 = (rs is not None and rs["n"] == 1, f"short {rs}")
    # independent of block: two symbols
    t7 = (len(b.lanes) >= 2, f"lanes={list(b.lanes)}")
    # last25 deact
    for i in range(25):
        b.on_close({"symbol": "AAA-USDT", "side": "LONG", "reason": "dca:sl", "client_id": "Gx02dxx", "pnl": -0.02, "pnl_pct": -0.002})
    t8 = (b.active is False, f"active={b.active} {b.deact_reason}")
    # disabled
    b2 = DcaBook()
    b2.load({"dcaEnabled": False})
    t9 = (b2.due("Z-USDT", "LONG", 1, 10, 9, now=t0) is None, "off")
    snap = b.snapshot()
    t10 = (snap["enabled"] and snap["maxSteps"] == 4 and "distancesPct" in snap, str(snap.get("distancesPct")))
    u = DcaBook()
    u.load({"dcaEnabled": True, "dcaAuto": False, "dcaMaxSteps": 0, "dcaStepDistancesPct": [0.5, 1], "dcaStepVolumeMultipliers": [1.5, 2], "dcaCooldownSeconds": 0})
    t11 = (u.max_steps <= 0 and u.unlimited(), f"steps={u.max_steps}")
    ulane = u.attach("UUU-USDT", "LONG", 1.0, 100.0)
    t12 = (len(ulane.steps) >= 2, f"seed={len(ulane.steps)}")
    for st in list(ulane.steps):
        st.filled = True
        st.qty = 1.0
    ru = u.due("UUU-USDT", "LONG", 1.0, 100.0, 90.0, now=t0)
    t13 = (ru is not None and int(ru["n"]) > 2, f"grow={None if ru is None else ru.get('n')}")
    return [
        ("dca-flat", t1[0], t1[1]),
        ("dca-below", t2[0], t2[1]),
        ("dca-step1", t3[0], str(t3[1])[:80]),
        ("dca-need-step2", t4[0], t4[1]),
        ("dca-step2", t5[0], str(t5[1])[:80]),
        ("dca-short", t6[0], str(t6[1])[:80]),
        ("dca-indep-lanes", t7[0], t7[1]),
        ("dca-deact-last25", t8[0], t8[1]),
        ("dca-disabled", t9[0], t9[1]),
        ("dca-snap", t10[0], t10[1]),
        ("dca-unlim-flag", t11[0], t11[1]),
        ("dca-unlim-seed", t12[0], t12[1]),
        ("dca-unlim-grow", t13[0], t13[1]),
    ]




def self_test() -> List[Tuple[str, bool, str]]:
    out = _math_self_test()
    global DCA_HARD_OFF
    # off by default, kill switch wins over the overlay
    d = DcaBook()
    d.load({})
    off_default = (not d.enabled) and d.due("AAA-USDT", "LONG", 1.0, 100.0, 90.0, now=time.time()) is None
    saved = DCA_HARD_OFF
    DCA_HARD_OFF = True
    try:
        k = DcaBook()
        k.load({"dcaEnabled": True})
        killed = (not k.enabled) and k.due("AAA-USDT", "LONG", 1.0, 100.0, 90.0, now=time.time()) is None
    finally:
        DCA_HARD_OFF = saved
    out.append(("dca-off-by-default-and-kill-switch", off_default and killed, f"default off={off_default} kill switch={killed}"))
    # auto plan: rungs scale with the position's own stop distance, below the stop, one small rung, exposure ceiling
    a = DcaBook()
    a.load({"dcaEnabled": True, "dcaCooldownSeconds": 0})
    wide = a.due("AAA-USDT", "LONG", 1.0, 100.0, 99.0, now=1.0, sl_frac=0.01)      # 0.4 x 1 % = 0.4 % rung, price 1 % down
    near = DcaBook()
    near.load({"dcaEnabled": True, "dcaCooldownSeconds": 0})
    not_yet = near.due("BBB-USDT", "LONG", 1.0, 100.0, 99.8, now=1.0, sl_frac=0.01)  # only 0.2 % down: rung at 0.4 % not reached
    ok_auto = bool(wide) and abs(wide["distancePct"] - 0.004) < 1e-9 and abs(wide["qty"] - AUTO_RUNG_MULT) < 1e-9 and not_yet is None
    out.append(("dca-auto-plan-scales-with-the-stop", ok_auto, f"rung={wide and wide['distancePct']} qty={wide and wide['qty']} early={not_yet}"))
    lane = wide["lane"] if wide else None
    capped = False
    if lane:
        a.record_fill(lane, wide["step"], wide["qty"], 99.0, "c1", now=1.0)
        again = a.due("AAA-USDT", "LONG", 1.0, 100.0, 90.0, now=2.0, sl_frac=0.01)
        capped = again is None          # the only auto rung is filled
    out.append(("dca-auto-single-rung-and-ceiling", capped, f"second add={'none' if capped else 'offered'}"))
    e = DcaBook()
    e.load({"dcaEnabled": True, "dcaCooldownSeconds": 0, "dcaMaxExposureX": 1.2})
    capped_add = e.due("EEE-USDT", "LONG", 1.0, 100.0, 99.0, now=1.0, sl_frac=0.01)
    out.append(("dca-auto-exposure-ceiling", bool(capped_add) and abs(capped_add["qty"] - 0.2) < 1e-9, f"qty under a 1.2x ceiling={capped_add and capped_add['qty']} (partial 0.5)"))
    # the lane is scored on its own add-on results and switches off when they lose
    c = DcaBook()
    c.load({"dcaEnabled": True, "dcaCooldownSeconds": 0, "dcaPfWindow": 8, "dcaMinPf": 1.2, "minPfLinked": False})
    for i in range(10):
        c.on_close({"symbol": "S%d-USDT" % i, "side": "LONG", "reason": "sl", "client_id": "", "dca_n": 1, "pnl_pct": -0.004})
    out.append(("dca-add-on-lane-switches-off-on-losses", (not c.active) and c.due("S-USDT", "LONG", 1.0, 100.0, 99.0, now=3.0, sl_frac=0.01) is None, f"active={c.active} {c.deact_reason}"))
    return out


if __name__ == "__main__":
    rows = self_test()
    bad = 0
    for name, ok, detail in rows:
        print(("PASS" if ok else "FAIL"), name, detail)
        bad += int(not ok)
    print("dca_engine", "ok" if not bad else f"fail={bad}")
    raise SystemExit(1 if bad else 0)
