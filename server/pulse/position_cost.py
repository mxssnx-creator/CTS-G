"""CTS Main-trade PF coordinate vs PositionCost.

1.00 = Neutral (net 0 after one PositionCost)
1.10 = +1× PositionCost net  (gross move = 2× cost)
Each 0.10 of ratio = one more PositionCost of net result.

required net % = cost% × ((ratio − 1) / 0.10)
"""
from __future__ import annotations

import math
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
# The live and historic coordinators share these named evaluation windows.  The
# largest window is intentionally bounded so every set can retain enough
# recent evidence without keeping its complete trade history in RAM.
EVALUATION_WINDOWS = (5, 10, 15, 25, 50, 75)


def _row_value(row: Any, *keys: str) -> Any:
    """Read the first present field from dicts, dataclasses, or API rows."""
    for key in keys:
        if isinstance(row, dict):
            if key in row and row.get(key) is not None:
                return row.get(key)
        elif hasattr(row, key):
            value = getattr(row, key)
            if value is not None:
                return value
    return None


def row_notional(row: Any) -> float:
    """Return the executed quote notional represented by one row."""
    direct = finite(_row_value(row, "notional", "quoteQty", "quoteQuantity", "cumQuote", "cumQuoteQty"))
    if direct > 1e-12:
        return direct
    qty = finite(_row_value(row, "qty", "executedQty", "filledQty", "cumQty", "quantity"))
    px = finite(_row_value(row, "entry", "avgPrice", "averagePrice", "price", "exit"))
    return max(0.0, qty * px)


def _nested_amount(value: Any) -> float:
    if isinstance(value, dict):
        for key in ("amount", "value", "qty", "quantity", "fee", "commission"):
            if value.get(key) is not None:
                return abs(finite(value.get(key)))
    return abs(finite(value))


def row_fee_usdt(row: Any) -> float:
    """Read an absolute exchange fee/commission from a fill or close row."""
    value = _row_value(
        row,
        "fee_total", "feeTotal", "totalFee", "totalCommission",
        "fee", "fees", "feeAmount", "commission", "commissionAmount",
        "transactionFee", "realizedFee",
    )
    if value is not None:
        return _nested_amount(value)
    entry = _row_value(row, "entry_fee", "entryFee", "openFee", "openCommission")
    exit_fee = _row_value(row, "exit_fee", "exitFee", "closeFee", "closeCommission")
    return _nested_amount(entry) + _nested_amount(exit_fee)


_FEE_RATE_KEYS = ("fee_rate", "feeRate", "commissionRate", "makerFeeRate", "takerFeeRate")


def _row_rate_pct(row: Any) -> Optional[float]:
    # ``costPct`` is a legacy/display field on historic simulation rows and
    # must not masquerade as an exchange measurement.  The explicit
    # position-cost aliases are authoritative; exchange fee-rate aliases are
    # measured evidence and are handled below.
    value = _row_value(row, "position_cost_pct", "positionCostPct", "cost_pct")
    if value is None:
        return None
    parsed = finite(value, -1.0)
    if parsed < 0:
        return None
    return normalize_position_cost_pct(parsed)


def _fee_rate_cost_pct(row: Any) -> Optional[float]:
    """Convert a one-leg exchange fee rate to round-trip PositionCost.

    Exchange APIs commonly expose rates as fractions (0.0005 = 0.05%), while
    the CTS contract stores PositionCost in percent (0.15 = 0.15%). Values up
    to 0.02 are interpreted as fractions and the result is doubled for the
    entry+exit position cost.
    """
    raw = _row_value(row, *_FEE_RATE_KEYS)
    if raw is None:
        return None
    parsed = finite(raw, -1.0)
    if parsed < 0:
        return None
    one_leg_pct = parsed * 100.0 if parsed <= 0.02 else parsed
    return normalize_position_cost_pct(one_leg_pct * 2.0)


