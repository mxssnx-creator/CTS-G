"""Canonical accounting contracts shared by the pulse engines and reports."""
from __future__ import annotations

import hashlib
import math
from typing import Any, Dict, Iterable, Mapping

STAGES = ("Base", "Main", "Real", "Live", "Exchange")
STAGE_ORDER = {name.lower(): index for index, name in enumerate(STAGES)}
AXES = ("prev", "last", "cont", "pause")
INDICATION_KINDS = ("state", "signals", "active", "direction", "move", "common", "trend", "break")
STRATEGIES = ("indications", "general", "block", "trailing", "dca", "exits")
DIRECTIONS = ("LONG", "SHORT")

PF_MIN = 0.80
PF_MAX = 2.50
PF_STEP = 0.02
VOLUME_RATIO_UNIT = 0.01


def finite_number(value: Any, fallback: float = 0.0) -> float:
    try:
        parsed = float(value)
    except Exception:
        return fallback
    return parsed if math.isfinite(parsed) else fallback


def normalize_pf(value: Any, fallback: float = 1.0) -> float:
    """Normalize every PF floor/control to the shared 0.80..2.50 contract."""
    parsed = finite_number(value, finite_number(fallback, 1.0))
    return round(max(PF_MIN, min(PF_MAX, parsed)), 2)


def normalize_position_cost_pct(value: Any, fallback: float = 0.15) -> float:
    """PositionCost is expressed as a percent: 0.15 means 0.15%."""
    parsed = max(0.0, finite_number(value, fallback))
    if parsed > 2.0:
        parsed /= 100.0
    return round(parsed if parsed <= 1.0 else fallback, 8)


def normalize_volume_ratio(value: Any, fallback: float = 1.0) -> float:
    """Keep volume multipliers finite; relative count uses VOLUME_RATIO_UNIT."""
    parsed = finite_number(value, fallback)
    return round(max(0.0, min(100.0, parsed)), 8)


def normalize_stage(value: Any, fallback: str = "Unqualified") -> str:
    raw = str(value or "").strip().lower()
    for stage in STAGES:
        if raw == stage.lower():
            return stage
    return fallback


def normalize_axis(value: Any, fallback: str = "") -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in AXES else fallback


def normalize_kind(value: Any, fallback: str = "") -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in INDICATION_KINDS else fallback


def normalize_direction(value: Any, fallback: str = "") -> str:
    raw = str(value or "").strip().upper()
    if raw in ("L", "1", "BUY"):
        return "LONG"
    if raw in ("S", "-1", "SELL"):
        return "SHORT"
    return raw if raw in DIRECTIONS else fallback


def stable_key(*parts: Any) -> str:
    """Return a compact deterministic key for retries and restart-safe dedupe."""
    raw = "|".join("" if part is None else str(part) for part in parts)
    digest = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:20]
    return f"v1:{digest}"


def increment_counts(target: Dict[str, int], values: Iterable[str], amount: int = 1) -> None:
    for value in values:
        key = str(value or "")
        if key:
            target[key] = int(target.get(key, 0) or 0) + int(amount)


def safe_mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


__all__ = [
    "AXES",
    "DIRECTIONS",
    "INDICATION_KINDS",
    "PF_MAX",
    "PF_MIN",
    "PF_STEP",
    "STAGES",
    "STAGE_ORDER",
    "STRATEGIES",
    "VOLUME_RATIO_UNIT",
    "finite_number",
    "increment_counts",
    "normalize_axis",
    "normalize_direction",
    "normalize_kind",
    "normalize_pf",
    "normalize_position_cost_pct",
    "normalize_stage",
    "normalize_volume_ratio",
    "safe_mapping",
    "stable_key",
]
