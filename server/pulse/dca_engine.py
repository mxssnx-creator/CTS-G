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

from position_cost import POSITION_COST_PCT_DEFAULT, last_n_cost_pf, signed_result_r

DEFAULT_DIST = [0.5, 1.0, 1.5, 2.0]
DEFAULT_MULT = [1.5, 2.0, 2.3, 2.5]
DCA_STEPS_MAX = 12


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
            if n > 0.08:
                n = n / 100.0
            out.append(max(0.0005, min(0.08, n)))
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
            out.append(max(0.25, min(2.5, n)))
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


class DcaBook:
    def __init__(self) -> None:
        self.enabled = False  # DCA off by default — desk overlay can enable it
        self.max_steps = 0
        self.distances = [d / 100.0 for d in DEFAULT_DIST]
        self.mults = list(DEFAULT_MULT)
        self.tp_mode = "average"
        self.be_pct = 0.002
        self.cooldown_s = 30.0
        self.pf_n = 15
        self.deact_n = 25
        self.min_pf = 1.25
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
        self.enabled = bool(ov.get("dcaEnabled", False))
        try:
            raw_steps = ov.get("dcaMaxSteps")
            if raw_steps is None:
                raw_steps = coord.get("dcaMaxSteps")
            if raw_steps is None:
                raw_steps = cts.get("dcaMaxSteps")
            step_n = int(raw_steps if raw_steps is not None else 0)
        except Exception:
            step_n = 0
        dist = ov.get("dcaStepDistancesPct") or coord.get("dcaStepDistancesPct") or cts.get("dcaStepDistancesPct") or DEFAULT_DIST
        self.distances = _pct_list(dist, [d / 100.0 for d in DEFAULT_DIST])
        # 0 = use the configured distance list (never unbounded grow).
        # Zero means the configured distance list, never an unbounded book.
        # A hard ceiling prevents malformed settings from allocating huge lanes.
        self.max_steps = max(1, min(DCA_STEPS_MAX, step_n if step_n > 0 else len(self.distances) or 4))
        if self.max_steps > 0:
            while len(self.distances) < self.max_steps:
                self.distances.append(self.distances[-1] + 0.005)
            self.distances = self.distances[: self.max_steps]
        # First add must be a real adverse move, not 0.5% noise.
        if self.distances:
            self.distances[0] = max(0.012, float(self.distances[0]))
            for i in range(1, len(self.distances)):
                self.distances[i] = max(float(self.distances[i]), self.distances[i - 1] + 0.004)
        mult = ov.get("dcaStepVolumeMultipliers") or coord.get("dcaStepVolumeMultipliers") or cts.get("dcaStepVolumeMultipliers") or DEFAULT_MULT
        self.mults = _mult_list(mult, DEFAULT_MULT)
        if self.max_steps > 0:
            while len(self.mults) < self.max_steps:
                self.mults.append(self.mults[-1])
            self.mults = self.mults[: self.max_steps]
        self.tp_mode = str(ov.get("dcaTakeProfitMode") or coord.get("dcaTakeProfitMode") or cts.get("dcaTakeProfitMode") or "average")
        be_raw = ov.get("dcaBreakevenProfitPct", coord.get("dcaBreakevenProfitPct", cts.get("dcaBreakevenProfitPct", 0.2)))
        be = float(be_raw if be_raw is not None else 0.2)
        self.be_pct = be / 100.0 if be > 0.05 else be
        cd_raw = ov.get("dcaCooldownSeconds", coord.get("dcaCooldownSeconds", cts.get("dcaCooldownSeconds", 30)))
        self.cooldown_s = float(cd_raw if cd_raw is not None else 30)
        self.pf_n = max(5, int(ov.get("dcaPfWindow") or ov.get("setPfWindow") or 15))
        self.deact_n = max(10, int(ov.get("dcaDeactN") or ov.get("setDeactN") or 25))
        self.min_pf = float(ov.get("dcaMinPf") or ov.get("minPf") or 1.25)
        self.auto_deact = bool(ov.get("dcaAutoDeact", True))
        self.cost_pct = float(ov.get("positionCostPct") or POSITION_COST_PCT_DEFAULT)
        if self.cost_pct > 2:
            self.cost_pct = self.cost_pct / 100.0
        if self.cost_pct > 1:
            self.cost_pct = POSITION_COST_PCT_DEFAULT

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

    def attach(self, symbol: str, side: str, qty: float, entry: float) -> DcaLane:
        k = self.key(symbol, side)
        lane = self.lanes.get(k)
        if lane is None:
            n = self._seed_n()
            steps = [
                DcaStep(n=i + 1, distance_pct=self._dist_at(i), mult=self._mult_at(i))
                for i in range(n)
            ]
            lane = DcaLane(symbol=symbol, side=side, parent_qty=qty, avg_entry=entry, steps=steps, last_add=time.time())
            self.lanes[k] = lane
        else:
            if lane.parent_qty <= 0 and qty > 0:
                lane.parent_qty = qty
            if float(lane.last_add or 0) <= 0:
                lane.last_add = time.time()
        return lane

    def drop(self, symbol: str, side: str) -> None:
        self.lanes.pop(self.key(symbol, side), None)

    def score(self) -> Dict[str, Any]:
        pc = last_n_cost_pf(self.closes, self.pf_n, self.cost_pct)
        last25 = self.closes[-self.deact_n :]
        avg_r = 0.0
        if last25:
            avg_r = sum(signed_result_r(float(r.get("pnl_pct") or 0), self.cost_pct) for r in last25) / len(last25)
        if self.auto_deact and len(last25) >= self.deact_n and avg_r < 0:
            self.active = False
            self.deact_reason = f"last{len(last25)} avgR {avg_r:.2f}<0"
        elif pc["count"] >= min(8, self.pf_n) and pc["ratio"] + 1e-9 < self.min_pf:
            self.active = False
            self.deact_reason = f"last15 PF {pc['ratio']:.2f}<{self.min_pf:.2f}"
        else:
            if not self.active and avg_r >= 0 and (pc["count"] < 8 or pc["ratio"] >= self.min_pf):
                self.active = True
                self.deact_reason = ""
        pc["last25AvgR"] = round(avg_r, 4)
        pc["active"] = self.active
        pc["deactReason"] = self.deact_reason
        return pc

    def due(self, symbol: str, side: str, qty: float, entry: float, px: float, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        self.score()
        if not self.active:
            self.skips += 1
            return None
        now = now or time.time()
        lane = self.attach(symbol, side, qty, entry)
        if self.cooldown_s > 0 and now - lane.last_add < self.cooldown_s:
            return None
        adv = adverse_pct(side, lane.avg_entry or entry, px)
        nxt = None
        for st in lane.steps:
            if st.filled or st.paused:
                continue
            nxt = st
            break
        if nxt is None:
            if self.unlimited():
                i = len(lane.steps)
                nxt = DcaStep(n=i + 1, distance_pct=self._dist_at(i), mult=self._mult_at(i))
                lane.steps.append(nxt)
            else:
                return None
        if adv + 1e-12 < nxt.distance_pct:
            return None
        add_qty = max(0.0, (lane.parent_qty or qty) * nxt.mult)
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

    def record_fill(self, lane: DcaLane, step: DcaStep, qty: float, px: float, cid: str) -> None:
        step.filled = True
        step.qty = qty
        step.px = px
        step.t = time.time()
        step.cid = cid
        prev_q = lane.parent_qty + sum(s.qty for s in lane.steps if s.filled and s is not step)
        tot = prev_q + qty
        if tot > 0:
            lane.avg_entry = ((lane.avg_entry * prev_q) + px * qty) / tot
        lane.last_add = time.time()
        lane.filled_n += 1
        self.emits += 1

    def on_close(self, rec: Dict[str, Any]) -> None:
        why = str(rec.get("reason") or "")
        cid = str(rec.get("client_id") or rec.get("clientId") or "")
        sym = str(rec.get("symbol") or "")
        side = str(rec.get("side") or "")
        lane = self.lanes.get(self.key(sym, side)) if sym else None
        used = bool(lane and int(getattr(lane, "filled_n", 0) or 0) > 0)
        kind_d = len(cid) > 4 and cid[4:5].lower() == "d"
        if not used and "dca" not in why.lower() and not kind_d:
            return
        self.closes.append(rec)
        if len(self.closes) > 80:
            self.closes = self.closes[-80:]
        self.drop(sym, side)
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
            "last15Ratio": pc.get("ratio"),
            "last25AvgR": pc.get("last25AvgR"),
            "last15N": pc.get("count"),
            "lanes": lanes,
        }


