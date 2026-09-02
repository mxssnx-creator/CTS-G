#!/usr/bin/env python3
"""Full Pulse results export: PF / DDT / PositionCost-net, per Set intern, Block, DCA."""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from position_cost import (
    POSITION_COST_PCT_DEFAULT,
    cost_as_frac,
    last_n_cost_pf,
    net_pnl_pct,
    net_pnl_usdt,
    signed_result_r,
)
from set_engine import drawdown_time


def _f(v: Any, fb: float = 0.0) -> float:
    try:
        n = float(v)
    except Exception:
        return fb
    return n if n == n and abs(n) != float("inf") else fb


def _row(c: Any) -> Dict[str, Any]:
    if isinstance(c, dict):
        return {
            "t": _f(c.get("t")),
            "symbol": str(c.get("symbol") or ""),
            "side": str(c.get("side") or ""),
            "qty": _f(c.get("qty")),
            "entry": _f(c.get("entry")),
            "exit": _f(c.get("exit") or c.get("exit_px")),
            "pnl": _f(c.get("pnl")),
            "pnl_pct": _f(c.get("pnl_pct")),
            "hold_s": _f(c.get("hold_s") or c.get("holdS")),
            "reason": str(c.get("reason") or ""),
            "set_id": str(c.get("set_id") or c.get("setId") or ""),
            "pack": str(c.get("pack") or ""),
            "client_id": str(c.get("client_id") or c.get("clientId") or ""),
            "sl_ratio": _f(c.get("sl_ratio") or c.get("slRatio")),
            "trail_key": str(c.get("trail_key") or c.get("trailKey") or ""),
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
        "hold_s": _f(getattr(c, "hold_s", 0)),
        "reason": str(getattr(c, "reason", "") or ""),
        "set_id": str(getattr(c, "set_id", "") or ""),
        "pack": str(getattr(c, "pack", "") or ""),
        "client_id": str(getattr(c, "client_id", "") or ""),
        "sl_ratio": _f(getattr(c, "sl_ratio", 0)),
        "trail_key": str(getattr(c, "trail_key", "") or ""),
    }


def enrich(row: Dict[str, Any], cost_pct: float) -> Dict[str, Any]:
    notion = max(0.0, row["qty"] * row["entry"])
    gross_pct = row["pnl_pct"]
    net_pct = net_pnl_pct(gross_pct, cost_pct)
    net_usdt = net_pnl_usdt(gross_pct, row["qty"], row["entry"], cost_pct) if notion else row["pnl"]
    r = signed_result_r(gross_pct, cost_pct)
    row["notional"] = round(notion, 6)
    row["grossPnlPct"] = round(gross_pct, 8)
    row["netPnlPct"] = round(net_pct, 8)
    row["netPnl"] = round(net_usdt, 8)
    row["resultR"] = round(r, 4)
    row["costPct"] = cost_pct
    row["costUsdt"] = round(notion * cost_as_frac(cost_pct), 8)
    return row


def pf_window(rows: Sequence[Dict[str, Any]], n: Optional[int], cost_pct: float) -> Dict[str, Any]:
    src = list(rows)[-n:] if n else list(rows)
    gp = gl = 0.0
    gp_net = gl_net = 0.0
    wins = losses = 0
    holds: List[float] = []
    for r in src:
        pnl = _f(r.get("pnl"))
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
        d = drawdown_time([{"t": x["t"], "pnl": x.get("netPnl", x["pnl"])} for x in items])
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


def by_indication(rows: Sequence[Dict[str, Any]], cost_pct: float) -> Dict[str, Any]:
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        reason = str(r.get("reason") or "")
        kind = "other"
        if reason.startswith("ind:"):
            bits = reason.split(":")
            kind = bits[1] if len(bits) > 1 else "signals"
        elif str(r.get("pack") or "") == "indications":
            kind = "indications"
        elif str(r.get("pack") or "") == "general":
            kind = "general"
        buckets.setdefault(kind, []).append(r)
    return {k: pf_window(v, None, cost_pct) for k, v in buckets.items()}