def row_position_cost_pct(row: Any, fallback: float = POSITION_COST_PCT_DEFAULT) -> float:
    """Return the row's measured round-trip cost or the manual fallback.

    A row with an explicit cost is authoritative.  When only one exchange
    execution leg contains a fee, the measured leg rate is doubled because
    PositionCost is a round-trip contract.  Rows carrying entry+exit/total
    fees are used as-is.  This keeps historical rows deterministic while
    allowing live exchange fills to replace the static estimate safely.
    """
    source = str(_row_value(row, "cost_source", "costSource", "source") or "").lower()
    explicit = _row_rate_pct(row)
    legacy_cost = _row_value(row, "costPct")
    if explicit is not None:
        return explicit
    if legacy_cost is not None and ("cost" in source or "exchange" in source):
        return normalize_position_cost_pct(legacy_cost, fallback)
    notion = row_notional(row)
    if notion <= 1e-12:
        return normalize_position_cost_pct(fallback)
    entry_fee = _nested_amount(_row_value(row, "entry_fee", "entryFee", "openFee", "openCommission"))
    exit_fee = _nested_amount(_row_value(row, "exit_fee", "exitFee", "closeFee", "closeCommission"))
    total_fee = _row_value(row, "fee_total", "feeTotal", "totalFee", "totalCommission")
    # Normalized report rows carry zero-valued fee placeholders.  They are not
    # measurements unless the row is explicitly marked as live/exchange cost
    # data; otherwise the configured manual fallback must remain authoritative.
    total_is_measured = total_fee is not None and (
        _nested_amount(total_fee) > 0
        or "exchange" in source
        or "live" in source
        or "cost" in source
    )
    if total_is_measured or (entry_fee > 0 and exit_fee > 0):
        measured = (_nested_amount(total_fee) if total_fee is not None else entry_fee + exit_fee) / notion * 100.0
        return normalize_position_cost_pct(measured, fallback)
    fee = row_fee_usdt(row)
    if fee > 0:
        # ``fee`` on an order/fill is normally one leg.  An explicit scope can
        # mark it as a complete position cost when the endpoint provides one.
        scope = str(_row_value(row, "fee_scope", "feeScope", "fee_type", "feeType") or "").lower()
        legs = 1.0 if scope in ("roundtrip", "position", "total", "both") else 2.0
        return normalize_position_cost_pct(fee / notion * 100.0 * legs, fallback)
    rate_cost = _fee_rate_cost_pct(row)
    if rate_cost is not None:
        return rate_cost
    return normalize_position_cost_pct(fallback)


def row_has_measured_cost(row: Any) -> bool:
    """Whether a row contains exchange-derived fee/cost evidence."""
    source = str(_row_value(row, "cost_source", "costSource") or "").lower()
    return bool(
        "exchange" in source
        or (
            _row_value(row, "position_cost_pct", "positionCostPct", "cost_pct") is not None
            and ("cost" in source or "live" in source)
        )
        or (_row_value(row, "costPct") is not None and ("cost" in source or "exchange" in source))
        or row_fee_usdt(row) > 0
        or _row_value(row, *_FEE_RATE_KEYS) is not None
    )


def exchange_order_cost_sample(row: Any, fallback: float = POSITION_COST_PCT_DEFAULT) -> Optional[Dict[str, float]]:
    """Normalize one exchange order/fill into a measured round-trip sample."""
    notion = row_notional(row)
    fee = row_fee_usdt(row)
    rate = _row_value(row, *_FEE_RATE_KEYS)
    if notion <= 1e-12 and rate is None:
        return None
    if rate is not None and fee <= 0:
        cost = _fee_rate_cost_pct(row)
        if cost is None:
            return None
    elif notion > 1e-12 and fee > 0:
        cost = row_position_cost_pct(row, fallback)
    else:
        return None
    return {"notional": float(notion), "fee": float(fee), "costPct": float(cost)}


def effective_position_cost_pct(rows: Sequence[Any], fallback: float = POSITION_COST_PCT_DEFAULT) -> Dict[str, Any]:
    """Weighted measured PositionCost summary with an explicit fallback state."""
    fallback_n = normalize_position_cost_pct(fallback)
    total_n = 0.0
    weighted = 0.0
    measured = 0
    for row in rows:
        if not row_has_measured_cost(row):
            continue
        notion = row_notional(row)
        if notion <= 1e-12:
            continue
        measured_cost = row_position_cost_pct(row, fallback_n)
        total_n += notion
        weighted += notion * measured_cost
        measured += 1
    if measured <= 0 or total_n <= 1e-12:
        return {
            "costPct": fallback_n,
            "fallbackPct": fallback_n,
            "sampleCount": 0,
            "notional": 0.0,
            "source": "manual-fallback",
            "complete": False,
        }
    return {
        "costPct": round(weighted / total_n, 8),
        "fallbackPct": fallback_n,
        "sampleCount": measured,
        "notional": round(total_n, 8),
        "source": "live-exchange",
        "complete": True,
    }


def normalize_pf(value: Any, fallback: float) -> float:
    """Clamp a configured PF floor without changing the requested 0.02 step."""
    try:
        parsed = float(value)
    except Exception:
        parsed = float(fallback)
    if parsed != parsed or abs(parsed) == float("inf"):
        parsed = float(fallback)
    return round(max(PF_MIN, min(PF_MAX, parsed)), 2)
