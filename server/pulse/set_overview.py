"""Bounded, source-separated previews for the hierarchical Sets browser.

System results cover Base-qualified configurations. Rejected configurations
remain in the catalog; confirmed exchange accounting remains complete.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from contracts import INDICATION_KINDS, stable_key

PREVIEW_PER_GROUP = 2


def number(value: Any, default: float = 0.0) -> float:
    import math
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def indication(row: dict, pack: str) -> str:
    kind = str(row.get("ind_kind") or row.get("indKind") or "").lower()
    if kind in INDICATION_KINDS:
        return kind
    reason = str(row.get("reason") or "")
    if reason.startswith("ind:") and reason.split(":")[1] in INDICATION_KINDS:
        return reason.split(":")[1]
    return "general" if pack == "general" else "combined"


def strategy(row: dict, trail: str = "") -> str:
    value = str(row.get("strategy") or row.get("pack") or "").lower()
    reason = str(row.get("reason") or "").lower()
    if value in ("block", "block-active") or reason.startswith("block"):
        return "block"
    if value == "dca" or reason.startswith("dca"):
        return "dca"
    if row.get("axis_key") or row.get("axisKey") or value == "axis":
        return "axis"
    if value in ("trailing", "trail") or trail not in ("", "0", "off"):
        return "trailing"
    return "normal"


def range_key(tp: Any) -> str:
    return f"{number(tp):.4f}" if number(tp) > 0 else "unknown"


class OverviewCollector:
    def __init__(self, book: Any):
        self.book = book
        self.groups: dict[tuple, dict] = {}
        self.previews: dict[tuple, list] = defaultdict(list)

    def add(self, meta: dict, tape: list, *, metrics: dict | None = None):
        key = (meta["scope"], meta["indicationKind"], range_key(meta.get("tpPct")), meta["strategyType"])
        group = self.groups.setdefault(key, {
            "scope": key[0], "indicationKind": key[1], "tpRange": key[2],
            "tpPct": meta.get("tpPct"), "strategyType": key[3], "setCount": 0,
        })
        group["setCount"] += 1
        # Retain references only for the bounded preview, not another catalog.
        rank = (-int(bool(tape) or bool(metrics)), -len(tape), str(meta["id"]))
        preview = self.previews[key]
        if len(preview) < PREVIEW_PER_GROUP or rank < preview[-1][0]:
            preview.append((rank, meta, tape, metrics))
            preview.sort(key=lambda item: item[0])
            del preview[PREVIEW_PER_GROUP:]

    def finish(self) -> dict:
        rows = []
        for key in sorted(self.groups):
            for _rank, meta, tape, supplied in self.previews[key]:
                m = supplied if supplied is not None else self.book._score_metrics(
                    sorted(tape, key=lambda row: number(row.get("t"))),
                    hist_n=len(tape), fast_historic=meta["scope"] == "system",
                )
                n = int(m.get("source_n", m.get("n", len(tape))))
                rows.append({
                    **meta, "tpRange": key[2], "n": n, "liveN": 0,
                    "histN": n if meta["scope"] == "system" else 0,
                    "sampleSource": "system-calculation" if meta["scope"] == "system" else "live-exchange",
                    "last15Ratio": m.get("last15_ratio"),
                    "last15N": int(m.get("last15_n", n)),
                    "last25AvgR": m.get("last25_avg_r"),
                    "maxDdS": m.get("max_dd_s"), "avgDdS": m.get("avg_dd_s"),
                    "wr": m.get("wr"), "expectancy": m.get("expectancy"),
                    "avgHoldS": m.get("avg_hold_s"),
                    "active": bool(m.get("active", False)),
                    "deactReason": str(m.get("reason") or ("" if n else "waiting for results")),
                    "costSubtracted": True,
                })
        return {"version": 1, "generatedAt": time.time(), "previewPerGroup": PREVIEW_PER_GROUP,
                "groups": list(self.groups.values()), "rows": rows}


def set_meta(st: Any, scope: str, kind: str, strat: str, suffix: str = "") -> dict:
    return {
        "id": stable_key(scope, st.id, kind, strat, suffix), "setId": st.id,
        "parentSetId": st.parent_set_id or st.id, "scope": scope,
        "indicationKind": kind, "strategyType": strat, "pack": st.pack,
        "tf": st.tf, "slRatio": st.sl_ratio, "trailKey": st.trail_key,
        "step": st.step, "tpPct": round(st.tp_pct * 100, 4),
    }


def build_overview(book: Any, axis_rows=(), *, axis_enabled: bool = True) -> dict:
    out = OverviewCollector(book)
    for st in book.by_idx:
        kind = "general" if st.pack == "general" else "combined"
        strat = "trailing" if st.kind == "trail" else "normal"
        if (st.stage_ledger or {}).get("base"):
            for side, metrics in st.by_side.items():
                if not book._base_metrics_ok(metrics):
                    continue
                meta = set_meta(st, "system", kind, strat, side)
                meta["side"] = side
                out.add(meta, [row for row in st.hist if str(row.get("side") or "").upper() == side])
        lanes: dict[tuple, list] = defaultdict(list)
        for row in st.live:
            # Open PnL, pending fills and unconfirmed local closes are never
            # Exchange results. The engine supplies completed round trips.
            if row.get("exchange_confirmed") is not True or row.get("partial") or row.get("ours") is False:
                continue
            key = (indication(row, st.pack), strategy(row, row.get("trail_key", st.trail_key)),
                   str(row.get("execution_lane") or ""), str(row.get("axis_key") or ""),
                   str(row.get("side") or ""), range_key(number(row.get("tp_pct")) * 100))
            lanes[key].append(row)
        for key, tape in lanes.items():
            if key[1] == "axis" and not axis_enabled:
                continue
            meta = set_meta(st, "exchange", key[0], key[1], "|".join(key[2:]))
            meta.update(axisKey=key[3], side=key[4], tpPct=number(tape[0].get("tp_pct")) * 100 or None)
            out.add(meta, tape)

    for meta, samples, metrics in qualified_strategy_results(book):
        out.add(meta, samples, metrics=metrics)

    for row in axis_rows if axis_enabled else ():
        st = book.sets.get(str(row.get("parentSetId") or ""))
        if st is None or not (st.stage_ledger or {}).get("base") or not row.get("qualified"):
            continue
        axis = str(row.get("axisKey") or "")
        meta = set_meta(st, "system", "general" if st.pack == "general" else "combined", "axis", axis)
        meta.update(axisKey=axis, relativeCount=row.get("relativeCount"))
        # Axis PF is its own closed window. Other metrics are unavailable in
        # the coordination record and must not inherit the parent's numbers.
        out.add(meta, [], metrics={"n": int(row.get("closedN") or 0), "last15_ratio": row.get("pf"),
                                  "active": bool(row.get("qualified")),
                                  "reason": row.get("qualificationReason") or ""})
    result = out.finish()
    qualified = sum(bool((st.stage_ledger or {}).get("base")) for st in book.by_idx)
    result["selection"] = {"catalogSets": len(book.by_idx), "baseQualifiedSets": qualified,
                           "excludedSets": len(book.by_idx) - qualified, "minPf": book.min_pf,
                           "systemScope": "base-qualified", "exchangeScope": "all-confirmed"}
    return result


def qualified_strategy_results(book: Any):
    """Independent strategy/range/direction Base checks, memoized by content."""
    from calculation_cache import CalculationCache
    from types import SimpleNamespace
    previous = getattr(book, "_strategy_result_cache", {})
    current = {}
    results = []
    for name, tape in list(book.ind_hist.items()) + list(book.strategy_hist.items()):
        buckets = defaultdict(list)
        for row in tape:
            # Unknown legacy ranges cannot qualify a current configuration.
            if not row.get("set_id") or not row.get("tp_pct") or not row.get("sl_ratio"):
                continue
            key = tuple(str(row.get(k) or "") for k in ("set_id", "pack", "ind_kind", "ind_config", "side",
                                                       "tp_pct", "sl_ratio", "trail_key", "step", "execution_lane"))
            buckets[key].append(row)
        for key, samples in buckets.items():
            sid = stable_key("system", name, *key)
            token = CalculationCache.signature(book, SimpleNamespace(hist=samples))
            cached = previous.get(sid)
            metrics = cached[1] if cached and cached[0] == token else book._score_metrics(samples)
            current[sid] = (token, metrics)
            if not book._base_metrics_ok(metrics):
                continue
            first = samples[0]
            meta = {"id": sid, "setId": key[0], "scope": "system", "pack": key[1],
                    "indicationKind": name if name in INDICATION_KINDS else indication(first, key[1]),
                    "strategyType": "normal" if name in INDICATION_KINDS else strategy({**first, "strategy": name.split(":")[0]}),
                    "indicationConfig": key[3], "side": key[4], "tpPct": number(first["tp_pct"]) * 100,
                    "slRatio": first["sl_ratio"], "trailKey": key[7], "step": first.get("step"), "tf": "1m"}
            results.append((meta, samples, metrics))
    book._strategy_result_cache = current
    return results


def merge_overviews(lanes: list[tuple[str, dict]]) -> dict | None:
    """Overall retains separate lane rows; PF is never averaged across desks."""
    groups: dict[tuple, dict] = {}
    rows = []
    stamps = []
    connections = []
    for connection, overview in lanes:
        if not isinstance(overview, dict) or overview.get("version") != 1:
            continue
        connections.append(connection)
        stamps.append(number(overview.get("generatedAt")))
        for group in overview.get("groups") or []:
            key = (group["scope"], group["indicationKind"], group["tpRange"], group["strategyType"])
            if key not in groups:
                groups[key] = {**group, "setCount": 0}
            groups[key]["setCount"] += int(group.get("setCount") or 0)
        for row in overview.get("rows") or []:
            rows.append({**row, "id": f"{connection}:{row['id']}", "connection": connection})
    if not connections:
        return None
    return {"version": 1, "generatedAt": min(stamps), "connections": connections,
            "groups": list(groups.values()), "rows": rows}