def by_direction(rows: Sequence[Dict[str, Any]], cost_pct: float) -> Dict[str, Any]:
    buckets: Dict[str, List[Dict[str, Any]]] = {"LONG": [], "SHORT": []}
    for r in rows:
        s = str(r.get("side") or "").upper()
        if s.startswith("L") or s in ("1", "BUY"):
            buckets["LONG"].append(r)
        elif s.startswith("S") or s in ("-1", "SELL"):
            buckets["SHORT"].append(r)
    return {
        k: {**pf_window(v, None, cost_pct), "direction": k, "costSubtracted": True}
        for k, v in buckets.items()
    }


def by_strategy(rows: Sequence[Dict[str, Any]], cost_pct: float) -> Dict[str, Any]:
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        pack = str(r.get("pack") or "unknown")
        buckets.setdefault(pack, []).append(r)
        reason = str(r.get("reason") or "")
        if reason.startswith("ind:"):
            bits = reason.split(":")
            kind = bits[1] if len(bits) > 1 else "signals"
            buckets.setdefault(f"indications:{kind}", []).append(r)
    return {
        k: {**pf_window(v, None, cost_pct), "strategy": k, "costSubtracted": True}
        for k, v in buckets.items()
    }


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
    ddt = drawdown_time([{"t": r["t"], "pnl": r.get("netPnl", r["pnl"])} for r in closed])
    ddt_gross = drawdown_time([{"t": r["t"], "pnl": r["pnl"]} for r in closed])
    occ = occupancy(st.get("open") or [])
    rows = []
    for r in sets.get("rows") or []:
        intern = r.get("intern") or {}
        rows.append({
            "id": r.get("id"),
            "pack": r.get("pack"),
            "slRatio": r.get("slRatio"),
            "step": r.get("step"),
            "trailKey": r.get("trailKey"),
            "tpPct": r.get("tpPct"),
            "n": r.get("n"),
            "liveN": r.get("liveN"),
            "wins": r.get("wins"),
            "wr": r.get("wr"),
            "expectancyNetCost": r.get("expectancy"),
            "avgHoldS": r.get("avgHoldS"),
            "classicPf": r.get("classicPf"),
            "last15Ratio": r.get("last15Ratio"),
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
        })
    blob: Dict[str, Any] = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "connection": conn or st.get("connection") or "",
        "mode": st.get("mode"),
        "unit": st.get("unit"),
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
            "pass": bool(pc.get("count", 0) < 8 or float(pc.get("ratio") or 1) + 1e-9 >= float((st.get("pfCost") or {}).get("minPf") or 1.1)),
            "minPf": (st.get("pfCost") or {}).get("minPf"),
        },
        "profitFactor": {
            "last5": pf_window(closed, 5, cost_pct),
            "last15": pf_window(closed, 15, cost_pct),
            "last25": pf_window(closed, 25, cost_pct),
            "all": pf_window(closed, None, cost_pct),
        },
        "drawdownTime": {
            "afterCost": {"maxDdS": ddt.get("maxS"), "avgDdS": ddt.get("avgS"), "episodes": ddt.get("episodes"), "maxDepth": ddt.get("maxDepth"), "currentS": ddt.get("currentS")},
            "gross": {"maxDdS": ddt_gross.get("maxS"), "avgDdS": ddt_gross.get("avgS"), "episodes": ddt_gross.get("episodes")},
        },
        "bySymbol": by_symbol(closed, cost_pct),
        "byPack": by_pack(closed, cost_pct),
        "byIndication": by_indication(closed, cost_pct),
        "byDirection": by_direction(closed, cost_pct),
        "byStrategy": by_strategy(closed, cost_pct),
        "byReason": by_reason(closed),
        "block": st.get("block"),
        "coordGate": (st.get("coord") or {}).get("gate"),
        "minStep": (st.get("coord") or {}).get("minStep"),
        "setMinStep": sets.get("minStep"),
        "setStepMax": sets.get("stepMax"),
        "setCount": sets.get("setCount"),
        "setActive": sets.get("activeCount"),
        "histFills": sets.get("histFills"),
        "setsProgress": sets.get("progress"),
        "sets": rows,
        "internBest": [
            {
                "id": r.get("id"),
                "pack": r.get("pack"),
                "last15Ratio": r.get("last15Ratio"),
                "last25AvgR": r.get("last25AvgR"),
                "maxDdS": r.get("maxDdS"),
                "n": r.get("n"),
                "liveN": r.get("liveN"),
                "active": r.get("active"),
                "deactReason": r.get("deactReason"),
            }
            for r in sorted(rows, key=lambda x: (-float(x.get("last15Ratio") or 0), float(x.get("maxDdS") or 0)))[:12]
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
            "setMinStep": 8,
            "setStepMax": 12,
            "minStep": 6,
            "exitRevOn": False,
            "exitMinHoldS": 45,
            "useMaxLeverage": True,
            "maxHoldS": 21600,
            "maxOnePerSymbolDirSet": True,
            "positionCostPct": cost_pct,
        },
    }
    return blob


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
    lines.append("- indication types: " + ", ".join(f"{k}={'ON' if types.get(k, True) else 'off'} hits={hits.get(k, 0)}" for k in ("state", "direction", "move", "active", "common", "signals")))
    bcov = cov.get("block") or {}
    lines.append(f"- block enabled={bcov.get('enabled')} counts={bcov.get('countN')} stack={bcov.get('maxStack')} liveLanes={bcov.get('liveLanes')}")
    for c in bcov.get("allCounts") or []:
        lines.append(f"  n={c.get('n')} inc={c.get('inc')}× add={c.get('targetAdd')} tot={c.get('targetBlock')} minPF={c.get('minPF')}")
    scov = cov.get("sets") or {}
    lines.append(f"- sets {scov.get('activeCount')}/{scov.get('setCount')} histFills={scov.get('histFills')} families={scov.get('families')} trailCover={scov.get('trailCover')}")
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
    lines += ["", "## Coverage / QA", ""]
    cov = blob.get("coverage") or {}
    lines.append(f"wsOk={cov.get('wsOk')} px={cov.get('px')} QA {cov.get('qaPass')}P/{cov.get('qaFail')}F controlsMissing={cov.get('controlsMissing')}")
    gate = blob.get("coordGate") or {}
    lines.append(f"gate allow={gate.get('allow')} {gate.get('reasons')}")
    return "\n".join(lines) + "\n"


