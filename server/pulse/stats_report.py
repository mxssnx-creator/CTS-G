#!/usr/bin/env python3
"""Full Pulse results export: JSON, Markdown, and standalone HTML for PF / DDT / PositionCost-net, per Set intern, Block, DCA."""
from __future__ import annotations

import json
import os
import time
from html import escape
from typing import Any, Dict, List, Optional, Sequence, Tuple

from position_cost import (
    POSITION_COST_PCT_DEFAULT,
    cost_as_frac,
    evaluation_windows,
    last_n_cost_pf,
    net_pnl_pct,
    net_pnl_usdt,
    row_net_pnl,
    row_pnl_pct,
    row_position_cost_pct,
    signed_result_r,
)
from set_engine import drawdown_time_by_symbol, IND_KINDS


def _f(v: Any, fb: float = 0.0) -> float:
    try:
        n = float(v)
    except Exception:
        return fb
    return n if n == n and abs(n) != float("inf") else fb


def _row(c: Any) -> Dict[str, Any]:
    if isinstance(c, dict):
        has_pct = c.get("pnl_pct") is not None
        return {
            "t": _f(c.get("t")),
            "symbol": str(c.get("symbol") or ""),
            "side": str(c.get("side") or ""),
            "qty": _f(c.get("qty")),
            "entry": _f(c.get("entry")),
            "exit": _f(c.get("exit") or c.get("exit_px")),
            "pnl": _f(c.get("pnl")),
            "pnl_pct": _f(c.get("pnl_pct")),
            "entry_fee": _f(c.get("entry_fee") or c.get("entryFee")),
            "exit_fee": _f(c.get("exit_fee") or c.get("exitFee")),
            "fee_total": _f(c.get("fee_total") or c.get("feeTotal") or c.get("totalFee")),
            "position_cost_pct": c.get("position_cost_pct") if c.get("position_cost_pct") is not None else c.get("positionCostPct"),
            "cost_source": str(c.get("cost_source") or c.get("costSource") or ""),
            "fee": _f(c.get("fee")),
            "_pnl_pct_present": has_pct,
            "hold_s": _f(c.get("hold_s") or c.get("holdS")),
            "reason": str(c.get("reason") or ""),
            "set_id": str(c.get("set_id") or c.get("setId") or ""),
            "pack": str(c.get("pack") or ""),
            "client_id": str(c.get("client_id") or c.get("clientId") or ""),
            "sl_ratio": _f(c.get("sl_ratio") or c.get("slRatio")),
            "trail_key": str(c.get("trail_key") or c.get("trailKey") or ""),
            "ind_kind": str(c.get("ind_kind") or c.get("indKind") or ""),
            "indKind": str(c.get("indKind") or c.get("ind_kind") or ""),
        }
    return {
        "t": _f(getattr(c, "t", 0)),
        "symbol": str(getattr(c, "symbol", "") or ""),
        "side": str(getattr(c, "side", "") or ""),
        "qty": _f(getattr(c, "qty", 0)),
        "entry": _f(getattr(c, "entry", 0)),
        "exit": _f(getattr(c, "exit", 0)),
        "pnl": _f(getattr(c, "pnl", 0)),
        "pnl_pct": _f(getattr(c, "pnl_pct", 0)),
        "entry_fee": _f(getattr(c, "entry_fee", 0)),
        "exit_fee": _f(getattr(c, "exit_fee", 0)),
        "fee_total": _f(getattr(c, "fee_total", 0)),
        "position_cost_pct": getattr(c, "position_cost_pct", None),
        "cost_source": str(getattr(c, "cost_source", "") or ""),
        "fee": _f(getattr(c, "fee", 0)),
        "_pnl_pct_present": getattr(c, "pnl_pct", None) is not None,
        "hold_s": _f(getattr(c, "hold_s", 0)),
        "reason": str(getattr(c, "reason", "") or ""),
        "set_id": str(getattr(c, "set_id", "") or ""),
        "pack": str(getattr(c, "pack", "") or ""),
        "client_id": str(getattr(c, "client_id", "") or ""),
        "sl_ratio": _f(getattr(c, "sl_ratio", 0)),
        "trail_key": str(getattr(c, "trail_key", "") or ""),
        "ind_kind": str(getattr(c, "ind_kind", "") or ""),
        "indKind": str(getattr(c, "ind_kind", "") or ""),
    }


def enrich(row: Dict[str, Any], cost_pct: float) -> Dict[str, Any]:
    notion = max(0.0, row["qty"] * row["entry"])
    row_cost = row_position_cost_pct(row, cost_pct)
    if not row.get("_pnl_pct_present", True) and notion > 1e-12:
        # Persisted Closed.pnl is net of one PositionCost. Reconstruct the
        # gross move before applying cost again.
        gross_pct = row["pnl"] / notion + cost_as_frac(row_cost)
    else:
        gross_pct = row_pnl_pct(row, row_cost)
    net_pct = net_pnl_pct(gross_pct, row_cost)
    net_usdt = net_pnl_usdt(gross_pct, row["qty"], row["entry"], row_cost) if notion else row["pnl"]
    r = signed_result_r(gross_pct, row_cost)
    row["notional"] = round(notion, 6)
    row["grossPnlPct"] = round(gross_pct, 8)
    row["grossPnl"] = round(notion * gross_pct, 8)
    row["netPnlPct"] = round(net_pct, 8)
    row["netPnl"] = round(net_usdt, 8)
    row["resultR"] = round(r, 4)
    row["costPct"] = row_cost
    row["costSource"] = row.get("cost_source") or ("live-exchange" if row_cost != cost_pct else "manual-fallback")
    row["costUsdt"] = round(notion * cost_as_frac(row_cost), 8)
    row.pop("_pnl_pct_present", None)
    return row


def pf_window(rows: Sequence[Dict[str, Any]], n: Optional[int], cost_pct: float) -> Dict[str, Any]:
    ordered = sorted(list(rows), key=lambda r: _f(r.get("t")))
    src = ordered[-n:] if n else ordered
    gp = gl = 0.0
    gp_net = gl_net = 0.0
    wins = losses = 0
    holds: List[float] = []
    for r in src:
        pnl = _f(r.get("grossPnl", r.get("pnl")))
        net = _f(r.get("netPnl"), pnl)
        if pnl > 0:
            wins += 1
            gp += pnl
        elif pnl < 0:
            losses += 1
            gl += abs(pnl)
        if net > 0:
            gp_net += net
        elif net < 0:
            gl_net += abs(net)
        holds.append(_f(r.get("hold_s")))
    cost = last_n_cost_pf(src, len(src) or 1, cost_pct) if src else last_n_cost_pf([], 1, cost_pct)
    classic = 99.0 if gp > 0 and gl <= 0 else (gp / gl if gl else 0.0)
    classic_net = 99.0 if gp_net > 0 and gl_net <= 0 else (gp_net / gl_net if gl_net else 0.0)
    return {
        "n": len(src),
        "wins": wins,
        "losses": losses,
        "wr": round(100.0 * wins / max(1, wins + losses), 1),
        "gp": round(gp, 6),
        "gl": round(gl, 6),
        "net": round(gp - gl, 6),
        "pf": round(float(cost.get("ratio") or 1.0), 4),
        "classicPf": round(classic, 4),
        "gpNetCost": round(gp_net, 6),
        "glNetCost": round(gl_net, 6),
        "netAfterCost": round(gp_net - gl_net, 6),
        "pfAfterCost": round(float(cost.get("ratio") or 1.0), 4),
        "avgHoldS": round(sum(holds) / len(holds), 1) if holds else 0.0,
        "costRatio": cost.get("ratio"),
        "avgR": cost.get("avgR"),
        "classicPfCost": cost.get("classicPf"),
        "costPct": cost_pct,
        "netAvg": round(float(cost.get("netAvg") or 0), 6),
        "costSubtracted": True,
        "scale": "1.00=neutral (0 after 1×PositionCost) · 1.10=+1×PositionCost",
    }


def by_symbol(rows: Sequence[Dict[str, Any]], cost_pct: float) -> List[Dict[str, Any]]:
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        buckets.setdefault(r["symbol"] or "?", []).append(r)
    out = []
    for s, items in buckets.items():
        d = drawdown_time_by_symbol([{"t": x["t"], "symbol": s, "pnl": x.get("netPnl", x["pnl"])} for x in items])
        w = pf_window(items, None, cost_pct)
        out.append({
            "symbol": s,
            **w,
            "maxDdS": d.get("maxS"),
            "avgDdS": d.get("avgS"),
            "ddEpisodes": d.get("episodes"),
        })
    out.sort(key=lambda r: r.get("netAfterCost", r.get("net", 0)))
    return out


