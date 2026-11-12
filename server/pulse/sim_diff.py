"""Simulation against live: what the replay would have booked for a settled Set close next to what the exchange booked.

The replay fills a stop exactly at its planned distance and a target exactly at its planned distance and charges one PositionCost per order. A live close
pays the real fill price and the real fees. For every settled Set close this module keeps both numbers and the three differences between them:

  fill gap   live gross move - simulated gross move          (stop / target slippage; negative = live worse)
  cost gap   simulated cost - live cost                      (positive = the live fees + slippage allowance were cheaper than the configured PositionCost)
  net gap    live net - simulated net  = fill gap + cost gap

Simulated gross: +tp_pct for a target exit, -sl_pct for a stop exit; any other exit (trailing, time, scratch, manual) has no planned level, so its simulated
gross is the live gross (fill gap 0) and it is counted apart. A close that is not scored anywhere else (unverified exit, closed while the engine was down) is
not scored here either. Everything is a fraction of the position notional, one value per close (per-order PF, not USDT).

Pure functions on plain numbers: the engine feeds `diff_note` with each settled close, the CLI (`scripts/live_sim_diff.py`) feeds it the trades file."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

BUCKETS = ("tp", "sl", "other")


def _num(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _get(rec: Any, key: str, default: Any = None) -> Any:
    if isinstance(rec, dict):
        return rec.get(key, default)
    return getattr(rec, key, default)


def new_diff() -> Dict[str, Any]:
    return {"n": 0, "unknown": 0, "buckets": {}, "packs": {}, "totals": _blank(), "stopFactors": [], "targetFactors": []}


def _blank() -> Dict[str, float]:
    return {"n": 0, "liveG": 0.0, "liveL": 0.0, "simG": 0.0, "simL": 0.0, "fill": 0.0, "cost": 0.0, "net": 0.0}


def bucket_of(reason: str) -> str:
    r = str(reason or "").lower()
    if r == "tp" or r.startswith("tp-") or r.startswith("tp "):
        return "tp"
    if r == "sl" or r.startswith("sl-") or r.startswith("sl "):
        return "sl"
    return "other"


def _put(row: Dict[str, float], live_net: float, sim_net: float, fill: float, cost: float) -> None:
    row["n"] += 1
    if live_net > 0:
        row["liveG"] += live_net
    else:
        row["liveL"] -= live_net
    if sim_net > 0:
        row["simG"] += sim_net
    else:
        row["simL"] -= sim_net
    row["fill"] += fill
    row["cost"] += cost
    row["net"] += live_net - sim_net


def diff_note(agg: Dict[str, Any], rec: Any, position_cost: float) -> None:
    """One settled close. `position_cost` is the configured PositionCost as a FRACTION (0.0012 = 0.12 %). Unscored or incomplete rows are counted unknown."""
    reason = str(_get(rec, "reason", "") or "").lower()
    if "unverified-exit" in reason or "-downtime" in reason:
        agg["unknown"] += 1
        return
    gross = _num(_get(rec, "pnl_pct"))
    net = _num(_get(rec, "net_pnl_pct"))
    sl = _num(_get(rec, "sl_pct"))
    tp = _num(_get(rec, "tp_pct"))
    cost = _num(position_cost)
    if gross is None or net is None or cost is None:
        agg["unknown"] += 1
        return
    kind = bucket_of(reason)
    if kind == "tp" and tp and tp > 0:
        sim_gross = tp
        agg["targetFactors"].append(gross / tp)
    elif kind == "sl" and sl and sl > 0:
        sim_gross = -sl
        agg["stopFactors"].append(-gross / sl)
    else:
        kind = "other"
        sim_gross = gross
    sim_net = sim_gross - cost
    fill = gross - sim_gross
    cost_gap = cost - (gross - net)
    agg["n"] += 1
    for row in (agg["totals"], agg["buckets"].setdefault(kind, _blank()),
                agg["packs"].setdefault(str(_get(rec, "pack", "") or "-"), _blank())):
        _put(row, net, sim_net, fill, cost_gap)
    for key in ("stopFactors", "targetFactors"):          # bounded: the newest 2,000
        if len(agg[key]) > 2000:
            del agg[key][:-2000]


def _pf(g: float, l_: float) -> Optional[float]:
    return round(g / l_, 4) if l_ > 0 else (99.0 if g > 0 else None)


def _pct(xs: List[float], q: float) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    return round(s[min(len(s) - 1, int(q * (len(s) - 1) + 0.5))], 4)


def _row(row: Dict[str, float]) -> Dict[str, Any]:
    n = int(row["n"])
    return {"n": n, "livePf": _pf(row["liveG"], row["liveL"]), "simPf": _pf(row["simG"], row["simL"]),
            "liveNoLoss": row["liveG"] > 0 and row["liveL"] == 0, "simNoLoss": row["simG"] > 0 and row["simL"] == 0,
            "liveNetPct": round(100.0 * (row["liveG"] - row["liveL"]), 4), "simNetPct": round(100.0 * (row["simG"] - row["simL"]), 4),
            "fillGapPct": round(100.0 * row["fill"], 4), "costGapPct": round(100.0 * row["cost"], 4), "netGapPct": round(100.0 * row["net"], 4),
            "netGapPerClosePct": round(100.0 * row["net"] / n, 5) if n else None}


def diff_report(agg: Dict[str, Any]) -> Dict[str, Any]:
    """The figures for the statistics. stopFactor = the live loss over the planned stop distance (1.0 = the replay's assumption); targetFactor the same for
    targets. A factor of the stops well above 1 says the replay overstates what stops deliver: widen the minimum stop distance (`slMinSteps`,
    `minSlDistancePct`) or skip the thin symbols; the report only measures, it changes no setting."""
    sf, tf = agg["stopFactors"], agg["targetFactors"]
    return {"closes": int(agg["n"]), "unknown": int(agg["unknown"]),
            "total": _row(agg["totals"]),
            "byExit": {k: _row(agg["buckets"][k]) for k in BUCKETS if k in agg["buckets"]},
            "byPack": {k: _row(v) for k, v in sorted(agg["packs"].items())},
            "stopFactor": {"n": len(sf), "median": _pct(sf, 0.5), "mean": round(sum(sf) / len(sf), 4) if sf else None, "p90": _pct(sf, 0.9)},
            "targetFactor": {"n": len(tf), "median": _pct(tf, 0.5), "mean": round(sum(tf) / len(tf), 4) if tf else None, "p10": _pct(tf, 0.1)},
            "basis": "settled Set closes; per-order returns on the position notional; simulated = planned stop / target level, configured PositionCost"}