def write(st: Dict[str, Any], dest_json: str, dest_md: str, *, cost_pct: float, conn: str) -> Dict[str, Any]:
    blob = build(st, cost_pct=cost_pct, conn=conn)
    tmp = dest_json + ".tmp"
    with open(tmp, "w") as f:
        json.dump(blob, f, separators=(",", ":"))
    os.replace(tmp, dest_json)
    with open(dest_md, "w") as f:
        f.write(render_md(blob))
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
    occ = occupancy([{"symbol": "A", "side": "LONG", "pack": "general", "setId": "s1"}, {"symbol": "B", "side": "SHORT", "pack": "indications", "setId": "s2"}])
    out.append(("rep-occ-unique", occ["duplicateSlots"] == 0 and occ["maxOnePerSymbolDirSet"], str(occ)))
    occ2 = occupancy([{"symbol": "A", "side": "LONG", "pack": "g", "setId": "s1"}, {"symbol": "A", "side": "LONG", "pack": "g", "setId": "s1"}])
    out.append(("rep-occ-dup", occ2["duplicateSlots"] == 1, str(occ2)))
    blob = build({"closed": rows, "sets": {"rows": [], "setCount": 0, "activeCount": 0}, "open": [], "pfCost": {"n": 15, "minPf": 1.1}}, cost_pct=0.15, conn="x02")
    md = render_md(blob)
    out.append(("rep-md", "PositionCost" in md and "Independent Sets" in md, md[:80]))
    out.append(("rep-blob", blob["costAccounting"]["positionCostPct"] == 0.15 and blob["occupancy"]["maxOnePerSymbolDirSet"], str(blob["costAccounting"]["last15"])))
    out.append(("rep-dir", set((blob.get("byDirection") or {}).keys()) == {"LONG", "SHORT"}, str(blob.get("byDirection"))))
    out.append(("rep-dir-cost", all(bool(v.get("costSubtracted")) for v in (blob.get("byDirection") or {}).values()), str(blob.get("byDirection"))))
    out.append(("rep-strat", "general" in (blob.get("byStrategy") or {}), str(blob.get("byStrategy"))))
    out.append(("rep-netavg", "netAvg" in (blob.get("profitFactor") or {}).get("all", {}), str((blob.get("profitFactor") or {}).get("all"))))
    return out


if __name__ == "__main__":
    failed = 0
    for name, ok, detail in self_test():
        print(("PASS" if ok else "FAIL"), name, detail[:160])
        if not ok:
            failed += 1
    raise SystemExit(failed)