def by_pack(rows: Sequence[Dict[str, Any]], cost_pct: float) -> Dict[str, Any]:
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        buckets.setdefault(r.get("pack") or "unknown", []).append(r)
    return {k: pf_window(v, None, cost_pct) for k, v in buckets.items()}


IND_KIND_SET = set(IND_KINDS)
STRAT_KEYS = ("indications", "general", "block", "trailing", "dca", "exits")


def _side_of(r: Dict[str, Any]) -> str:
    s = str(r.get("side") or "").upper()
    if s.startswith("L") or s in ("1", "BUY"):
        return "LONG"
    if s.startswith("S") or s in ("-1", "SELL"):
        return "SHORT"
    return ""


def _kind_of(r: Dict[str, Any]) -> str:
    k = str(r.get("ind_kind") or r.get("indKind") or "").strip().lower()
    if k in IND_KIND_SET:
        return k
    reason = str(r.get("reason") or "")
    if reason.startswith("ind:"):
        bits = reason.split(":")
        cand = (bits[1] if len(bits) > 1 else "signals").strip().lower()
        return cand if cand in IND_KIND_SET else "signals"
    return ""


def _strats_of(r: Dict[str, Any]) -> List[str]:
    keys: List[str] = []
    pack = str(r.get("pack") or "").lower()
    if pack in ("indications", "general", "block", "dca"):
        keys.append(pack)
    reason = str(r.get("reason") or "").lower()
    head = reason.split(":")[0].split()[0] if reason else ""
    kind = _kind_of(r)
    if head.startswith("block") or pack == "block":
        keys.append("block")
        if kind == "signals":
            keys.append("block:signals")
    if head.startswith("dca") or pack == "dca":
        keys.append("dca")
    trail = str(r.get("trail_key") or r.get("trailKey") or "")
    if trail and trail not in ("0", "off", "none"):
        keys.append("trailing")
    if any(tok in reason for tok in ("lock", "peak", "rev", "time-exit", "hard", "exit:")) or head in ("sl", "tp", "trail"):
        keys.append("exits")
    if kind:
        keys.append("indications")
        keys.append(f"indications:{kind}")
    elif pack == "indications":
        keys.append("indications")
    return list(dict.fromkeys(keys))