SL_TP_MIN = 0.1
SL_TP_MAX = 3.0
SL_TP_STEP = 0.1
SL_TP_RATIOS = tuple(round(SL_TP_MIN + i * SL_TP_STEP, 1) for i in range(int(round((SL_TP_MAX - SL_TP_MIN) / SL_TP_STEP)) + 1))


def sl_tp_grid(lo: float = SL_TP_MIN, hi: float = SL_TP_MAX, step: float = SL_TP_STEP) -> List[float]:
    """Inclusive SL:TP ratio axis: 0.1 … 3.0 in 0.1 increments."""
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
    """PositionCost as a fraction.

    CTS stores PositionCost in percent (0.15 means 0.15%). A small legacy
    caller may still pass a fraction such as 0.0015; values at or below 0.02
    retain that compatibility convention.
    """
    c = max(0.0, finite(cost_pct, POSITION_COST_PCT_DEFAULT))
    return c / 100.0 if c > 0.02 else c


def net_pnl_pct(pnl_pct: float, cost_pct: float = POSITION_COST_PCT_DEFAULT) -> float:
    """Gross price-move fraction minus one PositionCost."""
    return finite(pnl_pct) - cost_as_frac(cost_pct)


def net_pnl_usdt(pnl_pct: float, qty: float, entry: float, cost_pct: float = POSITION_COST_PCT_DEFAULT) -> float:
    """USDT result after deducting PositionCost from the gross move."""
    notion = max(0.0, finite(qty) * finite(entry))
    return notion * net_pnl_pct(pnl_pct, cost_pct)


def row_pnl_pct(row: Any, cost_pct: float = POSITION_COST_PCT_DEFAULT) -> float:
    """Gross price-move fraction from a fill row.

    New fills carry ``pnl_pct``. Older persisted tapes can contain only the
    already cost-net USDT ``pnl`` plus quantity and entry. Reconstruct the
    gross move in that case so a missing percentage cannot become a synthetic
    cost-only loss in PF, expectancy, or DDT calculations.
    """
    raw = row.get("pnl_pct") if isinstance(row, dict) else getattr(row, "pnl_pct", None)
    if raw is not None:
        return finite(raw)
    if isinstance(row, dict):
        pnl = finite(row.get("pnl"))
        qty = finite(row.get("qty"))
        entry = finite(row.get("entry"))
    else:
        pnl = finite(getattr(row, "pnl", 0))
        qty = finite(getattr(row, "qty", 0))
        entry = finite(getattr(row, "entry", 0))
    notional = max(0.0, qty * entry)
    row_cost = row_position_cost_pct(row, cost_pct)
    return pnl / notional + cost_as_frac(row_cost) if notional > 1e-12 else 0.0


def row_net_pnl(row: Any, cost_pct: float = POSITION_COST_PCT_DEFAULT) -> float:
    """Net fraction after subtracting one PositionCost. Always from the gross move."""
    actual_cost = row_position_cost_pct(row, cost_pct)
    return net_pnl_pct(row_pnl_pct(row, actual_cost), actual_cost)


