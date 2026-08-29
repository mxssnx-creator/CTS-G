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


class DcaBook:
    def __init__(self) -> None:
        self.enabled = True
        self.max_steps = 4
        self.distances = [d / 100.0 for d in DEFAULT_DIST]
        self.mults = list(DEFAULT_MULT)
        self.tp_mode = "average"
        self.be_pct = 0.002
        self.cooldown_s = 30.0
        self.pf_n = 15
        self.deact_n = 25
        self.min_pf = 1.10
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
        self.enabled = bool(ov.get("dcaEnabled", True))
        self.max_steps = max(1, min(8, int(ov.get("dcaMaxSteps") or coord.get("dcaMaxSteps") or cts.get("dcaMaxSteps") or 4)))
        dist = ov.get("dcaStepDistancesPct") or coord.get("dcaStepDistancesPct") or cts.get("dcaStepDistancesPct") or DEFAULT_DIST
        self.distances = _pct_list(dist, [d / 100.0 for d in DEFAULT_DIST])
        while len(self.distances) < self.max_steps:
            self.distances.append(self.distances[-1] + 0.005)
        self.distances = self.distances[: self.max_steps]
        mult = ov.get("dcaStepVolumeMultipliers") or coord.get("dcaStepVolumeMultipliers") or cts.get("dcaStepVolumeMultipliers") or DEFAULT_MULT
        self.mults = _mult_list(mult, DEFAULT_MULT)
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
        self.min_pf = float(ov.get("dcaMinPf") or ov.get("minPf") or 1.10)
        self.auto_deact = bool(ov.get("dcaAutoDeact", True))
        self.cost_pct = float(ov.get("positionCostPct") or POSITION_COST_PCT_DEFAULT)
        if self.cost_pct > 2:
            self.cost_pct = self.cost_pct / 100.0
        if self.cost_pct > 1:
            self.cost_pct = POSITION_COST_PCT_DEFAULT

    def attach(self, symbol: str, side: str, qty: float, entry: float) -> DcaLane:
        k = self.key(symbol, side)
        lane = self.lanes.get(k)
        if lane is None:
            steps = [
                DcaStep(n=i + 1, distance_pct=self.distances[i], mult=self.mults[i])
                for i in range(self.max_steps)
            ]
            lane = DcaLane(symbol=symbol, side=side, parent_qty=qty, avg_entry=entry, steps=steps)
            self.lanes[k] = lane
        else:
            lane.parent_qty = max(lane.parent_qty, qty)
            if lane.avg_entry <= 0:
                lane.avg_entry = entry
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
        if "dca" not in why.lower() and not (len(cid) > 4 and cid[4:5] == "d"):
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
    # 0.4% adverse < 0.5% step1
    r = b.due("AAA-USDT", "LONG", 1.0, 100.0, 99.6, now=t0)
    t2 = (r is None, "below")
    # 0.6% adverse → step 1, qty 1.5
    r = b.due("AAA-USDT", "LONG", 1.0, 100.0, 99.4, now=t0)
    t3 = (r is not None and r["n"] == 1 and abs(r["qty"] - 1.5) < 1e-9, f"{r}")
    assert r is not None
    b.record_fill(r["lane"], r["step"], r["qty"], 99.4, "Gx02dtest1")
    # cooldown 0, step2 needs 1%
    r2 = b.due("AAA-USDT", "LONG", 1.0, 100.0, 99.4, now=t0)
    t4 = (r2 is None, "need 1pct")
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
    ]


if __name__ == "__main__":
    rows = self_test()
    bad = 0
    for name, ok, detail in rows:
        print(("PASS" if ok else "FAIL"), name, detail)
        bad += int(not ok)
    print("dca_engine", "ok" if not bad else f"fail={bad}")
    raise SystemExit(1 if bad else 0)