def _with_ddt(window: Dict[str, Any], rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    d = drawdown_time_by_symbol([{"t": x.get("t"), "symbol": x.get("symbol") or "?", "pnl": x.get("netPnl", x.get("pnl"))} for x in rows]) if rows else {"maxS": 0.0, "avgS": 0.0, "episodes": 0}
    window["maxDdS"] = d.get("maxS")
    window["avgDdS"] = d.get("avgS")
    window["ddEpisodes"] = d.get("episodes")
    window["costSubtracted"] = True
    return window


def _bucket_stats(items: Sequence[Dict[str, Any]], cost_pct: float) -> Dict[str, Any]:
    w = pf_window(items, None, cost_pct)
    _with_ddt(w, items)
    by_side: Dict[str, Any] = {}
    for d in ("LONG", "SHORT"):
        sub = [x for x in items if _side_of(x) == d]
        sw = pf_window(sub, None, cost_pct)
        _with_ddt(sw, sub)
        sw["direction"] = d
        by_side[d] = sw
    w["bySide"] = by_side
    w["validated"] = int(w.get("n") or 0) >= 8 and float(w.get("pf") or 0) + 1e-9 >= 1.0
    return w


def by_indication(rows: Sequence[Dict[str, Any]], cost_pct: float) -> Dict[str, Any]:
    buckets: Dict[str, List[Dict[str, Any]]] = {k: [] for k in IND_KINDS}
    for r in rows:
        k = _kind_of(r)
        if k:
            buckets.setdefault(k, []).append(r)
    out: Dict[str, Any] = {}
    for k in IND_KINDS:
        blob = _bucket_stats(buckets.get(k) or [], cost_pct)
        blob["kind"] = k
        out[k] = blob
    return out


def by_direction(rows: Sequence[Dict[str, Any]], cost_pct: float) -> Dict[str, Any]:
    buckets: Dict[str, List[Dict[str, Any]]] = {"LONG": [], "SHORT": []}
    for r in rows:
        s = _side_of(r)
        if s:
            buckets[s].append(r)
    return {
        k: {**_bucket_stats(v, cost_pct), "direction": k}
        for k, v in buckets.items()
    }


def by_strategy(rows: Sequence[Dict[str, Any]], cost_pct: float) -> Dict[str, Any]:
    buckets: Dict[str, List[Dict[str, Any]]] = {k: [] for k in STRAT_KEYS}
    for r in rows:
        for key in _strats_of(r):
            buckets.setdefault(key, []).append(r)
    out: Dict[str, Any] = {}
    for k, v in buckets.items():
        blob = _bucket_stats(v, cost_pct)
        blob["strategy"] = k
        out[k] = blob
    for k in STRAT_KEYS + ("block:signals",):
        out.setdefault(k, {**_bucket_stats([], cost_pct), "strategy": k})
    return out


def merge_kind_stats(
    closed: Sequence[Dict[str, Any]],
    cost_pct: float,
    *,
    gate: Optional[Dict[str, Any]] = None,
    hits: Optional[Dict[str, Any]] = None,
    types: Optional[Dict[str, Any]] = None,
    kind_live: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Closed-tape PF/DDT + intern gate + live scan hits for every indication type."""
    out = by_indication(closed, cost_pct)
    gate = gate or {}
    hits = hits or {}
    types = types or {}
    kind_live = kind_live or {}
    for k in IND_KINDS:
        blob = out.setdefault(k, {**_bucket_stats([], cost_pct), "kind": k})
        g = gate.get(k) if isinstance(gate.get(k), dict) else {}
        live = kind_live.get(k) if isinstance(kind_live.get(k), dict) else {}
        hist_n = int(g.get("n") or 0)
        live_n = int(blob.get("n") or 0)
        if hist_n >= live_n and hist_n:
            blob["pf"] = round(float(g.get("pf") or blob.get("pf") or 0), 4)
            blob["n"] = hist_n
            blob["maxDdS"] = g.get("maxDdS", blob.get("maxDdS"))
            blob["avgDdS"] = g.get("avgDdS", blob.get("avgDdS"))
            blob["ddEpisodes"] = g.get("ddEpisodes", blob.get("ddEpisodes"))
            blob["netAvg"] = g.get("netAvg", blob.get("netAvg"))
            if isinstance(g.get("bySide"), dict) and g.get("bySide"):
                blob["bySide"] = g.get("bySide")
            blob["validated"] = bool(g.get("validated"))
            blob["profitable"] = bool(g.get("profitable"))
        blob["ok"] = g.get("ok") if "ok" in g else bool(blob.get("validated") and float(blob.get("pf") or 0) >= 1.0)
        blob["hits"] = int(live.get("hits") or hits.get(k) or 0)
        blob["scanSymbols"] = int(live.get("symbols") or 0)
        blob["scanLong"] = int(live.get("long") or 0)
        blob["scanShort"] = int(live.get("short") or 0)
        blob["avgConf"] = float(live.get("avgConf") or 0)
        blob["avgStrength"] = float(live.get("avgStrength") or 0)
        blob["enabled"] = bool(types.get(k, live.get("enabled", True)))
        blob["processed"] = True
        blob["kind"] = k
        blob["costSubtracted"] = True
    return out


def merge_strategy_stats(
    closed: Sequence[Dict[str, Any]],
    cost_pct: float,
    *,
    coverage: Optional[Dict[str, Any]] = None,
    block: Optional[Dict[str, Any]] = None,
    dca: Optional[Dict[str, Any]] = None,
    exits: Optional[Dict[str, Any]] = None,
    sets_rows: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    out = by_strategy(closed, cost_pct)
    cov_on = (coverage or {}).get("strategies") or {}
    block = block or {}
    dca = dca or {}
    exits = exits or {}
    pack_pf: Dict[str, List[float]] = {}
    for r in sets_rows or []:
        pack = str(r.get("pack") or "")
        if pack:
            pack_pf.setdefault(pack, []).append(float(r.get("last15Ratio") or 0))
        if str(r.get("kind") or "") == "trail":
            pack_pf.setdefault("trailing", []).append(float(r.get("last15Ratio") or 0))
    extras = {
        "block": {
            "enabled": bool(block.get("enabled", cov_on.get("block", True))),
            "n": int(block.get("countN") or len(block.get("lanes") or []) or (out.get("block") or {}).get("n") or 0),
            "pf": float(block.get("last15Ratio") or (out.get("block") or {}).get("pf") or 0),
        },
        "dca": {
            "enabled": bool(dca.get("enabled", cov_on.get("dca", False))),
            "n": int(dca.get("last15N") or len(dca.get("lanes") or []) or (out.get("dca") or {}).get("n") or 0),
            "pf": float(dca.get("last15Ratio") or (out.get("dca") or {}).get("pf") or 0),
        },
        "trailing": {"enabled": bool(cov_on.get("trailing", True))},
        "exits": {
            "enabled": bool(exits.get("enabled", cov_on.get("exits", True))),
            "n": sum(int(ln.get("n") or 0) for ln in (exits.get("lanes") or []) if isinstance(ln, dict)) or int((out.get("exits") or {}).get("n") or 0),
            "pf": next((float(ln.get("last15Ratio") or 0) for ln in (exits.get("lanes") or []) if isinstance(ln, dict) and ln.get("selected")), float((out.get("exits") or {}).get("pf") or 0)),
        },
        "indications": {"enabled": bool(cov_on.get("indications", True))},
        "general": {"enabled": bool(cov_on.get("general", True))},
    }
    for k in STRAT_KEYS:
        blob = out.setdefault(k, {**_bucket_stats([], cost_pct), "strategy": k})
        extra = extras.get(k) or {}
        blob["enabled"] = bool(extra.get("enabled", cov_on.get(k, k != "dca")))
        if extra.get("pf") and not blob.get("n"):
            blob["pf"] = round(float(extra["pf"]), 4)
        if extra.get("n") and not blob.get("n"):
            blob["n"] = int(extra["n"])
        if pack_pf.get(k) and not blob.get("n"):
            blob["pf"] = round(sum(pack_pf[k]) / len(pack_pf[k]), 4)
        blob["strategy"] = k
        blob["costSubtracted"] = True
        blob["processed"] = True
    return out


def by_reason(rows: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    c: Dict[str, int] = {}
    for r in rows:
        k = str(r.get("reason") or "x").split()[0][:24]
        c[k] = c.get(k, 0) + 1
    return c


def occupancy(open_pos: Sequence[Any]) -> Dict[str, Any]:
    keys = []
    seen = set()
    dup = 0
    for p in open_pos:
        if isinstance(p, dict):
            sym, side = str(p.get("symbol") or ""), str(p.get("side") or "")
            pack = str(p.get("pack") or "")
            sid = str(p.get("setId") or p.get("set_id") or "")
        else:
            sym, side = str(getattr(p, "symbol", "")), str(getattr(p, "side", ""))
            pack = str(getattr(p, "pack", ""))
            sid = str(getattr(p, "set_id", ""))
        key = f"{sym}|{side}|{pack}|{sid}"
        keys.append(key)
        if key in seen:
            dup += 1
        seen.add(key)
    return {
        "open": len(keys),
        "uniqueSlots": len(seen),
        "duplicateSlots": dup,
        "maxOnePerSymbolDirSet": dup == 0,
        "slots": keys,
    }


def build(st: Dict[str, Any], *, cost_pct: float = POSITION_COST_PCT_DEFAULT, conn: str = "") -> Dict[str, Any]:
    cost_pct = float(cost_pct or POSITION_COST_PCT_DEFAULT)
    closed = [enrich(_row(c), cost_pct) for c in (st.get("closed") or [])]
    sets = st.get("sets") or {}
    exits = st.get("exits") or {}
    pc = last_n_cost_pf(closed, int((st.get("pfCost") or {}).get("n") or 15), cost_pct) if closed else last_n_cost_pf([], 15, cost_pct)
    windows = evaluation_windows(closed, cost_pct)
    ddt = drawdown_time_by_symbol([{"t": r["t"], "symbol": r.get("symbol") or "?", "pnl": r.get("netPnl", r["pnl"])} for r in closed])
    ddt_gross = drawdown_time_by_symbol([{"t": r["t"], "symbol": r.get("symbol") or "?", "pnl": r["pnl"]} for r in closed])
    occ = occupancy(st.get("open") or [])
    rows = []
    for r in sets.get("rows") or []:
        intern = r.get("intern") or {}
        rows.append({
            "id": r.get("id"),
            "parentSetId": r.get("parentSetId") or r.get("id"),
            "stage": r.get("stage") or "Unqualified",
            "stageQualified": r.get("stageQualified") or "",
            "stageLedger": r.get("stageLedger") or {},
            "basePf": r.get("basePf"),
            "mainPf": r.get("mainPf"),
            "realPf": r.get("realPf"),
            "axisKey": r.get("axisKey") or "",
            "relativeCount": r.get("relativeCount") or 1,
            "volumeRatio": r.get("volumeRatio") or 1.0,
            "indicationKind": r.get("indicationKind") or "",
            "strategyAdjustments": r.get("strategyAdjustments") or {},
            "pack": r.get("pack"),
            "slRatio": r.get("slRatio"),
            "step": r.get("step"),
            "trailKey": r.get("trailKey"),
            "tpPct": r.get("tpPct"),
            "n": r.get("n"),
            "liveN": r.get("liveN"),
            "histN": r.get("histN"),
            "wins": r.get("wins"),
            "wr": r.get("wr"),
            "expectancyNetCost": r.get("expectancy"),
            "avgHoldS": r.get("avgHoldS"),
            "classicPf": r.get("classicPf"),
            "last15Ratio": r.get("last15Ratio"),
            "evaluationWindows": r.get("evaluationWindows") or {},
            "last15Classic": r.get("last15Classic"),
            "last15R": r.get("last15R"),
            "last25AvgR": r.get("last25AvgR"),
            "last25AvgPnl": r.get("last25AvgPnl"),
            "maxDdS": r.get("maxDdS"),
            "avgDdS": r.get("avgDdS"),
            "ddEpisodes": r.get("ddEpisodes"),
            "gp": r.get("gp"),
            "gl": r.get("gl"),
            "exits": r.get("exits"),
            "intern": intern,
            "active": r.get("active"),
            "deactReason": r.get("deactReason"),
            "locked": r.get("locked"),
            "costSubtracted": True,
            "bySide": r.get("bySide"),
            "live": r.get("live") or {},
            "source": r.get("source") or ("live-exchange" if int(r.get("liveN") or 0) else "hist-sim"),
        })
    blob: Dict[str, Any] = {
    "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "connection": conn or st.get("connection") or "",
    "mode": st.get("mode"),
    "unit": st.get("unit"),
    "running": bool(st.get("running")),
    "halted": bool(st.get("halted")),
    "paused": bool(st.get("paused")),
    "haltReason": st.get("haltReason"),
    "historic": st.get("historic") or {},

        "equity": st.get("equity"),
        "startEquity": st.get("startEquity"),
        "available": st.get("available"),
        "sessionPnl": st.get("sessionPnl"),
        "unrealized": st.get("unrealized"),
        "realizedPnl": st.get("realizedPnl"),
        "wins": st.get("wins"),
        "losses": st.get("losses"),
        "winRate": st.get("winRate"),
        "openCount": st.get("openCount"),
        "open": st.get("open") or [],
        "occupancy": occ,
        "cycle": st.get("cycle"),
        "scanMs": st.get("scanMs"),
        "rssMb": st.get("rssMb"),
        "uptimeS": st.get("uptimeS"),
        "symbols": st.get("symbols") or [],
        "symbolCount": st.get("symbolCount") or len(st.get("symbols") or []),
        "leverage": st.get("leverage"),
        "leverageMap": st.get("leverageMap"),
        "leverageMax": st.get("leverageMax"),
        "useMaxLeverage": True,
        "costAccounting": {
            "positionCostPct": cost_pct,
            "costFrac": cost_as_frac(cost_pct),
            "rule": "1.00 PF = net 0 after 1× PositionCost; 1.10 = +1× PositionCost. All intern PF/R/E deduct cost once from gross price-move.",
            "last15": pc,
            "evaluationWindows": windows,
            "pass": bool(pc.get("count", 0) < 8 or float(pc.get("ratio") or 1) + 1e-9 >= float((st.get("pfCost") or {}).get("minPf") or 1.1)),
            "minPf": (st.get("pfCost") or {}).get("minPf"),
        },
        "profitFactor": {
            "last5": pf_window(closed, 5, cost_pct),
            "last15": pf_window(closed, 15, cost_pct),
            "last25": pf_window(closed, 25, cost_pct),
            "last50": pf_window(closed, 50, cost_pct),
            "last75": pf_window(closed, 75, cost_pct),
            "evaluationWindows": windows,
            "all": pf_window(closed, None, cost_pct),
        },
        "drawdownTime": {
            "afterCost": {"maxDdS": ddt.get("maxS"), "avgDdS": ddt.get("avgS"), "episodes": ddt.get("episodes"), "maxDepth": ddt.get("maxDepth"), "currentS": ddt.get("currentS")},
            "gross": {"maxDdS": ddt_gross.get("maxS"), "avgDdS": ddt_gross.get("avgS"), "episodes": ddt_gross.get("episodes")},
        },
        "bySymbol": by_symbol(closed, cost_pct),
        "byPack": by_pack(closed, cost_pct),
        "byIndication": merge_kind_stats(
            closed,
            cost_pct,
            gate=(sets.get("indGate") or (st.get("coverage") or {}).get("indicationGate") or {}),
            hits=(st.get("coverage") or {}).get("indicationHits") or (st.get("indications") or {}).get("typeHits") or {},
            types=(st.get("coverage") or {}).get("indicationTypes") or (st.get("indications") or {}).get("types") or {},
            kind_live=(st.get("indications") or {}).get("kindStats") or {},
        ),
        "byDirection": by_direction(closed, cost_pct),
        "byStrategy": merge_strategy_stats(
            closed,
            cost_pct,
            coverage=st.get("coverage") or {},
            block=st.get("block") or {},
            dca=st.get("dca") or {},
            exits=exits,
            sets_rows=rows,
        ),
        "byReason": by_reason(closed),
        "block": st.get("block"),
        "coordGate": (st.get("coord") or {}).get("gate"),
        "minStep": (st.get("coord") or {}).get("minStep"),
        "setMinStep": sets.get("minStep"),
        "setStepMax": sets.get("stepMax"),
        "setCount": sets.get("setCount"),
        "setActive": sets.get("activeCount"),
        "setValidated": sets.get("validatedCount"),
        "stageLineage": {
            "stageDefaults": sets.get("stageDefaults") or {},
            "stageCounts": sets.get("stageCounts") or {},
            "stageParentCounts": sets.get("stageParentCounts") or {},
            "qualifiedParentIds": sets.get("qualifiedParentIds") or {},
            "costSubtracted": True,
        },
        "axis": {
            "counts": sets.get("axisCounts") or {},
            "volumeRatio": sets.get("axisVolumeRatio") or 0.01,
            "closedOnly": True,
        },
        "histFills": sets.get("histFills"),
        "liveFills": sets.get("liveFills"),
        "liveProcessed": sets.get("liveProcessed"),
        "liveActive": sets.get("liveActive"),
        "liveOverview": sets.get("liveOverview") or {},
        "setsProgress": sets.get("progress"),
        "sets": rows,
        "internBest": [
            {
                "id": r.get("id"),
                "pack": r.get("pack"),
                "last15Ratio": (r.get("live") or {}).get("last15Ratio") if int(r.get("liveN") or 0) else r.get("last15Ratio"),
                "last25AvgR": r.get("last25AvgR"),
                "maxDdS": (r.get("live") or {}).get("maxDdS") if int(r.get("liveN") or 0) else r.get("maxDdS"),
                "n": r.get("n"),
                "liveN": r.get("liveN"),
                "netAvg": (r.get("live") or {}).get("netAvg") if int(r.get("liveN") or 0) else r.get("expectancyNetCost"),
                "active": r.get("active"),
                "deactReason": r.get("deactReason"),
                "source": r.get("source"),
                "costSubtracted": True,
            }
            for r in sorted(
                rows,
                key=lambda x: (
                    0 if int(x.get("liveN") or 0) else 1,
                    -float(((x.get("live") or {}).get("last15Ratio") if int(x.get("liveN") or 0) else x.get("last15Ratio")) or 0),
                    float(x.get("maxDdS") or 0),
                ),
            )[:12]
        ],
        "exits": exits.get("lanes"),
        "exitRevOn": exits.get("revOn"),
        "dca": st.get("dca"),
        "indications": st.get("indications"),
        "closed": closed[-80:],
        "closedN": len(closed),
        "tests": st.get("tests") or [],
        "engine": st.get("engine"),
        "api": st.get("api"),
        "activity": st.get("activity") or (st.get("coverage") or {}).get("activity") or {},
        "events": st.get("events") or (st.get("coverage") or {}).get("events") or [],
        "coverage": {
            "px": st.get("klinesReady"),
            "klinesTf": st.get("klinesTf"),
            "wsOk": (st.get("api") or {}).get("wsOk"),
            "wsAgeMs": (st.get("api") or {}).get("wsAgeMs"),
            "controlsMissing": sum(1 for p in (st.get("open") or []) if isinstance(p, dict) and not p.get("controls")),
            "qaPass": (st.get("engine") or {}).get("qaPass"),
            "qaFail": (st.get("engine") or {}).get("qaFail"),
            **(st.get("coverage") or {}),
        },
        "defaults": {
            "setMinStep": 3,
            "setStepMax": 22,
            "minStep": 3,
            "exitRevOn": False,
            "exitMinHoldS": 45,
            "useMaxLeverage": True,
            "maxHoldS": 21600,
            "maxOnePerSymbolDirSet": True,
            "positionCostPct": cost_pct,
        },
    }
    return blob


def render_html(blob: Dict[str, Any]) -> str:
    """Render the canonical stats blob as a self-contained, human-readable report."""

    def number(value: Any, digits: int = 2) -> str:
        if value is None:
            return "—"
        if isinstance(value, bool):
            return "yes" if value else "no"
        try:
            value = float(value)
        except (TypeError, ValueError):
            return escape(str(value))
        if value != value:
            return "—"
        if value == float("inf"):
            return "∞"
        if value == float("-inf"):
            return "−∞"
        return f"{value:,.{digits}f}"

    def signed(value: Any, digits: int = 4) -> str:
        try:
            value_num = float(value)
        except (TypeError, ValueError):
            return number(value, digits)
        return ("+" if value_num >= 0 else "") + number(value_num, digits)

    def text(value: Any, fallback: str = "—") -> str:
        return escape(str(value if value not in (None, "") else fallback))

    def cell(value: Any, class_name: str = "") -> str:
        cls = f' class="{escape(class_name, quote=True)}"' if class_name else ""
        return f"<td{cls}>{value}</td>"

    def metric_class(value: Any) -> str:
        try:
            return "positive" if float(value) >= 0 else "negative"
        except (TypeError, ValueError):
            return ""

    def pf_class(value: Any, count: Any) -> str:
        try:
            if int(count or 0) < 1:
                return "muted"
            return "positive" if float(value or 0) >= 1.1 else "negative" if float(value or 0) < 1 else ""
        except (TypeError, ValueError):
            return "muted"

    def timestamp(value: Any) -> str:
        try:
            value = float(value)
            return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(value)) if value > 0 else "—"
        except (TypeError, ValueError, OverflowError):
            return "—"

    def table(headers: List[str], rows: List[List[str]], empty: str = "No data available") -> str:
        head = "".join(f'<th scope="col">{escape(header)}</th>' for header in headers)
        if rows:
            body = "".join("<tr>" + "".join(row) + "</tr>" for row in rows)
        else:
            body = f'<tr><td class="empty" colspan="{len(headers)}">{escape(empty)}</td></tr>'
        return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'

    def json_text(value: Any) -> str:
        try:
            return escape(json.dumps(value, indent=2, ensure_ascii=False, default=str))
        except Exception:
            return escape(str(value))

    connection = text(blob.get("connection"), "unknown")
    unit = text(blob.get("unit"), "—")
    mode = text(blob.get("mode"), "—")
    generated = text(blob.get("generatedAt"), "—")
    running = bool(blob.get("running", False))
    status = "RUNNING" if running else "STOPPED"
    status_class = "positive" if running else "muted"
    cost = (blob.get("costAccounting") or {}).get("last15") or {}
    windows = blob.get("profitFactor") or {}
    last15 = windows.get("last15") or cost
    ddt = (blob.get("drawdownTime") or {}).get("afterCost") or {}
    coverage = blob.get("coverage") or {}
    sets_coverage = coverage.get("sets") or {}
    historic = blob.get("historic") or {}
    historic_coverage = historic.get("coverage") or {}
    historic_symbols = historic_coverage.get("symbols") or {}
    historic_bars = historic_coverage.get("bars") or {}
    gate = blob.get("coordGate") or {}
    set_rows = [row for row in (blob.get("sets") or []) if isinstance(row, dict)]
    set_rows.sort(key=lambda row: (-float(row.get("last15Ratio") or 0), float(row.get("maxDdS") or 0)))
    symbols = blob.get("symbols") or []
    closed = [row for row in (blob.get("closed") or []) if isinstance(row, dict)]
    open_rows = [row for row in (blob.get("open") or []) if isinstance(row, dict)]

    overview_cards = "".join(
        [
            f'<div class="card"><span class="label">Equity</span><strong>{number(blob.get("equity"), 4)}</strong><small>{unit} · start {number(blob.get("startEquity"), 4)}</small></div>',
            f'<div class="card"><span class="label">Session PnL</span><strong class="{metric_class(blob.get("sessionPnl"))}">{signed(blob.get("sessionPnl"), 4)}</strong><small>{number(blob.get("wins"), 0)}W / {number(blob.get("losses"), 0)}L · {number(blob.get("winRate"), 1)}% WR</small></div>',
            f'<div class="card"><span class="label">Last 15 cost PF</span><strong class="{pf_class(last15.get("ratio"), last15.get("count"))}">{number(last15.get("ratio"), 2)}</strong><small>n {number(last15.get("count"), 0)} · min {number((blob.get("costAccounting") or {}).get("minPf"), 2)}</small></div>',
            f'<div class="card"><span class="label">Drawdown time</span><strong>{number(float(ddt.get("maxDdS") or 0) / 3600, 2)} h</strong><small>{number(ddt.get("episodes"), 0)} episodes · avg {number(float(ddt.get("avgDdS") or 0) / 3600, 2)} h</small></div>',
            f'<div class="card"><span class="label">Set coverage</span><strong>{number(sets_coverage.get("validatedCount", blob.get("setValidated")), 0)} / {number(sets_coverage.get("setCount", blob.get("setCount")), 0)}</strong><small>{number(sets_coverage.get("activeCount", blob.get("setActive")), 0)} active · {number(blob.get("histFills"), 0)} historic fills</small></div>',
        ]
    )

    window_rows: List[List[str]] = []
    for name in ("last5", "last15", "last25", "last50", "last75", "all"):
        item = windows.get(name) or {}
        window_rows.append(
            [
                cell(text(name)),
                cell(number(item.get("n"), 0)),
                cell(number(item.get("wins"), 0)),
                cell(number(item.get("losses"), 0)),
                cell(number(item.get("pfAfterCost", item.get("pf")), 2), pf_class(item.get("pfAfterCost", item.get("pf")), item.get("n"))),
                cell(number(item.get("classicPf"), 2)),
                cell(signed(item.get("netAfterCost", item.get("net")), 4), metric_class(item.get("netAfterCost", item.get("net")))),
                cell(number(item.get("avgHoldS"), 0)),
            ]
        )

    symbol_rows: List[List[str]] = []
    for item in sorted((row for row in (blob.get("bySymbol") or []) if isinstance(row, dict)), key=lambda row: -float(row.get("netAfterCost", row.get("net", 0)) or 0)):
        symbol_rows.append(
            [
                cell(text(item.get("symbol"))),
                cell(number(item.get("n"), 0)),
                cell(number(item.get("wins"), 0)),
                cell(number(item.get("losses"), 0)),
                cell(number(item.get("pfAfterCost", item.get("pf")), 2), pf_class(item.get("pfAfterCost", item.get("pf")), item.get("n"))),
                cell(signed(item.get("netAfterCost", item.get("net")), 4), metric_class(item.get("netAfterCost", item.get("net")))),
                cell(number(float(item.get("maxDdS") or 0) / 3600, 2)),
            ]
        )

    indication_rows: List[List[str]] = []
    for kind, item in sorted((blob.get("byIndication") or {}).items()):
        if not isinstance(item, dict):
            continue
        indication_rows.append(
            [
                cell(text(kind)),
                cell(number(item.get("n"), 0)),
                cell(number(item.get("wins"), 0)),
                cell(number(item.get("losses"), 0)),
                cell(number(item.get("pfAfterCost", item.get("pf")), 2), pf_class(item.get("pfAfterCost", item.get("pf")), item.get("n"))),
                cell(number(item.get("wr"), 1) + "%"),
                cell(number(float(item.get("maxDdS") or 0) / 3600, 2)),
                cell("pass" if item.get("validated") and item.get("profitable") else "review" if item.get("n") else "cold", "positive" if item.get("validated") and item.get("profitable") else "muted"),
            ]
        )

    strategy_rows: List[List[str]] = []
    for strategy, item in sorted((blob.get("byStrategy") or {}).items()):
        if not isinstance(item, dict):
            continue
        strategy_rows.append(
            [
                cell(text(strategy)),
                cell("on" if item.get("enabled") else "off", "positive" if item.get("enabled") else "muted"),
                cell(number(item.get("n"), 0)),
                cell(number(item.get("pfAfterCost", item.get("pf")), 2), pf_class(item.get("pfAfterCost", item.get("pf")), item.get("n"))),
                cell(number(item.get("wr"), 1) + "%"),
                cell(number(float(item.get("maxDdS") or 0) / 3600, 2)),
            ]
        )

    pack_rows: List[List[str]] = []
    for pack, item in sorted((blob.get("byPack") or {}).items()):
        if not isinstance(item, dict):
            continue
        pack_rows.append(
            [
                cell(text(pack)),
                cell(number(item.get("n"), 0)),
                cell(number(item.get("pfAfterCost", item.get("pf")), 2), pf_class(item.get("pfAfterCost", item.get("pf")), item.get("n"))),
                cell(signed(item.get("netAfterCost", item.get("net")), 4), metric_class(item.get("netAfterCost", item.get("net")))),
                cell(number(item.get("wr"), 1) + "%"),
            ]
        )

    set_html_rows: List[List[str]] = []
    for item in set_rows[:60]:
        set_html_rows.append(
            [
                cell(text(item.get("id"))),
                cell(text(item.get("pack"))),
                cell(text(item.get("indicationKind"))),
                cell(number(item.get("n"), 0)),
                cell(number(item.get("last15Ratio"), 2), pf_class(item.get("last15Ratio"), item.get("n"))),
                cell(number(item.get("last25AvgR"), 2), metric_class(item.get("last25AvgR"))),
                cell(number(item.get("expectancyNetCost"), 4), metric_class(item.get("expectancyNetCost"))),
                cell(number(float(item.get("maxDdS") or 0) / 3600, 2)),
                cell("on" if item.get("active") else text(item.get("deactReason"), "off"), "positive" if item.get("active") else "muted"),
            ]
        )

    open_html_rows: List[List[str]] = []
    for item in open_rows[:60]:
        open_html_rows.append(
            [
                cell(text(item.get("symbol"))),
                cell(text(item.get("side"))),
                cell(number(item.get("qty"), 4)),
                cell(number(item.get("entry"), 6)),
                cell(number(item.get("px"), 6)),
                cell(signed(item.get("uPnlPct"), 3) + "%", metric_class(item.get("uPnlPct"))),
                cell(text(item.get("pack"))),
                cell("protected" if item.get("controls") else "missing", "positive" if item.get("controls") else "muted"),
            ]
        )

    close_html_rows: List[List[str]] = []
    for item in reversed(closed[-40:]):
        close_html_rows.append(
            [
                cell(timestamp(item.get("t"))),
                cell(text(item.get("symbol"))),
                cell(text(item.get("side"))),
                cell(number(item.get("qty"), 4)),
                cell(number(item.get("entry"), 6)),
                cell(number(item.get("exit"), 6)),
                cell(signed(item.get("pnl"), 4), metric_class(item.get("pnl"))),
                cell(signed(float(item.get("pnl_pct") or 0) * 100, 3) + "%", metric_class(item.get("pnl_pct"))),
                cell(text(item.get("reason"))),
            ]
        )

    strategies = coverage.get("strategies") or {}
    indication_types = coverage.get("indicationTypes") or {}
    coverage_rows = [
        [cell("Status"), cell(f'<span class="{status_class}">{status}</span>')],
        [cell("Connection"), cell(connection)],
        [cell("Mode"), cell(mode)],
        [cell("Historic phase"), cell(text(historic.get("phase"), "idle"))],
        [cell("Historic symbols"), cell(f"{number(historic_symbols.get('completed', len(historic.get('validSymbols') or [])), 0)} / {number(historic_symbols.get('valid', len(historic.get('selectedSymbols') or [])), 0)} valid · {number(len(historic.get('gappedSymbols') or []), 0)} gapped")],
        [cell("Historic bars"), cell(f"{number(historic_bars.get('completed'), 0)} / {number(historic_bars.get('requested'), 0)} · {number(historic_bars.get('missing'), 0)} missing")],
        [cell("Published watermark"), cell(f"{number(len(historic.get('lastPublishedWatermark') or historic.get('watermark') or {}), 0)} symbols")],
        [cell("Last complete run"), cell(timestamp(historic.get("lastCompleteRun")))],
        [cell("Next complete run"), cell(timestamp(historic.get("nextRunAt")))],
        [cell("Historic tape"), cell("complete" if historic.get("coordinationComplete") else "partial", "positive" if historic.get("coordinationComplete") else "muted")],
        [cell("Symbols"), cell(f"{number(len(symbols), 0)} configured")],
        [cell("Websocket"), cell(number(coverage.get("wsOk"), 0))],
        [cell("Price coverage"), cell(number(coverage.get("px"), 0))],
        [cell("QA pass / fail"), cell(f"{number(coverage.get('qaPass'), 0)} / {number(coverage.get('qaFail'), 0)}")],
        [cell("Control gaps"), cell(number(coverage.get("controlsMissing"), 0))],
        [cell("Coordination gate"), cell("open" if gate.get("allow") else "closed", "positive" if gate.get("allow") else "muted")],
        [cell("Strategies"), cell(", ".join(f"{escape(str(key))}={'on' if value else 'off'}" for key, value in sorted(strategies.items())))],
        [cell("Indication types"), cell(", ".join(f"{escape(str(key))}={'on' if value else 'off'}" for key, value in sorted(indication_types.items())))],
    ]

    metadata = {
        "generatedAt": blob.get("generatedAt"),
        "connection": blob.get("connection"),
        "mode": blob.get("mode"),
        "costAccounting": blob.get("costAccounting"),
        "coverage": blob.get("coverage"),
        "coordGate": blob.get("coordGate"),
        "historic": blob.get("historic"),
        "setsProgress": blob.get("setsProgress"),
    }

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>CTS-G · Pulse results · {connection}</title>
<style>
:root {{
  --bg: #07110e;
  --panel: #0f221c;
  --text: #d9f0e6;
  --muted: #7f9d90;
  --accent: #3dcf8e;
  color-scheme: dark;
  font: 15px/1.5 system-ui, sans-serif;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text); }}
main {{ max-width: 1440px; margin: 0 auto; padding: 28px 20px 64px; }}
header {{ border-bottom: 1px solid color-mix(in srgb, var(--muted) 35%, var(--bg)); padding: 12px 0 24px; }}
h1 {{ max-width: 900px; margin: 8px 0 10px; font-size: clamp(28px, 5vw, 52px); line-height: 1.08; letter-spacing: -.04em; }}
h2 {{ margin: 0 0 14px; font-size: 20px; letter-spacing: -.02em; }}
h3 {{ margin: 20px 0 8px; font-size: 15px; }}
p {{ max-width: 92ch; }}
.label {{ color: var(--muted); font: 11px/1.2 ui-monospace, monospace; letter-spacing: .14em; text-transform: uppercase; }}
.muted {{ color: var(--muted); }}
.positive {{ color: var(--accent); }}
.negative {{ color: var(--muted); }}
.card-grid {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin: 22px 0; }}
.card {{ min-height: 122px; border: 1px solid color-mix(in srgb, var(--muted) 28%, var(--bg)); background: var(--panel); border-radius: 12px; padding: 15px; }}
.card strong {{ display: block; margin-top: 12px; font: 600 clamp(22px, 3vw, 32px)/1 ui-monospace, monospace; letter-spacing: -.04em; }}
.card small {{ display: block; margin-top: 10px; color: var(--muted); font-size: 12px; }}
.panel {{ margin-top: 16px; border: 1px solid color-mix(in srgb, var(--muted) 28%, var(--bg)); background: var(--panel); border-radius: 12px; padding: 18px; }}
.panel-head {{ display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 10px; }}
.table-wrap {{ overflow-x: auto; border: 1px solid color-mix(in srgb, var(--muted) 22%, var(--bg)); border-radius: 8px; }}
table {{ width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }}
th, td {{ padding: 9px 10px; border-bottom: 1px solid color-mix(in srgb, var(--muted) 20%, var(--bg)); text-align: left; white-space: nowrap; vertical-align: top; }}
th {{ position: sticky; top: 0; background: var(--panel); color: var(--muted); font: 11px ui-monospace, monospace; letter-spacing: .08em; text-transform: uppercase; }}
tbody tr:last-child td {{ border-bottom: 0; }}
tbody tr:nth-child(even) {{ background: color-mix(in srgb, var(--muted) 5%, var(--panel)); }}
td {{ font-size: 13px; }}
.empty {{ padding: 24px; color: var(--muted); text-align: center; }}
.two {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }}
.status-line {{ display: flex; flex-wrap: wrap; gap: 8px 16px; color: var(--muted); font: 12px ui-monospace, monospace; }}
.status-line b {{ color: var(--text); font-weight: 500; }}
details {{ border-top: 1px solid color-mix(in srgb, var(--muted) 25%, var(--bg)); margin-top: 16px; padding-top: 12px; }}
summary {{ cursor: pointer; color: var(--muted); }}
pre {{ max-height: 420px; overflow: auto; padding: 14px; background: var(--bg); border-radius: 8px; color: var(--muted); font: 12px/1.5 ui-monospace, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }}
footer {{ padding-top: 20px; color: var(--muted); font-size: 12px; }}
@media (max-width: 980px) {{ .card-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }} }}
@media (max-width: 680px) {{ main {{ padding: 18px 12px 48px; }} .card-grid, .two {{ grid-template-columns: 1fr; }} .panel {{ padding: 14px; }} }}
@media print {{ body {{ background: var(--text); color: var(--bg); }} .panel, .card {{ break-inside: avoid; }} }}
</style>
</head>
<body>
<main>
<header>
  <div class="label">CTS-G · canonical live stats export</div>
  <h1>Pulse results · {connection}</h1>
  <p class="muted">One self-contained HTML report generated from the same canonical stats blob as the JSON and Markdown exports. Generated {generated}.</p>
  <div class="status-line"><span>status <b class="{status_class}">{status}</b></span><span>mode <b>{mode}</b></span><span>unit <b>{unit}</b></span><span>closed <b>{number(blob.get("closedN"), 0)}</b></span><span>open <b>{number(blob.get("openCount"), 0)}</b></span></div>
</header>
<section class="card-grid">{overview_cards}</section>
<section class="panel"><div class="panel-head"><h2>Profit factor windows</h2><span class="muted">PositionCost deducted · neutral 1.00 · +1× cost 1.10</span></div>{table(["Window", "N", "Wins", "Losses", "Cost PF", "Classic PF", "Net", "Avg hold s"], window_rows)}</section>
<section class="two">
  <div class="panel"><h2>By symbol</h2>{table(["Symbol", "N", "Wins", "Losses", "Cost PF", "Net", "Max DDt h"], symbol_rows)}</div>
  <div class="panel"><h2>By indication type</h2>{table(["Type", "N", "Wins", "Losses", "Cost PF", "WR", "Max DDt h", "Gate"], indication_rows)}</div>
</section>
<section class="two">
  <div class="panel"><h2>By strategy</h2>{table(["Strategy", "State", "N", "Cost PF", "WR", "Max DDt h"], strategy_rows)}</div>
  <div class="panel"><h2>By pack</h2>{table(["Pack", "N", "Cost PF", "Net", "WR"], pack_rows)}</div>
</section>
<section class="panel"><div class="panel-head"><h2>Independent Set ranking</h2><span class="muted">top {min(len(set_rows), 60)} of {len(set_rows)} rows · cost-net evidence</span></div>{table(["Set", "Pack", "Kind", "N", "PF15", "R25", "E", "Max DDt h", "State"], set_html_rows)}</section>
<section class="two">
  <div class="panel"><h2>Open book</h2>{table(["Symbol", "Side", "Qty", "Entry", "Mark", "uPnL %", "Pack", "Controls"], open_html_rows)}</div>
  <div class="panel"><h2>Recent closed tape</h2>{table(["Time", "Symbol", "Side", "Qty", "Entry", "Exit", "PnL", "Move %", "Reason"], close_html_rows)}</div>
</section>
<section class="panel"><div class="panel-head"><h2>Coverage and gate</h2><span class="muted">engine health, catalog state, and coordination decision</span></div>{table(["Metric", "Value"], coverage_rows)}{gate.get("reasons") and f'<p class="muted">Gate reasons: {escape(" · ".join(str(reason) for reason in gate.get("reasons") or []))}</p>' or ""}</section>
<section class="panel"><h2>Report lineage</h2><p class="muted">This report is read-only evidence. It does not start the engine, submit orders, alter a gate, or promote a candidate. Re-run the export after the next complete stats snapshot to capture fresh values.</p><details><summary>Show structured metadata</summary><pre>{json_text(metadata)}</pre></details></section>
<footer>CTS-G Pulse · no external libraries, tracking, or remote assets.</footer>
</main>
</body>
</html>"""


def render_md(blob: Dict[str, Any]) -> str:
    pc = (blob.get("costAccounting") or {}).get("last15") or {}
    pf = blob.get("profitFactor") or {}
    ddt = (blob.get("drawdownTime") or {}).get("afterCost") or {}
    sets = blob.get("sets") or []
    occ = blob.get("occupancy") or {}
    lines = [
        f"# Pulse results · {blob.get('connection')}",
        f"Generated {blob.get('generatedAt')}",
        "",
        "## Overview",
        f"- Equity **{blob.get('equity')}** {blob.get('unit')} · session {blob.get('sessionPnl')} · {blob.get('wins')}W/{blob.get('losses')}L ({blob.get('winRate')}%)",
        f"- Symbols **{blob.get('symbolCount')}** · open {blob.get('openCount')} · RSS {blob.get('rssMb')}MB · scan {blob.get('scanMs')}ms · cycle {blob.get('cycle')}",
        f"- Occupancy unique={occ.get('uniqueSlots')} dup={occ.get('duplicateSlots')} max1={occ.get('maxOnePerSymbolDirSet')}",
        "",
        "## Open book",
    ]
    for p in blob.get("open") or []:
        if not isinstance(p, dict):
            continue
        lines.append(
            f"- `{p.get('symbol')}` {p.get('side')} qty={p.get('qty')} u={p.get('uPnlPct')}% age={round(float(p.get('ageS') or 0),0)}s set=`{p.get('setId') or ''}` pack={p.get('pack')} ctrl={p.get('controls')} sl={bool(p.get('slOid'))} tp={bool(p.get('tpOid'))} overall={p.get('overall')}"
        )
    if not blob.get("open"):
        lines.append("- (flat)")
    lines += [
        "",
        "## PositionCost accounting",
        f"- Cost **{(blob.get('costAccounting') or {}).get('positionCostPct')}%** (frac {(blob.get('costAccounting') or {}).get('costFrac')})",
        f"- Last15 cost-PF **{pc.get('ratio')}** avgR {pc.get('avgR')} classic {pc.get('classicPf')} pass={(blob.get('costAccounting') or {}).get('pass')} min={(blob.get('costAccounting') or {}).get('minPf')}",
        f"- Rule: {(blob.get('costAccounting') or {}).get('rule')}",
        "",
        "## Profit factor (USDT, PositionCost deducted)",
    ]
    for name, w in pf.items():
        if not isinstance(w, dict):
            continue
        lines.append(
            f"- {name}: n={w.get('n')} {w.get('wins')}W/{w.get('losses')}L PF={w.get('pf')} net={w.get('net')} · afterCost PF={w.get('pfAfterCost')} net={w.get('netAfterCost')} ratio={w.get('costRatio')} WR={w.get('wr')}%"
        )
    lines += [
        "",
        f"## Drawdown time (after cost) max **{ddt.get('maxDdS')}s** avg **{ddt.get('avgDdS')}s** episodes {ddt.get('episodes')}",
        "",
        "## By pack",
    ]
    for pack, w in (blob.get("byPack") or {}).items():
        lines.append(f"- {pack}: n={w.get('n')} PF={w.get('pf')} afterCost={w.get('pfAfterCost')} net={w.get('netAfterCost')}")
    lines += ["", "## By indication type", ""]
    for kind, w in (blob.get("byIndication") or {}).items():
        lines.append(f"- {kind}: n={w.get('n')} {w.get('wins')}W/{w.get('losses')}L PF={w.get('pf')} afterCost={w.get('pfAfterCost')} net={w.get('netAfterCost')} WR={w.get('wr')}%")
    lines += ["", "## By symbol", ""]
    for r in blob.get("bySymbol") or []:
        lines.append(
            f"- `{r.get('symbol')}` n={r.get('n')} {r.get('wins')}W/{r.get('losses')}L PF={r.get('pf')} afterCost={r.get('pfAfterCost')} net={r.get('netAfterCost')} maxDDt={r.get('maxDdS')}s"
        )
    lines += ["", "## Independent Sets (intern, cost deducted)", ""]
    lines.append(f"active {blob.get('setActive')}/{blob.get('setCount')} · hist fills {blob.get('histFills')} · minStep {blob.get('setMinStep')}-{blob.get('setStepMax')}")
    lov = blob.get("liveOverview") or {}
    lines += [
        "",
        "## Live on-exchange Sets (cost subtracted)",
        f"processed {lov.get('processed') or blob.get('liveProcessed') or 0} · on {lov.get('active') or blob.get('liveActive') or 0} · fills {lov.get('fills') or blob.get('liveFills') or 0} · PF {lov.get('last15Ratio')} net {lov.get('netAvg')} · source live-exchange",
    ]
    for r in (lov.get("rows") or [])[:16]:
        lines.append(
            f"- `{r.get('id')}` live n={r.get('n')} PF={r.get('last15Ratio')} net={r.get('netAvg')} DDt={r.get('maxDdS')}s {'ON' if r.get('active') else 'OFF'} {r.get('deactReason') or ''}"
        )
    lines += ["", "## Set intern ranking", ""]
    for r in sorted(sets, key=lambda x: (-float(x.get("last15Ratio") or 0), float(x.get("maxDdS") or 0)))[:20]:
        lines.append(
            f"- `{r.get('id')}` PF15={r.get('last15Ratio')} R25={r.get('last25AvgR')} WR={r.get('wr')} E={r.get('expectancyNetCost')} hold={r.get('avgHoldS')}s maxDDt={r.get('maxDdS')}s n={r.get('n')}+{r.get('liveN')} on={r.get('active')} {r.get('deactReason') or ''}"
        )
    lines += ["", "## Coverage", ""]
    cov = blob.get("coverage") or {}
    strat = cov.get("strategies") or {}
    types = cov.get("indicationTypes") or {}
    hits = cov.get("indicationHits") or {}
    lines.append("- strategies: " + ", ".join(f"{k}={'ON' if v else 'off'}" for k, v in strat.items()))
    lines.append("- indication types: " + ", ".join(f"{k}={'ON' if types.get(k, True) else 'off'} hits={hits.get(k, 0)}" for k in ("state", "direction", "move", "active", "common", "signals", "trend", "break")))
    bcov = cov.get("block") or {}
    lines.append(f"- block enabled={bcov.get('enabled')} counts={bcov.get('countN')} stack={bcov.get('maxStack')} liveLanes={bcov.get('liveLanes')}")
    for c in bcov.get("allCounts") or []:
        lines.append(f"  n={c.get('n')} inc={c.get('inc')}× add={c.get('targetAdd')} tot={c.get('targetBlock')} minPF={c.get('minPF')}")
    scov = cov.get("sets") or {}
    lines.append(f"- sets valid {scov.get('validatedCount')}/{scov.get('setCount')} · active {scov.get('activeCount')}/{scov.get('setCount')} histFills={scov.get('histFills')} families={scov.get('families')} trailCover={scov.get('trailCover')}")
    cc = cov.get("controls") or {}
    lines.append(f"- controls ok={cc.get('ok')} missing={cc.get('missing')} open={cc.get('open')}")
    lines += ["", "## Block strategy", ""]
    blk = blob.get("block") or {}
    lines.append(f"enabled={blk.get('enabled')} maxStack={blk.get('maxStack')} volRatio={blk.get('volumeRatio')} pfRatio={blk.get('profitFactorRatio')} minPF={blk.get('defaultMinPF')}")
    for lane in blk.get("lanes") or []:
        lines.append(f"- {lane.get('symbol')} {lane.get('side')} base={lane.get('baseQty')} add={lane.get('confirmedAdd')} agg={lane.get('aggregate')}")
        for c in (lane.get("counts") or [])[:8]:
            lines.append(f"  count {c.get('n')} inc={c.get('inc')} minPF={c.get('minPF')} obsPF={c.get('obsPF')} pass={c.get('pass')} paused={c.get('paused')}")
    lines += ["", "## DCA", ""]
    dca = blob.get("dca") or {}
    lines.append(f"enabled={dca.get('enabled')} steps={dca.get('maxSteps')} dist={dca.get('distances')} last15={dca.get('last15Ratio')} active={dca.get('active')}")
    lines += ["", "## Exits", ""]
    for ln in blob.get("exits") or []:
        lines.append(f"- {ln.get('key')} n={ln.get('n')} wins={ln.get('wins')} PF15={ln.get('last15Ratio')} active={ln.get('active')}")
    activity = blob.get("activity") or {}
    lines += ["", "## Activity ledger", ""]
    lines.append(f"events={activity.get('eventCount', 0)} duplicates={activity.get('duplicateCount', 0)} fills={activity.get('fillCount', 0)} requests={activity.get('requestCount', 0)} responses={activity.get('responseCount', 0)} fees={activity.get('fees', 0)} parity={activity.get('parity', 'pending')}")
    for event in (activity.get("tail") or [])[:12]:
        if isinstance(event, dict):
            lines.append(f"- {event.get('event_type')} {event.get('status')} {event.get('symbol') or ''} {event.get('detail') or ''}")
    lines += ["", "## Coverage / QA", ""]
    cov = blob.get("coverage") or {}
    lines.append(f"wsOk={cov.get('wsOk')} px={cov.get('px')} QA {cov.get('qaPass')}P/{cov.get('qaFail')}F controlsMissing={cov.get('controlsMissing')}")
    gate = blob.get("coordGate") or {}
    lines.append(f"gate allow={gate.get('allow')} {gate.get('reasons')}")
    return "\n".join(lines) + "\n"


def write(
    st: Dict[str, Any],
    dest_json: str,
    dest_md: str,
    *,
    cost_pct: float,
    conn: str,
    dest_html: Optional[str] = None,
) -> Dict[str, Any]:
    blob = build(st, cost_pct=cost_pct, conn=conn)
    tmp = dest_json + ".tmp"
    with open(tmp, "w") as f:
        json.dump(blob, f, separators=(",", ":"))
    os.replace(tmp, dest_json)
    with open(dest_md, "w") as f:
        f.write(render_md(blob))
    if dest_html:
        html_tmp = dest_html + ".tmp"
        with open(html_tmp, "w") as f:
            f.write(render_html(blob))
        os.replace(html_tmp, dest_html)
    return blob


def self_test() -> List[Tuple[str, bool, str]]:
    out: List[Tuple[str, bool, str]] = []
    rows = [
        {"t": 1, "symbol": "AAA-USDT", "side": "LONG", "qty": 1, "entry": 100, "pnl": 0.15, "pnl_pct": 0.003, "hold_s": 60, "reason": "tp", "pack": "general", "set_id": "g:st3"},
        {"t": 2, "symbol": "AAA-USDT", "side": "LONG", "qty": 1, "entry": 100, "pnl": -0.15, "pnl_pct": -0.0015, "hold_s": 40, "reason": "sl", "pack": "general", "set_id": "g:st3"},
    ]
    e = [enrich(_row(r), 0.15) for r in rows]
    out.append(("rep-cost-win", abs(e[0]["netPnl"] - 0.15) < 1e-9 and abs(e[0]["resultR"] - 1.0) < 1e-9, str(e[0])))
    out.append(("rep-cost-loss", e[1]["netPnl"] < 0 and e[1]["resultR"] < 0, str(e[1])))
    w = pf_window(e, None, 0.15)
    out.append(("rep-pf-after-cost", "pfAfterCost" in w and "costRatio" in w, str(w)))
    window_rows = [
        {"t": 1, "pnl": 1.0, "netPnl": 1.0},
        {"t": 2, "pnl": -10.0, "netPnl": -10.0},
        {"t": 3, "pnl": 3.0, "netPnl": 3.0},
        {"t": 4, "pnl": 4.0, "netPnl": 4.0},
    ]
    newest_first = list(reversed(window_rows))
    newest = pf_window(newest_first, 2, 0.15)
    out.append(("rep-pf-latest-window", newest["gp"] == 7.0 and newest["gl"] == 0.0, str(newest)))
    legacy = enrich(_row({"t": 7, "symbol": "AAA-USDT", "qty": 100, "entry": 1, "pnl": 1.0}), 0.15)
    out.append(("rep-legacy-pnl-normalized", abs(legacy["netPnl"] - 1.0) < 1e-9 and abs(legacy["resultR"] - 6.6667) < 1e-3, str(legacy)))
    occ = occupancy([{"symbol": "A", "side": "LONG", "pack": "general", "setId": "s1"}, {"symbol": "B", "side": "SHORT", "pack": "indications", "setId": "s2"}])
    out.append(("rep-occ-unique", occ["duplicateSlots"] == 0 and occ["maxOnePerSymbolDirSet"], str(occ)))
    occ2 = occupancy([{"symbol": "A", "side": "LONG", "pack": "g", "setId": "s1"}, {"symbol": "A", "side": "LONG", "pack": "g", "setId": "s1"}])
    out.append(("rep-occ-dup", occ2["duplicateSlots"] == 1, str(occ2)))
    blob = build({"closed": rows, "sets": {"rows": [], "setCount": 0, "activeCount": 0}, "open": [], "pfCost": {"n": 15, "minPf": 1.1}}, cost_pct=0.15, conn="x02")
    md = render_md(blob)
    out.append(("rep-md", "PositionCost" in md and "Independent Sets" in md, md[:80]))
    html_report = render_html(blob)
    out.append(("rep-html", "<!doctype html>" in html_report and "Profit factor windows" in html_report and "metadata" in html_report, html_report[:80]))
    out.append(("rep-blob", blob["costAccounting"]["positionCostPct"] == 0.15 and blob["occupancy"]["maxOnePerSymbolDirSet"], str(blob["costAccounting"]["last15"])))
    out.append(("rep-dir", set((blob.get("byDirection") or {}).keys()) == {"LONG", "SHORT"}, str(blob.get("byDirection"))))
    out.append(("rep-dir-cost", all(bool(v.get("costSubtracted")) for v in (blob.get("byDirection") or {}).values()), str(blob.get("byDirection"))))
    out.append(("rep-strat", "general" in (blob.get("byStrategy") or {}), str(blob.get("byStrategy"))))
    out.append(("rep-netavg", "netAvg" in (blob.get("profitFactor") or {}).get("all", {}), str((blob.get("profitFactor") or {}).get("all"))))
    kinds = blob.get("byIndication") or {}
    out.append(("rep-ind-all-six", set(kinds.keys()) == set(IND_KINDS), str(sorted(kinds))))
    out.append(("rep-ind-ddt", all("maxDdS" in (kinds.get(k) or {}) and "bySide" in (kinds.get(k) or {}) for k in IND_KINDS), str({k: list((kinds.get(k) or {}).keys())[:8] for k in IND_KINDS})))
    mixed = [
        {"t": 3, "symbol": "BBB-USDT", "side": "SHORT", "qty": 1, "entry": 10, "pnl": 0.08, "pnl_pct": 0.002, "hold_s": 30, "reason": "ind:active:tp", "pack": "indications", "ind_kind": "active", "trail_key": "0.3:0.1"},
        {"t": 4, "symbol": "BBB-USDT", "side": "LONG", "qty": 1, "entry": 10, "pnl": -0.04, "pnl_pct": -0.001, "hold_s": 20, "reason": "block:1", "pack": "block"},
        {"t": 5, "symbol": "CCC-USDT", "side": "LONG", "qty": 1, "entry": 8, "pnl": 0.02, "pnl_pct": 0.001, "hold_s": 15, "reason": "dca:2", "pack": "dca"},
        {"t": 6, "symbol": "CCC-USDT", "side": "SHORT", "qty": 1, "entry": 8, "pnl": 0.01, "pnl_pct": 0.0008, "hold_s": 12, "reason": "lock", "pack": "general"},
    ]
    mixed_e = [enrich(_row(r), 0.15) for r in mixed]
    ki = merge_kind_stats(mixed_e, 0.15, hits={"active": 4, "state": 2}, types={k: True for k in IND_KINDS})
    out.append(("rep-ind-kind-field", int((ki.get("active") or {}).get("n") or 0) == 1 and (ki.get("active") or {}).get("hits") == 4, str(ki.get("active"))))
    out.append(("rep-ind-processed", all(bool((ki.get(k) or {}).get("processed")) for k in IND_KINDS), str({k: (ki.get(k) or {}).get("processed") for k in IND_KINDS})))
    stt = merge_strategy_stats(mixed_e, 0.15, coverage={"strategies": {"indications": True, "general": True, "block": True, "dca": False, "trailing": True, "exits": True}})
    out.append(("rep-strat-canonical", all(k in stt for k in STRAT_KEYS), str(sorted(stt))))
    out.append(("rep-strat-block", int((stt.get("block") or {}).get("n") or 0) >= 1, str(stt.get("block"))))
    out.append(("rep-strat-dca-n", int((stt.get("dca") or {}).get("n") or 0) >= 1, str(stt.get("dca"))))
    out.append(("rep-strat-trail", int((stt.get("trailing") or {}).get("n") or 0) >= 1, str(stt.get("trailing"))))
    out.append(("rep-strat-exits", int((stt.get("exits") or {}).get("n") or 0) >= 1, str(stt.get("exits"))))
    out.append(("rep-strat-ind-kind", "indications:active" in stt, str(sorted(stt))))
    cross = build({"closed": [
        {"t": 100, "symbol": "A", "pnl": 1.0},
        {"t": 160, "symbol": "A", "pnl": -2.0},
        {"t": 50_000, "symbol": "B", "pnl": 1.0},
        {"t": 50_060, "symbol": "B", "pnl": -0.2},
        {"t": 50_120, "symbol": "B", "pnl": 1.5},
    ], "sets": {"rows": []}, "open": []}, cost_pct=0.15, conn="x02")
    out.append(("rep-ddt-symbol-isolation", cross["drawdownTime"]["afterCost"]["maxDdS"] == 60.0, str(cross["drawdownTime"])))
    return out


if __name__ == "__main__":
    failed = 0
    for name, ok, detail in self_test():
        print(("PASS" if ok else "FAIL"), name, detail[:160])
        if not ok:
            failed += 1
    raise SystemExit(failed)