def completed_roundtrips(rows: Sequence[Any]) -> list[Dict[str, Any]]:
    """Aggregate confirmed close legs; partial fills are not extra samples."""
    groups: Dict[tuple, list] = {}
    seen: set = set()
    for raw in rows:
        row = raw if isinstance(raw, dict) else vars(raw)
        if not row.get("exchange_confirmed") or not row.get("client_id"):
            continue
        fill_key = row.get("close_fill_id") or row.get("fillId")
        if fill_key and fill_key in seen:
            continue
        if fill_key:
            seen.add(fill_key)
        key = (row.get("conn"), row.get("symbol"), row.get("side"), row["client_id"])
        groups.setdefault(key, []).append(row)
    out = []
    for legs in groups.values():
        legs.sort(key=lambda row: finite(row.get("t")))
        if legs[-1].get("partial"):
            continue
        row = dict(legs[-1])
        notion = sum(row_notional(leg) for leg in legs)
        if notion <= 0:
            continue
        qty = sum(finite(leg.get("qty")) for leg in legs)
        expected_qty = max(finite(leg.get("roundtrip_qty")) for leg in legs)
        if expected_qty > 0 and qty < expected_qty * (1 - 1e-8):
            continue  # incomplete retained history cannot promote this config
        cost = sum(row_position_cost_pct(leg) * row_notional(leg) for leg in legs) / notion
        row.update(qty=qty, entry=notion / max(qty, 1e-12),
                   pnl=sum(finite(leg.get("pnl")) for leg in legs),
                   pnl_pct=sum(row_pnl_pct(leg) * row_notional(leg) for leg in legs) / notion,
                   fee_total=sum(finite(leg.get("fee_total")) for leg in legs),
                   position_cost_pct=cost, partial=False, closeLegs=len(legs))
        out.append(row)
    return sorted(out, key=lambda row: finite(row.get("t")))


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
    # API surfaces provide both chronological and newest-first tapes. A
    # timestamp-normalized tail makes every "last N" gate mean the same thing.
    window = sorted(
        list(rows),
        key=lambda row: finite(row.get("t") if isinstance(row, dict) else getattr(row, "t", 0)),
    )[-max(1, int(n)) :]
    rs: List[float] = []
    nets: List[float] = []
    gp = gl = 0.0
    weighted_cost = 0.0
    weighted_notional = 0.0
    for row in window:
        actual_cost = row_position_cost_pct(row, cost_pct)
        pnl_pct = row_pnl_pct(row, actual_cost)
        net = row_net_pnl(row, actual_cost)
        rs.append(signed_result_r(pnl_pct, actual_cost))
        nets.append(net)
        notion = row_notional(row)
        if row_has_measured_cost(row) and notion > 1e-12:
            weighted_cost += actual_cost * notion
            weighted_notional += notion
        if net > 0:
            gp += net
        elif net < 0:
            gl += abs(net)
    count = len(rs)
    avg_r = sum(rs) / count if count else 0.0
    net_avg = sum(nets) / count if count else 0.0
    ratio = ratio_from_r(avg_r) if count else RATIO_BASE
    effective_cost = weighted_cost / weighted_notional if weighted_notional > 1e-12 else normalize_position_cost_pct(cost_pct)
    classic = (gp / gl) if gl > 0 else (99.0 if gp > 0 else 0.0)
    return {
        "n": float(n),
        "count": float(count),
        "avgR": round(avg_r, 4),
        "ratio": round(ratio, 4),
        "classicPf": round(classic, 4),
        "costPct": round(float(effective_cost), 8),
        "netPct": round(net_move_pct(ratio, effective_cost), 4),
        "grossPct": round(gross_move_pct(ratio, effective_cost), 4),
        "netAvg": round(net_avg, 6),
        "costSubtracted": True,
        "costSource": "live-exchange" if weighted_notional > 1e-12 else "manual-fallback",
        "costSamples": sum(1 for row in window if row_has_measured_cost(row)),
    }


def evaluation_windows(
    rows: Sequence[Any],
    cost_pct: float = POSITION_COST_PCT_DEFAULT,
    windows: Sequence[int] = EVALUATION_WINDOWS,
    required_samples: int = 8,
) -> Dict[str, Dict[str, Any]]:
    """Return the shared last-position-N PF/EV view for one independent tape.

    Every window is calculated from the same timestamp-normalized tape and
    deducts the measured row cost once, falling back to the configured manual
    cost.  ``available`` distinguishes a real N-sample window from a cold
    tape; ``validated`` is a positive-PF/sample signal and is deliberately
    independent from any strategy-specific minimum PF floor.
    """
    ordered = sorted(
        [row for row in rows if row is not None],
        key=lambda row: finite(row.get("t") if isinstance(row, dict) else getattr(row, "t", 0)),
    )
    out: Dict[str, Dict[str, Any]] = {}
    seen: set[int] = set()
    for raw_n in windows:
        try:
            requested = max(1, int(raw_n))
        except Exception:
            continue
        if requested in seen:
            continue
        seen.add(requested)
        metric = last_n_cost_pf(ordered, requested, cost_pct)
        count = int(metric.get("count") or 0)
        ratio = float(metric.get("ratio") or RATIO_BASE)
        required = max(1, min(requested, int(required_samples or 1)))
        out[f"last{requested}"] = {
            "requestedN": requested,
            "n": count,
            "available": count >= requested,
            "requiredSamples": required,
            "validated": count >= required and ratio + 1e-9 >= RATIO_BASE,
            "pf": round(ratio, 4),
            "classicPf": float(metric.get("classicPf") or 0.0),
            "avgR": float(metric.get("avgR") or 0.0),
            "netAvg": float(metric.get("netAvg") or 0.0),
            "netPct": float(metric.get("netPct") or 0.0),
            "costPct": float(metric.get("costPct") or normalize_position_cost_pct(cost_pct)),
            "costSamples": int(metric.get("costSamples") or 0),
            "costSubtracted": True,
        }
    return out


