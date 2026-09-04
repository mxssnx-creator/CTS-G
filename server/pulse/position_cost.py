"""CTS Main-trade PF coordinate vs PositionCost.

1.00 = Neutral (net 0 after one PositionCost)
1.10 = +1× PositionCost net  (gross move = 2× cost)
Each 0.10 of ratio = one more PositionCost of net result.

required net % = cost% × ((ratio − 1) / 0.10)
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

POSITION_COST_PCT_DEFAULT = 0.15
RATIO_BASE = 1.0
RATIO_SCALE = 0.10
# User-facing PF controls share one contract across UI, overlay, and workers.
PF_MIN = 0.80
PF_MAX = 2.50
PF_STEP = 0.02
RATIO_MIN = PF_MIN
RATIO_MAX = PF_MAX
RATIO_STEP = PF_STEP
LAST_N_DEFAULT = 15


def normalize_pf(value: Any, fallback: float) -> float:
    """Clamp a configured PF floor without changing the requested 0.02 step."""
    try:
        parsed = float(value)
    except Exception:
        parsed = float(fallback)
    if parsed != parsed or abs(parsed) == float("inf"):
        parsed = float(fallback)
    return round(max(PF_MIN, min(PF_MAX, parsed)), 2)
SL_TP_MIN = 0.2
SL_TP_MAX = 2.6
SL_TP_STEP = 0.2
SL_TP_RATIOS = tuple(round(SL_TP_MIN + i * SL_TP_STEP, 1) for i in range(int(round((SL_TP_MAX - SL_TP_MIN) / SL_TP_STEP)) + 1))


def sl_tp_grid(lo: float = SL_TP_MIN, hi: float = SL_TP_MAX, step: float = SL_TP_STEP) -> List[float]:
    """Inclusive SL:TP ratio axis. Default 0.2 … 2.6 step 0.2 → 13 books."""
    a = finite(lo, SL_TP_MIN)
    b = finite(hi, SL_TP_MAX)
    s = finite(step, SL_TP_STEP)
    if s <= 0:
        s = SL_TP_STEP
    if b < a:
        a, b = b, a
    a = max(SL_TP_MIN, min(SL_TP_MAX, a))
    b = max(SL_TP_MIN, min(SL_TP_MAX, b))
    out: List[float] = []
    x = a
    guard = 0
    while x <= b + 1e-9 and guard < 64:
        out.append(round(x, 1))
        x = round(x + s, 10)
        guard += 1
    return out or list(SL_TP_RATIOS)


def finite(v: Any, fallback: float = 0.0) -> float:
    try:
        n = float(v)
    except Exception:
        return fallback
    return n if n == n and abs(n) != float("inf") else fallback


def snap_ratio(value: Any, lo: float = SL_TP_MIN, hi: float = SL_TP_MAX, step: float = SL_TP_STEP) -> float:
    x = finite(value, 0.6)
    if x <= 0:
        x = 0.6
    x = max(lo, min(hi, x))
    n = int((x - lo) / step + 0.5 + 1e-12)
    nmax = int((hi - lo) / step + 0.5)
    n = max(0, min(nmax, n))
    return round(lo + n * step, 1)


def cost_as_frac(cost_pct: float = POSITION_COST_PCT_DEFAULT) -> float:
    """PositionCost as a fraction. 0.15 (percent) → 0.0015; 0.0015 already a fraction."""
    c = max(0.0, finite(cost_pct, POSITION_COST_PCT_DEFAULT))
    return c / 100.0 if c > 0.05 else c


def net_pnl_pct(pnl_pct: float, cost_pct: float = POSITION_COST_PCT_DEFAULT) -> float:
    """Gross price-move fraction minus one PositionCost."""
    return finite(pnl_pct) - cost_as_frac(cost_pct)


def net_pnl_usdt(pnl_pct: float, qty: float, entry: float, cost_pct: float = POSITION_COST_PCT_DEFAULT) -> float:
    """USDT result after deducting PositionCost from the gross move."""
    notion = max(0.0, finite(qty) * finite(entry))
    return notion * net_pnl_pct(pnl_pct, cost_pct)


def row_pnl_pct(row: Any) -> float:
    """Gross price-move fraction from a fill row."""
    if isinstance(row, dict):
        return finite(row.get("pnl_pct"))
    return finite(getattr(row, "pnl_pct", 0))


def row_net_pnl(row: Any, cost_pct: float = POSITION_COST_PCT_DEFAULT) -> float:
    """Net fraction after subtracting one PositionCost. Always from the gross move."""
    return net_pnl_pct(row_pnl_pct(row), cost_pct)


def row_side(row: Any) -> str:
    raw = ""
    if isinstance(row, dict):
        raw = str(row.get("side") or row.get("direction") or "")
    else:
        raw = str(getattr(row, "side", "") or getattr(row, "direction", "") or "")
    u = raw.strip().upper()
    if u.startswith("L") or u in ("1", "BUY"):
        return "LONG"
    if u.startswith("S") or u in ("-1", "SELL"):
        return "SHORT"
    return ""


def filter_side(rows: Sequence[Any], side: Optional[str] = None) -> List[Any]:
    if not side:
        return [r for r in rows]
    want = str(side).strip().upper()
    if want in ("L", "1", "BUY"):
        want = "LONG"
    elif want in ("S", "-1", "SELL"):
        want = "SHORT"
    if want not in ("LONG", "SHORT"):
        return [r for r in rows]
    return [r for r in rows if row_side(r) == want]


def signed_result_r(pnl_pct: float, cost_pct: float = POSITION_COST_PCT_DEFAULT) -> float:
    """pnl_pct is a fraction (0.001 = 0.10%). Cost is a percent (0.15)."""
    cost = max(1e-9, finite(cost_pct, POSITION_COST_PCT_DEFAULT))
    gross_move_pct = finite(pnl_pct) * 100.0
    return (gross_move_pct - cost) / cost


def ratio_from_r(signed_r: float) -> float:
    return RATIO_BASE + finite(signed_r) * RATIO_SCALE


def r_from_ratio(ratio: float) -> float:
    return (finite(ratio, RATIO_BASE) - RATIO_BASE) / RATIO_SCALE


def net_move_pct(ratio: float, cost_pct: float) -> float:
    return finite(cost_pct) * r_from_ratio(ratio)


def gross_move_pct(ratio: float, cost_pct: float) -> float:
    cost = max(0.0, finite(cost_pct))
    return cost + net_move_pct(ratio, cost)


def last_n_cost_pf(
    rows: Sequence[Any],
    n: int = LAST_N_DEFAULT,
    cost_pct: float = POSITION_COST_PCT_DEFAULT,
) -> Dict[str, float]:
    window = list(rows)[-max(1, int(n)) :]
    rs: List[float] = []
    nets: List[float] = []
    gp = gl = 0.0
    for row in window:
        pnl_pct = row_pnl_pct(row)
        net = row_net_pnl(row, cost_pct)
        rs.append(signed_result_r(pnl_pct, cost_pct))
        nets.append(net)
        if net > 0:
            gp += net
        elif net < 0:
            gl += abs(net)
    count = len(rs)
    avg_r = sum(rs) / count if count else 0.0
    net_avg = sum(nets) / count if count else 0.0
    ratio = ratio_from_r(avg_r) if count else RATIO_BASE
    classic = (gp / gl) if gl > 0 else (99.0 if gp > 0 else 0.0)
    return {
        "n": float(n),
        "count": float(count),
        "avgR": round(avg_r, 4),
        "ratio": round(ratio, 4),
        "classicPf": round(classic, 4),
        "costPct": float(cost_pct),
        "netPct": round(net_move_pct(ratio, cost_pct), 4),
        "grossPct": round(gross_move_pct(ratio, cost_pct), 4),
        "netAvg": round(net_avg, 6),
        "costSubtracted": True,
    }


def clamp_pct(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def resolve_sl_tp(
    *,
    base_sl: float,
    base_tp: float,
    sl_min: float,
    sl_max: float,
    tp_min: float,
    tp_max: float,
    ind_sl: float = 0.0,
    ind_tp: float = 0.0,
    cost_pct: float = POSITION_COST_PCT_DEFAULT,
    tp_cost_ratio: float = 5.0,
    sl_to_tp: float = 0.6,
    rr: float = 1.8,
    bind_sl_to_tp: bool = True,
) -> tuple[float, float, str]:
    """Return SL/TP as fractions. TP is primary; SL = TP × ratio (clamped 0.2–2.6)."""
    ratio = max(SL_TP_MIN, min(SL_TP_MAX, round(finite(sl_to_tp, 0.6), 1)))
    if ratio <= 0:
        ratio = 0.6
    cost_tp = max(tp_min, (cost_pct * tp_cost_ratio) / 100.0)
    if ind_tp > 0:
        tp = clamp_pct(ind_tp, tp_min, tp_max)
        src = "indication"
    elif base_tp > 0:
        tp = clamp_pct(base_tp, tp_min, tp_max)
        src = "overlay"
    else:
        tp = clamp_pct(cost_tp, tp_min, tp_max)
        src = "cost"
    if bind_sl_to_tp:
        sl = clamp_pct(tp * ratio, sl_min, sl_max)
        src = f"{src}:r{ratio:.1f}"
        return sl, tp, src
    cost_sl = max(sl_min, cost_tp * ratio)
    sl, chosen_tp = cost_sl, cost_tp
    if ind_sl > 0 and ind_tp > 0:
        sl = clamp_pct(ind_sl, sl_min, sl_max)
        chosen_tp = clamp_pct(ind_tp, tp_min, tp_max)
        src = "indication"
    else:
        sl = clamp_pct(base_sl if base_sl > 0 else cost_sl, sl_min, sl_max)
        chosen_tp = clamp_pct(base_tp if base_tp > 0 else cost_tp, tp_min, tp_max)
        src = "overlay"
    if chosen_tp < sl * 1.05:
        chosen_tp = clamp_pct(sl * rr, tp_min, tp_max)
    return sl, chosen_tp, src


if __name__ == "__main__":
    assert abs(signed_result_r(0.003, 0.15) - 1.0) < 1e-9
    assert abs(ratio_from_r(1.0) - 1.10) < 1e-9
    assert abs(ratio_from_r(0.0) - 1.00) < 1e-9
    assert abs(cost_as_frac(0.15) - 0.0015) < 1e-12
    assert abs(cost_as_frac(0.0015) - 0.0015) < 1e-12
    assert abs(net_pnl_pct(0.003, 0.15) - 0.0015) < 1e-12
    assert abs(net_pnl_usdt(0.003, 1.0, 100.0, 0.15) - 0.15) < 1e-9
    assert abs(row_net_pnl({"pnl_pct": 0.003, "pnl": 9}, 0.15) - 0.0015) < 1e-12
    assert row_side({"side": "long"}) == "LONG" and row_side({"side": "SELL"}) == "SHORT"
    assert len(filter_side([{"side": "LONG"}, {"side": "SHORT"}], "LONG")) == 1
    n = last_n_cost_pf([{"pnl_pct": 0.003, "pnl": 0.0015}] * 10, 10, 0.15)
    assert n["costSubtracted"] and abs(n["netAvg"] - 0.0015) < 1e-9
    print("position_cost ok")
    rows = [{"pnl_pct": 0.003, "pnl": 1.0}] * 15
    got = last_n_cost_pf(rows, 15, 0.15)
    assert abs(got["ratio"] - 1.10) < 1e-6, got
    sl, tp, src = resolve_sl_tp(
        base_sl=0.0048, base_tp=0.0075,
        sl_min=0.002, sl_max=0.012, tp_min=0.0035, tp_max=0.024,
        cost_pct=0.15, tp_cost_ratio=5, sl_to_tp=0.64,
    )
    assert src.endswith("r0.6") and abs(tp - 0.0075) < 1e-9
    assert abs(sl - 0.0075 * 0.6) < 1e-9, (sl, tp, src)
    sl15, tp15, src15 = resolve_sl_tp(
        base_sl=0.0048, base_tp=0.0075,
        sl_min=0.002, sl_max=0.02, tp_min=0.0035, tp_max=0.024,
        sl_to_tp=1.5,
    )
    assert abs(sl15 - 0.0075 * 1.5) < 1e-9 and sl15 > tp15
    assert abs(snap_ratio(0.64) - 0.6) < 1e-9
    print("position_cost ok", got, src, src15)
