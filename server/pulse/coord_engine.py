#!/usr/bin/env python3
"""CTS-accurate Main-stage axes, rearrangements, and threshold gates for pulse."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Sequence, Tuple
from position_cost import LAST_N_DEFAULT, POSITION_COST_PCT_DEFAULT, last_n_cost_pf, normalize_pf
from contracts import AXES, VOLUME_RATIO_UNIT, stable_key

AXIS_SPECS = {
    "prev": {"min": 4, "max": 12, "step": 2, "default": 12},
    "last": {"min": 1, "max": 4, "step": 1, "default": 4},
    "cont": {"min": 1, "max": 8, "step": 1, "default": 8},
    "pause": {"min": 1, "max": 8, "step": 1, "default": 8},
}


def clamp_window(axis: str, value: Any) -> int:
    spec = AXIS_SPECS[axis]
    try:
        parsed = int(value)
    except Exception:
        parsed = spec["default"]
    clamped = max(spec["min"], min(spec["max"], parsed))
    return spec["min"] + ((clamped - spec["min"]) // spec["step"]) * spec["step"]


def profit_factor(pnls: Sequence[float]) -> float:
    gp = sum(x for x in pnls if x > 0)
    gl = abs(sum(x for x in pnls if x < 0))
    if gl <= 0:
        return 99.0 if gp > 0 else 1.0
    return gp / gl


def consec_loss(pnls: Sequence[float]) -> int:
    n = 0
    for x in reversed(pnls):
        if x < 0:
            n += 1
        else:
            break
    return n


@dataclass
class Axis:
    enabled: bool
    max_window: int


class Coordinator:
    def __init__(self) -> None:
        self.axes: Dict[str, Axis] = {
            "prev": Axis(True, 12),
            "last": Axis(True, 4),
            "cont": Axis(True, 8),
            "pause": Axis(True, 8),
        }
        self.min_pf = 1.15
        # Stage PF floors use the shared 0.80–2.50 / 0.02 contract.
        self.stage_min_pf = {"base": 1.05, "main": 1.10, "real": 1.15}
        self.pf_window = LAST_N_DEFAULT
        self.position_cost_pct = POSITION_COST_PCT_DEFAULT
        self.noise = 0.05
        self.vol_weight = 0.3
        self.outbreak = [3, 5, 10]
        self.prev_min_count = 5
        self.prev_window = 25
        self.main_eval = 5
        self.real_eval = 3
        self.min_step = 3
        self.max_sl_ratio = 2.5
        self.trailing_min_step = 3
        self.pos_count_vol_ratio = 0.05
        self.rearrange = True
        self.rearrange_gap = 0.22
        self.last: Dict[str, Any] = {}
        self.coordination: Dict[str, Dict[str, int]] = {
            axis: {
                "evaluated": 0,
                "qualified": 0,
                "selected": 0,
                "entered": 0,
                "exited": 0,
                "blocked": 0,
                "rejected": 0,
                "paused": 0,
                "long": 0,
                "short": 0,
            }
            for axis in AXES
        }
        self._axis_seen: set[str] = set()

    def load(self, cts: Dict[str, Any], ov: Dict[str, Any]) -> None:
        coord = cts.get("coordination_settings") or cts.get("coordinationSettings") or {}
        nested = coord.get("axes") if isinstance(coord, dict) else {}
        nested = nested or {}

        def ax(name: str, cap: str, default_on: bool = True) -> Axis:
            n = nested.get(name) or {}
            en = ov.get(f"axis{cap}Enabled")
            if en is None:
                en = cts.get(f"axis{cap}Enabled", n.get("enabled", default_on))
            win = ov.get(f"axis{cap}MaxWindow", n.get("maxWindow") or cts.get(f"axis{cap}MaxWindow") or AXIS_SPECS[name]["default"])
            return Axis(bool(en), clamp_window(name, win))

        self.axes = {
            "prev": ax("prev", "Prev"),
            "last": ax("last", "Last"),
            "cont": ax("cont", "Cont"),
            "pause": ax("pause", "Pause"),
        }
        try:
            stages_cts = (cts.get("strategies") or {}).get("main") or {}
            st = stages_cts.get("real") or {}
            self.min_pf = float(ov.get("realMinPf") or ov.get("minPf") or st.get("min_profit_factor") or cts.get("realProfitFactor") or 1.15)
        except Exception:
            self.min_pf = float(ov.get("realMinPf") or ov.get("minPf") or 1.15)
        # Per-stage floors: overlay wins, then strategies.main.<stage>, then the shared defaults.
        try:
            stages_cts = (cts.get("strategies") or {}).get("main") or {}
        except Exception:
            stages_cts = {}
        for _stage, _dflt in (("base", 1.05), ("main", 1.10), ("real", 1.15)):
            _v = ov.get(f"{_stage}MinPf")
            if _v is None:
                try:
                    _v = (stages_cts.get(_stage) or {}).get("min_profit_factor")
                except Exception:
                    _v = None
            if _v is None and _stage == "real":
                _v = self.min_pf
            try:
                self.stage_min_pf[_stage] = normalize_pf(_v, _dflt) if _v is not None else _dflt
            except Exception:
                self.stage_min_pf[_stage] = _dflt
        # Strictest stage (Real) is the canonical min PF consumers read.
        self.min_pf = self.stage_min_pf["real"]
        self.pf_window = int(ov.get("pfWindow") or 15)
        self.position_cost_pct = float(ov.get("positionCostPct") or cts.get("exchangePositionCost") or cts.get("positionCost") or POSITION_COST_PCT_DEFAULT)
        if self.position_cost_pct > 2:
            self.position_cost_pct = self.position_cost_pct / 100.0
        if self.position_cost_pct > 1:
            self.position_cost_pct = POSITION_COST_PCT_DEFAULT
        self.noise = float(ov.get("noise") or cts.get("activeNoiseFilter") or 0.05)
        self.vol_weight = float(ov.get("volWeight") or cts.get("activeVolatilityWeight") or 0.3)
        raw_ob = ov.get("outbreak") or cts.get("activeOutbreakRanges") or [3, 5, 10]
        if isinstance(raw_ob, str):
            raw_ob = [int(x) for x in raw_ob.replace("[", "").replace("]", "").split(",") if x.strip().isdigit()]
        self.outbreak = [int(x) for x in raw_ob][:4] or [3, 5, 10]
        self.prev_min_count = int(ov.get("prevPosMinCount") or coord.get("prevPosMinCount") or 5)
        self.prev_window = int(ov.get("prevPosWindow") or coord.get("prevPosWindow") or 25)
        self.main_eval = int(ov.get("mainEvalPosCount") or coord.get("mainEvalPosCount") or 5)
        self.real_eval = int(ov.get("realEvalPosCount") or coord.get("realEvalPosCount") or 3)
        self.min_step = int(ov.get("minStep") or coord.get("minStep") or 3)
        self.max_sl_ratio = float(ov.get("maxStopLossRatio") or coord.get("maxStopLossRatio") or 2.5)
        self.trailing_min_step = int(ov.get("trailingMinStep") or coord.get("trailingMinStep") or 3)
        self.pos_count_vol_ratio = float(ov.get("posCountsVolumeRatio") or coord.get("posCountsVolumeRatio") or cts.get("posCountsVolumeRatio") or 0.05)
        self.rearrange = bool(ov.get("rearrange", True))
        self.rearrange_gap = float(ov.get("rearrangeGap") or 0.22)

    def outbreak_ok(self, bars: Sequence[Sequence[float]]) -> bool:
        if len(bars) < max(self.outbreak + [self.min_step]):
            return False
        last = bars[-1][3] or 0
        if last <= 0:
            return False
        hits = 0
        for n in self.outbreak:
            w = bars[-n:]
            hi = max(b[1] for b in w)
            lo = min(b[2] for b in w)
            if (hi - lo) / last >= self.noise:
                hits += 1
        return hits >= 1

    def vol_boost(self, bars: Sequence[Sequence[float]]) -> float:
        if len(bars) < 12:
            return 0.0
        vols = [b[4] for b in bars]
        avg = sum(vols[-12:]) / 12 or 1.0
        last = vols[-1]
        if last > avg * (1 + self.vol_weight):
            return min(0.18, (last / avg - 1) * 0.08)
        return 0.0

    def gate(
        self,
        closed_rows: Sequence[Any],
        consec: int,
        intern: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, List[str], Dict[str, float]]:
        reasons: List[str] = []
        pnls = []
        for row in closed_rows:
            if isinstance(row, dict):
                pnls.append(float(row.get("pnl") or 0))
            else:
                pnls.append(float(getattr(row, "pnl", 0) or 0))
        last_w = self.axes["last"].max_window
        prev_w = min(self.prev_window, self.axes["prev"].max_window * 2)
        cost = last_n_cost_pf(closed_rows, self.pf_window, self.position_cost_pct)
        last_cost = last_n_cost_pf(closed_rows, last_w, self.position_cost_pct)
        prev_cost = last_n_cost_pf(closed_rows, prev_w, self.position_cost_pct)
        main_cost = last_n_cost_pf(closed_rows, max(3, self.main_eval), self.position_cost_pct)
        real_cost = last_n_cost_pf(closed_rows, max(3, self.real_eval), self.position_cost_pct)
        intern = intern or {}
        intern_pf = float(intern.get("pf") or intern.get("indications") or intern.get("general") or 0)
        intern_n = float(intern.get("n") or 0)
        metrics: Dict[str, float] = {
            "lastPf": round(float(last_cost["ratio"]), 3),
            "prevPf": round(float(prev_cost["ratio"]), 3),
            "consecLoss": float(consec),
            "last15Ratio": cost["ratio"],
            "last15R": cost["avgR"],
            "last15N": cost["count"],
            "classicPf15": cost["classicPf"],
            "costPct": cost["costPct"],
            "minPf": self.min_pf,
            "baseMinPf": float(self.stage_min_pf.get("base", 1.05)),
            "mainMinPf": float(self.stage_min_pf.get("main", 1.10)),
            "realMinPf": float(self.stage_min_pf.get("real", 1.15)),
            "pfNeutral": 1.0,
            "pfPlus1x": 1.1,
            "internPf": round(intern_pf, 4) if intern_pf else 0.0,
            "internN": intern_n,
            "mainPf": round(float(main_cost["ratio"]), 4),
            "mainN": float(main_cost["count"]),
            "realPf": round(float(real_cost["ratio"]), 4),
            "realN": float(real_cost["count"]),
        }
        allow = True
        sample_ok = cost["count"] >= min(8, self.pf_window)
        intern_ok = intern_n >= max(3, int(self.prev_min_count or 5)) and intern_pf + 1e-9 >= 1.0
        if intern_ok:
            metrics["internOpen"] = 1.0
        base_floor = float(self.stage_min_pf.get("base", 1.05))
        main_floor = float(self.stage_min_pf.get("main", 1.10))
        real_floor = float(self.stage_min_pf.get("real", 1.15))
        last_n_ok = int(last_cost["count"]) >= min(3, last_w)
        if self.axes["last"].enabled and last_n_ok:
            if last_cost["ratio"] + 1e-9 < base_floor:
                allow = False
                reasons.append(
                    f"base/last {int(last_cost['count'])} PF {last_cost['ratio']:.2f}<{base_floor:.2f} (1.00=neutral 1.10=+1×cost)"
                )
        if self.axes["prev"].enabled and sample_ok and int(prev_cost["count"]) >= self.prev_min_count:
            floor = base_floor * 0.85
            if prev_cost["ratio"] + 1e-9 < floor and cost["ratio"] + 1e-9 < floor:
                allow = False
                reasons.append(f"prev PF {prev_cost['ratio']:.2f}<{floor:.2f} (cost-scale)")
        if self.axes["pause"].enabled:
            pause_n = self.axes["pause"].max_window
            if consec >= pause_n or consec_loss(pnls[-pause_n:]) >= pause_n:
                allow = False
                reasons.append(f"pause {consec}/{pause_n}")
        # Main / real stages are advisory intern: they do not freeze the book.
        if sample_ok and float(main_cost["count"]) >= max(3, self.main_eval) and float(main_cost["ratio"]) + 1e-9 < main_floor:
            reasons.append(f"main {int(main_cost['count'])} PF {main_cost['ratio']:.2f}<{main_floor:.2f}")
        if sample_ok and float(real_cost["count"]) >= max(3, self.real_eval) and float(real_cost["ratio"]) + 1e-9 < real_floor:
            reasons.append(f"real {int(real_cost['count'])} PF {real_cost['ratio']:.2f}<{real_floor:.2f}")
        stages = {
            "intern": {"pf": intern_pf, "n": intern_n, "open": bool(intern_ok)},
            "base": {"pf": float(cost["ratio"]), "n": float(cost["count"]), "minPf": base_floor},
            "main": {"pf": float(main_cost["ratio"]), "n": float(main_cost["count"]), "minPf": main_floor},
            "real": {"pf": float(real_cost["ratio"]), "n": float(real_cost["count"]), "minPf": real_floor},
            "last": {"pf": float(last_cost["ratio"]), "n": float(last_cost["count"]), "minPf": base_floor},
            "prev": {"pf": float(prev_cost["ratio"]), "n": float(prev_cost["count"])},
        }
        self.last = {"allow": allow, "reasons": reasons, "metrics": metrics, "stages": stages}
        return allow, reasons, metrics

    def record_coordination(self, axis: str, outcome: str, direction: str = "", event_key: str = "") -> bool:
        """Count one coordination outcome once per stable child/event key."""
        axis_name = str(axis or "").strip().lower()
        if axis_name not in self.coordination:
            return False
        key = event_key or stable_key(axis_name, outcome, direction)
        if key in self._axis_seen:
            return False
        self._axis_seen.add(key)
        if len(self._axis_seen) > 4096:
            self._axis_seen = set(list(self._axis_seen)[-2048:])
        bucket = self.coordination[axis_name]
        name = str(outcome or "").strip().lower()
        if name in bucket:
            bucket[name] += 1
        if str(direction or "").upper() in ("LONG", "SHORT"):
            bucket[str(direction).upper().lower()] += 1
        return True

    def axis_variants(
        self,
        parent_set_id: str,
        closed_rows: Sequence[Any],
        open_rows: Optional[Sequence[Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Build deterministic count-pos children without counting a parent twice.

        Prev/Last/Cont/Pause are evaluated from closed rows only. Open rows are
        accepted separately so callers cannot accidentally leak an active
        position into the Prev tape. Every child keeps the Base parent ID and a
        stable dedupe key; 100 relative positions equals one parent volume.
        """
        closed = [r for r in closed_rows if r is not None]
        _ = list(open_rows or ())  # explicit boundary: never mixed into closed
        out: List[Dict[str, Any]] = []
        for axis, spec in AXIS_SPECS.items():
            cfg = self.axes[axis]
            if not cfg.enabled:
                continue
            start = int(spec["min"])
            stop = min(int(cfg.max_window), int(spec["max"]))
            step = int(spec["step"])
            for count in range(start, stop + 1, step):
                if axis == "prev":
                    # Previous means the closed window immediately before the
                    # current closed window. Open positions never enter either
                    # window, even when a caller supplies open_rows.
                    tape = closed[-(count * 2) : -count] if len(closed) >= count * 2 else []
                else:
                    tape = closed[-count:]
                pf = last_n_cost_pf(tape, count, self.position_cost_pct)
                losses = [float((r.get("pnl") if isinstance(r, dict) else getattr(r, "pnl", 0)) or 0) for r in tape]
                paused = axis == "pause" and consec_loss(losses) >= count
                qualifies = (
                    len(tape) >= min(3, count)
                    and not paused
                    and float(pf.get("ratio") or 0.0) + 1e-9 >= float(self.stage_min_pf.get("base", 1.05))
                )
                child_key = stable_key(parent_set_id, axis, count, len(tape), round(float(pf.get("ratio") or 0.0), 6))
                self.record_coordination(axis, "evaluated", event_key=child_key + ":evaluated")
                if paused:
                    self.record_coordination(axis, "paused", event_key=child_key + ":paused")
                elif qualifies:
                    self.record_coordination(axis, "qualified", event_key=child_key + ":qualified")
                else:
                    self.record_coordination(axis, "blocked", event_key=child_key + ":blocked")
                out.append({
                    "axisKey": f"{axis}:{count}",
                    "axis": axis,
                    "parentSetId": str(parent_set_id),
                    "relativeCount": count,
                    "volumeRatio": round(count * VOLUME_RATIO_UNIT, 6),
                    "volumeRatioUnit": VOLUME_RATIO_UNIT,
                    "closedOnly": True,
                    "openExcluded": True,
                    "closedN": len(tape),
                    "pf": round(float(pf.get("ratio") or 1.0), 6),
                    "paused": paused,
                    "qualified": qualifies,
                    "qualificationReason": "qualified" if qualifies else (
                        "paused" if paused else f"closed={len(tape)} pf={float(pf.get('ratio') or 0.0):.2f}"
                    ),
                    "dedupeKey": child_key,
                })
        return out

    @staticmethod
    def aggregate_axis_variants(variants: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """Aggregate relative children once per parent while retaining details."""
        rows = [v for v in variants if isinstance(v, dict)]
        parents = sorted({str(v.get("parentSetId") or "") for v in rows if v.get("parentSetId")})
        by_parent: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            parent = str(row.get("parentSetId") or "")
            if parent:
                by_parent.setdefault(parent, []).append(row)
        parent_rows = []
        for parent in sorted(by_parent):
            children = by_parent[parent]
            qualified = [row for row in children if row.get("qualified")]
            parent_rows.append({
                "parentSetId": parent,
                "childCount": len(children),
                "qualifiedChildren": len(qualified),
                "childVolumeRatio": round(sum(float(row.get("volumeRatio") or 0) for row in qualified), 6),
                "countedVolumeRatio": VOLUME_RATIO_UNIT if qualified else 0.0,
            })
        return {
            "parentCount": len(parents),
            "parentSetIds": parents,
            "childCount": len(rows),
            "volumeRatio": round(sum(float(row.get("countedVolumeRatio") or 0) for row in parent_rows), 6),
            "childVolumeRatio": round(sum(float(row.get("childVolumeRatio") or 0) for row in parent_rows), 6),
            "volumeRatioUnit": VOLUME_RATIO_UNIT,
            "qualifiedChildren": sum(1 for v in rows if v.get("qualified")),
            "axes": {a: sum(1 for v in rows if v.get("axis") == a) for a in AXIS_SPECS},
            "parents": parent_rows,
            "rows": rows,
        }

    def size_mult(self, open_n: int) -> float:
        """Count-pos volume: each extra open trims new-entry size by posCountsVolumeRatio."""
        ratio = max(0.0, min(0.3, float(self.pos_count_vol_ratio or 0)))
        n = max(0, int(open_n or 0))
        return max(0.35, 1.0 - n * ratio)

    def add_stack_cap(self, configured_stack: int, last_pf: float) -> int:
        """Cont axis on additional strategies: weak last-PF keeps only the first half of counts."""
        stack = max(1, int(configured_stack or 1))
        if not self.axes["cont"].enabled:
            return stack
        try:
            pf = float(last_pf or 0)
        except Exception:
            pf = 0.0
        if pf + 1e-9 >= float(self.min_pf or 1.15):
            return stack
        return max(1, stack // 2)

    def add_gate(
        self,
        closed_rows: Sequence[Any],
        consec: int,
        intern: Optional[Dict[str, Any]] = None,
        count: Optional[int] = None,
        count_tape: Optional[Sequence[float]] = None,
    ) -> Tuple[bool, List[str], Dict[str, float]]:
        """Axis coordination for additional strategies (Block / DCA) using count-pos.

        Pause / last / prev apply independently of new entries. When a Block
        count tape is given, last+pause also run on that count's own history.
        """
        allow, reasons, metrics = self.gate(closed_rows, consec, intern=intern)
        if count is not None:
            n = max(1, int(count))
            tape = [float(x) for x in (count_tape or []) if x is not None]
            last_w = max(1, int(self.axes["last"].max_window))
            tail = tape[-max(last_w, n) :]
            metrics["count"] = float(n)
            metrics["countN"] = float(len(tail))
            if self.axes["last"].enabled and len(tail) >= min(3, last_w):
                gp = sum(x for x in tail if x > 0)
                gl = abs(sum(x for x in tail if x < 0))
                pf = (gp / gl) if gl > 0 else (2.0 if gp > 0 else 1.0)
                floor = float(self.stage_min_pf.get("base", 1.05))
                metrics["countPf"] = round(pf, 4)
                if pf + 1e-9 < floor:
                    allow = False
                    reasons.append(f"count-pos n={n} last{len(tail)} PF {pf:.2f}<{floor:.2f}")
            if self.axes["pause"].enabled and tail:
                pause_n = max(1, int(self.axes["pause"].max_window))
                cl = consec_loss(tail)
                metrics["countConsec"] = float(cl)
                if cl >= pause_n:
                    allow = False
                    reasons.append(f"count-pos pause n={n} {cl}/{pause_n}")
        metrics["addsAllow"] = 1.0 if allow else 0.0
        self.last = {**(self.last or {}), "addsAllow": allow, "addReasons": list(reasons), "metrics": metrics}
        return allow, reasons, metrics

    def slot_cap(self, max_open: int, last_pf: float) -> int:
        if max_open <= 0 or max_open >= 10**8:
            return 10**9
        cap = max_open
        if self.axes["cont"].enabled:
            extra = self.axes["cont"].max_window
            if last_pf >= self.min_pf:
                cap = min(max_open, extra)
            else:
                cap = min(max_open, max(2, extra // 2))
        return max(1, cap)

    def pick_rearrange(
        self,
        opens: List[Dict[str, Any]],
        ranked: List[Tuple[float, str, int, str]],
        max_open: int,
    ) -> Optional[Dict[str, Any]]:
        if not self.rearrange or not opens or not ranked:
            return None
        if len(opens) < max_open:
            return None
        weak = min(
            opens,
            key=lambda p: (p.get("uPnlPct", 0.0), -p.get("ageS", 0.0), p.get("conf", 0.0)),
        )
        best = ranked[0]
        best_conf, best_sym, best_d, best_why = best
        if best_sym == weak.get("symbol"):
            return None
        if best_conf < (weak.get("conf") or 0) + self.rearrange_gap:
            return None
        if weak.get("uPnlPct", 0) > 0.12:
            return None
        if weak.get("ageS", 0) < max(8.0, self.min_step * 2.0):
            return None
        return {
            "from": weak.get("symbol"),
            "to": best_sym,
            "dir": best_d,
            "why": best_why,
            "conf": best_conf,
            "weakPnl": weak.get("uPnlPct"),
        }

    def snapshot(self) -> Dict[str, Any]:
        return {
            "axes": {k: asdict(v) for k, v in self.axes.items()},
            "minPf": self.min_pf,
            "stageMinPf": dict(self.stage_min_pf),
            "pfWindow": self.pf_window,
            "positionCostPct": self.position_cost_pct,
            "pfNeutral": 1.0,
            "pfPlus1xCost": 1.1,
            "noise": self.noise,
            "volWeight": self.vol_weight,
            "outbreak": self.outbreak,
            "minStep": self.min_step,
            "maxSlRatio": self.max_sl_ratio,
            "trailingMinStep": self.trailing_min_step,
            "posCountVolRatio": self.pos_count_vol_ratio,
            "countPos": {
                "openMult": True,
                "addGate": True,
                "prevMinCount": self.prev_min_count,
                "prevWindow": self.prev_window,
                "mainEval": self.main_eval,
                "realEval": self.real_eval,
            },
            "rearrange": self.rearrange,
            "rearrangeGap": self.rearrange_gap,
            "mainEval": self.main_eval,
            "realEval": self.real_eval,
            "stages": (self.last or {}).get("stages") or {},
            "coordination": {
                "axes": {axis: dict(self.coordination.get(axis) or {}) for axis in AXES},
                "eventCount": len(self._axis_seen),
                "volumeRatioUnit": VOLUME_RATIO_UNIT,
            },
            "gate": self.last,
        }