def self_test() -> List[Tuple[str, bool, str]]:
    b = DcaBook()
    b.load({"dcaEnabled": True, "dcaMaxSteps": 4, "dcaStepDistancesPct": [0.5, 1, 1.5, 2], "dcaStepVolumeMultipliers": [1.5, 2, 2.3, 2.5], "dcaCooldownSeconds": 0})
    t0 = time.time()
    # no add at entry
    r = b.due("AAA-USDT", "LONG", 1.0, 100.0, 100.0, now=t0)
    t1 = (r is None, f"flat={r}")
    # 0.4% adverse < 1.2% first step
    r = b.due("AAA-USDT", "LONG", 1.0, 100.0, 99.6, now=t0)
    t2 = (r is None, "below")
    # 1.3% adverse → step 1, qty 1.5
    r = b.due("AAA-USDT", "LONG", 1.0, 100.0, 98.7, now=t0)
    t3 = (r is not None and r["n"] == 1 and abs(r["qty"] - 1.5) < 1e-9, f"{r}")
    assert r is not None
    b.record_fill(r["lane"], r["step"], r["qty"], 98.7, "Gx02dtest1")
    # cooldown 0, step2 needs first+0.4%
    r2 = b.due("AAA-USDT", "LONG", 1.0, 100.0, 98.7, now=t0)
    t4 = (r2 is None, "need next")
    r2 = b.due("AAA-USDT", "LONG", 1.0, 100.0, 97.0, now=t0)
    t5 = (r2 is not None and r2["n"] == 2 and abs(r2["qty"] - 2.0) < 1e-9, f"{r2}")
    # short side ≥ first distance
    rs = b.due("BBB-USDT", "SHORT", 2.0, 50.0, 50.7, now=t0)
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
    u.load({"dcaEnabled": True, "dcaMaxSteps": 0, "dcaStepDistancesPct": [0.5, 1], "dcaStepVolumeMultipliers": [1.5, 2], "dcaCooldownSeconds": 0})
    t11 = (u.max_steps == 2 and not u.unlimited(), f"steps={u.max_steps}")
    ulane = u.attach("UUU-USDT", "LONG", 1.0, 100.0)
    t12 = (len(ulane.steps) == 2, f"seed={len(ulane.steps)}")
    for st in list(ulane.steps):
        st.filled = True
        st.qty = 1.0
    ru = u.due("UUU-USDT", "LONG", 1.0, 100.0, 90.0, now=t0)
    t13 = (ru is None, f"grow={None if ru is None else ru.get('n')}")
    # parent qty stays frozen after fills
    p = DcaBook()
    p.load({"dcaEnabled": True, "dcaMaxSteps": 2, "dcaStepDistancesPct": [0.5, 1], "dcaStepVolumeMultipliers": [1.5, 2], "dcaCooldownSeconds": 0})
    r1 = p.due("PPP-USDT", "LONG", 1.0, 100.0, 98.7, now=t0)
    t14 = (r1 is not None and abs(r1["qty"] - 1.5) < 1e-9 and abs(r1["lane"].parent_qty - 1.0) < 1e-9, f"parent={None if r1 is None else r1['lane'].parent_qty}")
    if r1:
        p.record_fill(r1["lane"], r1["step"], r1["qty"], 98.7, "Gx02p1")
        p.attach("PPP-USDT", "LONG", 2.5, 98.7)  # later qty must not rewrite parent
    t15 = (abs(p.lanes["PPP-USDT:LONG"].parent_qty - 1.0) < 1e-9, f"frozen={p.lanes.get('PPP-USDT:LONG') and p.lanes['PPP-USDT:LONG'].parent_qty}")
    hi = DcaBook()
    hi.load({"dcaEnabled": True, "dcaStepVolumeMultipliers": [2.1, 3.7, 4.8, 6.2]})
    t16 = (max(hi.mults) <= 2.5 and abs(hi.mults[0] - 2.1) < 1e-9, f"mults={hi.mults}")
    capped = DcaBook()
    capped.load({"dcaEnabled": True, "dcaMaxSteps": 999, "dcaStepDistancesPct": [1], "dcaCooldownSeconds": 0})
    t16b = (capped.max_steps == DCA_STEPS_MAX and len(capped.distances) == DCA_STEPS_MAX, f"steps={capped.max_steps}")
    cd = DcaBook()
    cd.load({"dcaEnabled": True, "dcaMaxSteps": 2, "dcaStepDistancesPct": [0.5, 1], "dcaStepVolumeMultipliers": [1.5, 2], "dcaCooldownSeconds": 30})
    t_open = time.time()
    cd.attach("CD-USDT", "LONG", 1.0, 100.0)
    rcd = cd.due("CD-USDT", "LONG", 1.0, 100.0, 98.7, now=t_open + 3)
    t17 = (rcd is None, f"early={rcd}")
    rcd2 = cd.due("CD-USDT", "LONG", 1.0, 100.0, 98.7, now=t_open + 31)
    t18 = (rcd2 is not None and rcd2["n"] == 1, f"after={None if rcd2 is None else rcd2.get('n')}")
    parent = DcaBook()
    parent.load({"dcaEnabled": True, "dcaMaxSteps": 2, "dcaStepDistancesPct": [0.5, 1], "dcaStepVolumeMultipliers": [1.5, 2], "dcaCooldownSeconds": 0})
    pr = parent.due("QQQ-USDT", "LONG", 1.0, 100.0, 98.7, now=t0)
    if pr:
        parent.record_fill(pr["lane"], pr["step"], pr["qty"], 98.7, "Gx01dxx")
    parent.on_close({"symbol": "QQQ-USDT", "side": "LONG", "reason": "sl", "client_id": "Gx01og060308000ab", "pnl": -0.02, "pnl_pct": -0.002})
    t19 = (len(parent.closes) == 1, f"parent-close n={len(parent.closes)}")
    t20 = (parent.distances[0] >= 0.012 - 1e-12, f"minDist={parent.distances}")
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
        ("dca-zero-means-list", t11[0], t11[1]),
        ("dca-zero-seed", t12[0], t12[1]),
        ("dca-no-unbounded-grow", t13[0], t13[1]),
        ("dca-parent-step1", t14[0], str(t14[1])[:80]),
        ("dca-parent-frozen", t15[0], t15[1]),
        ("dca-mult-clamp", t16[0], t16[1]),
        ("dca-hard-step-cap", t16b[0], t16b[1]),
        ("dca-cooldown-from-open", t17[0], t17[1]),
        ("dca-cooldown-elapsed", t18[0], t18[1]),
        ("dca-parent-close-scores", t19[0], t19[1]),
        ("dca-min-first-dist", t20[0], t20[1]),
    ]


if __name__ == "__main__":
    rows = self_test()
    bad = 0
    for name, ok, detail in rows:
        print(("PASS" if ok else "FAIL"), name, detail)
        bad += int(not ok)
    print("dca_engine", "ok" if not bad else f"fail={bad}")
    raise SystemExit(1 if bad else 0)