def normalize_position_cost_pct(value: Any, fallback: float = POSITION_COST_PCT_DEFAULT) -> float:
    """Normalize PositionCost to the percent convention used by every tape."""
    c = finite(value, fallback)
    if c < 0:
        c = 0.0
    if c > 2.0:
        c /= 100.0
    return round(c if c <= 1.0 else fallback, 8)


def _classic_pf(values: Sequence[float], fallback: float = 0.0) -> float:
    gp = sum(value for value in values if value > 0)
    gl = abs(sum(value for value in values if value < 0))
    if gl > 0:
        return gp / gl
    return 99.0 if gp > 0 else fallback


def cost_aware_metrics(
    rows: Sequence[Any],
    cost_pct: float = POSITION_COST_PCT_DEFAULT,
    required_samples: int = 8,
) -> Dict[str, Any]:
    """Return gross/net PF and EV from one shared closed sample.

    ``pnl_pct`` is the gross price move and PositionCost is deducted exactly
    once. Confidence is deliberately a sample-coverage signal, not a claim of
    statistical certainty; callers can show the explicit insufficient status.
    """
    cost = normalize_position_cost_pct(cost_pct)
    gross: List[float] = []
    net: List[float] = []
    measured = 0
    for row in rows:
        row_cost = row_position_cost_pct(row, cost)
        gross_value = row_pnl_pct(row, row_cost)
        gross.append(gross_value)
        net.append(gross_value - cost_as_frac(row_cost))
        measured += int(row_has_measured_cost(row))
    sample = len(gross)
    required = max(1, int(required_samples or 1))
    confidence = min(1.0, sample / required) if sample else 0.0
    uncertainty = 1.0 / math.sqrt(sample) if sample else 1.0
    return {
        "sampleCount": sample,
        "requiredSamples": required,
        "grossPf": round(_classic_pf(gross), 6),
        "netPf": round(_classic_pf(net), 6),
        "grossEv": round(sum(gross) / sample, 8) if sample else 0.0,
        "netEv": round(sum(net) / sample, 8) if sample else 0.0,
        "ev": round(sum(net) / sample, 8) if sample else 0.0,
        "confidence": round(confidence, 4),
        "uncertainty": round(uncertainty, 4),
        "insufficientSample": sample < required,
        "status": "insufficient-sample" if sample < required else "qualified-sample",
        "costPct": cost,
        "costSubtracted": True,
        "costSource": "live-exchange" if measured else "manual-fallback",
        "costSamples": measured,
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
    """Return SL/TP as fractions; TP is primary and ratio is clamped to 0.1–3.0."""
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
    legacy = {"t": 1, "pnl": 1.0, "qty": 100.0, "entry": 1.0}
    assert abs(row_pnl_pct(legacy, 0.15) - 0.0115) < 1e-12
    assert abs(row_net_pnl(legacy, 0.15) - 0.01) < 1e-12
    measured = {"t": 2, "pnl_pct": 0.004, "qty": 10, "entry": 100, "fee_total": 0.30, "cost_source": "live-exchange"}
    assert abs(row_position_cost_pct(measured, 0.15) - 0.03) < 1e-12
    assert abs(row_net_pnl(measured, 0.15) - 0.0037) < 1e-12
    eff = effective_position_cost_pct([measured], 0.15)
    assert eff["source"] == "live-exchange" and abs(float(eff["costPct"]) - 0.03) < 1e-12
    sample = exchange_order_cost_sample({"executedQty": 10, "avgPrice": 100, "commission": 0.15}, 0.15)
    assert sample and abs(float(sample["costPct"]) - 0.03) < 1e-12
    assert row_side({"side": "long"}) == "LONG" and row_side({"side": "SELL"}) == "SHORT"
    assert len(filter_side([{"side": "LONG"}, {"side": "SHORT"}], "LONG")) == 1
    n = last_n_cost_pf([{"pnl_pct": 0.003, "pnl": 0.0015}] * 10, 10, 0.15)
    assert n["costSubtracted"] and abs(n["netAvg"] - 0.0015) < 1e-9
    windows = evaluation_windows([{"t": i, "pnl_pct": 0.003, "pnl": 0.0015} for i in range(80)], 0.15)
    assert set(windows) == {"last5", "last10", "last15", "last25", "last50", "last75"}
    assert windows["last75"]["n"] == 75 and windows["last75"]["available"]
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
