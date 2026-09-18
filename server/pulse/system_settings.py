"""Operational settings, separate from strategy qualification and execution."""
from __future__ import annotations

import math
import json
from position_cost import shared_pf_settings, normalize_pf, POSITIVE_PF, SL_MIN_PCT
from validation_policy import control_min_trades
from pathlib import Path

# name: default, minimum, maximum, integer. Zero memory limits mean automatic.
SYSTEM_LIMITS = json.loads(Path(__file__).with_name("system-limits.json").read_text())


def normalize_system_settings(raw=None):
    raw = raw if isinstance(raw, dict) else {}
    result = {}
    for key, (default, lo, hi, integer) in SYSTEM_LIMITS.items():
        try:
            value = float(raw.get(key, default))
            if not math.isfinite(value) or isinstance(raw.get(key), bool):
                value = default
        except (TypeError, ValueError, OverflowError):
            value = default
        value = min(hi, max(lo, value))
        result[key] = int(value) if integer else value
    if result["rssSoftMb"] and result["rssHardMb"]:
        result["rssHardMb"] = max(result["rssSoftMb"], result["rssHardMb"])
    return result


def calculation_overlay(overlay, cts=None):
    """Execution switches never remove the General reference calculator.

    Standalone research can still select packs explicitly. Runtime callers
    use this boundary before building/rebuilding any coordination catalog.
    """
    result = shared_pf_settings(overlay)
    result["controlMinTrades"] = control_min_trades(result.get("controlMinTrades"))
    result.setdefault("histLookbackBars", 2880)
    try:
        hours = int(round(float(result.get("histTestHours", 20))))
    except (TypeError, ValueError, OverflowError):
        hours = 20
    result["histTestHours"] = max(4, min(64, hours))
    result["histTestMinPf"] = normalize_pf(result.get("histTestMinPf"), POSITIVE_PF)
    result["histTestEnabled"] = True if result.get("histTestEnabled") is None else bool(result.get("histTestEnabled"))
    try:
        refresh = int(round(float(result.get("histTestRefreshHours", 2))))
    except (TypeError, ValueError, OverflowError):
        refresh = 2
    result["histTestRefreshHours"] = max(1, min(8, refresh))
    result.setdefault("baseEvalPosCount", result.get("setPfWindow", 30))
    result["setPfWindow"] = result["baseEvalPosCount"]
    result.setdefault("setMinSamples", result["baseEvalPosCount"])
    for key in ("maxPerGroup", "setMaxActive", "entryPolicyMaxCandidates"):
        result.setdefault(key, 0)
    result.setdefault("maxOpen", 100)
    result.setdefault("symbolCap", 50)
    for key in ("axisPrevEnabled", "axisLastEnabled", "axisContEnabled", "axisPauseEnabled"):
        result.setdefault(key, False)
    result["stratGeneral"] = True
    result["histEnabled"] = True
    modules = dict(result.get("modules") or {}) if isinstance(result.get("modules"), dict) else {}
    modules.update({"core.historic": True, "strategy.sets": True})
    result["modules"] = modules
    def level(value, fallback):
        try:
            return max(0, min(6, int(value)))
        except (TypeError, ValueError, OverflowError):
            return fallback
    stack = level(result.get("blockMaxStack", 6), 6) or 6
    result["blockActiveMinLevel"] = min(stack, level(result.get("blockActiveMinLevel", (cts or {}).get("blockActiveMinLevel", 0)), 0))
    def sl_min_pct(value, fallback=SL_MIN_PCT):
        try:
            n = float(fallback if value is None else value)
        except (TypeError, ValueError, OverflowError):
            n = fallback
        if not math.isfinite(n):
            n = fallback
        return max(SL_MIN_PCT, min(3.0, n))
    result["slMinPct"] = sl_min_pct(result.get("slMinPct"))
    result["indStopMinPct"] = sl_min_pct(result.get("indStopMinPct"))
    try:
        sl_pct = float(result["slMinPct"] if result.get("slPct") is None else result.get("slPct"))
        if not math.isfinite(sl_pct):
            sl_pct = result["slMinPct"]
    except (TypeError, ValueError, OverflowError):
        sl_pct = result["slMinPct"]
    result["slPct"] = max(result["slMinPct"], sl_pct)
    result.update(normalize_system_settings(result))
    return result
