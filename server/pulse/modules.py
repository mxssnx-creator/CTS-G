"""Generic module registry. Core never hard-codes a venue or pack."""
from __future__ import annotations

from typing import Any, Dict

LAYERS = ("core", "feed", "exchange", "strategy", "risk", "exec", "desk")

DEFAULT_FLAGS = {
    "core.engine": True,
    "core.historic": True,
    "core.load": True,
    "strategy.sets": True,
    "strategy.exits": True,
    "feed.ws": True,
    "feed.rest": True,
    "exchange.bingx": True,
    "strategy.block": True,
    "strategy.trailing": True,
    "strategy.dca": False,  # DCA off by default — overlay dcaEnabled=true turns it on
    "strategy.indications": True,
    "strategy.coord": True,
    "feed.signals": True,
    "feed.tf1m": True,
    "feed.tf5m": True,
    "feed.tf15m": True,
    "feed.tfCombined": True,
    "strategy.rearrange": True,
    "risk.equityFloor": True,
    "risk.notionalCap": True,
    "risk.slTpRatios": True,
    "strategy.trailRecalc": True,
    "exec.controls": True,
    "exec.batch": True,
}


def _enabled(value: Any, default: bool = True) -> bool:
    if value in (False, 0, "false", "0", "off"):
        return False
    if value in (True, 1, "true", "1", "on"):
        return True
    return default


def range_processing(overlay: Dict[str, Any], range_name: str = "") -> Dict[str, bool]:
    ranges = overlay.get("strategyProcessingByRange")
    own = ranges.get(range_name, {}) if isinstance(ranges, dict) else {}
    own = own if isinstance(own, dict) else {}
    mods = overlay.get("modules") if isinstance(overlay.get("modules"), dict) else {}
    return {"normal": _enabled(overlay.get("stratNormal")) and _enabled(own.get("normal")),
            "trailing": _enabled(overlay.get("stratTrailing")) and _enabled(mods.get("strategy.trailing")) and _enabled(own.get("trailing"))}


def trailing_enabled(overlay: Dict[str, Any], range_name: str = "") -> bool:
    """An overall or exact-range disable always wins."""
    return range_processing(overlay, range_name)["trailing"]


def additional_processing_enabled(overlay: Dict[str, Any]) -> bool:
    """Configured adjustments retain their parent's calculated evidence with Normal off."""
    mods=overlay.get("modules") if isinstance(overlay.get("modules"),dict) else {}
    axis=_enabled(mods.get("strategy.coord")) and any(_enabled(overlay.get("axis"+name+"Enabled")) for name in ("Prev","Last","Cont","Pause"))
    return (axis or _enabled(mods.get("strategy.block")) and _enabled(overlay.get("blockEnabled"),False) and _enabled(overlay.get("stratBlock"))
            or _enabled(mods.get("strategy.dca")) and _enabled(overlay.get("dcaEnabled"),False) and _enabled(overlay.get("stratDca")) and (_enabled(overlay.get("dcaOnSets"),False) or axis and _enabled(overlay.get("dcaOnAxis"),False)))


def resolve(overlay: Dict[str, Any]) -> Dict[str, bool]:
    flags = dict(DEFAULT_FLAGS)
    mods = overlay.get("modules") if isinstance(overlay.get("modules"), dict) else {}
    for k, v in mods.items():
        flags[str(k)] = bool(v)
    if "blockEnabled" in overlay:
        flags["strategy.block"] = bool(overlay["blockEnabled"])
    # DCA is an opt-in lane: off unless the overlay switches it on (and the build-time kill switch is not set)
    from dca_engine import DCA_HARD_OFF
    flags["strategy.dca"] = bool(overlay.get("dcaEnabled", False)) and not DCA_HARD_OFF
    if "controlOrders" in overlay:
        flags["exec.controls"] = bool(overlay["controlOrders"])
    if "rearrange" in overlay:
        flags["strategy.rearrange"] = bool(overlay["rearrange"])
    if "indEnabled" in overlay:
        flags["strategy.indications"] = bool(overlay["indEnabled"])
    if "tf1m" in overlay:
        flags["feed.tf1m"] = bool(overlay["tf1m"])
    if "tf5m" in overlay:
        flags["feed.tf5m"] = bool(overlay["tf5m"])
    if "tf15m" in overlay:
        flags["feed.tf15m"] = bool(overlay["tf15m"])
    if "tfCombined" in overlay:
        flags["feed.tfCombined"] = bool(overlay["tfCombined"])
    if "slToTpAuto" in overlay:
        flags["risk.slTpRatios"] = True
    if "trailAuto" in overlay:
        flags["strategy.trailRecalc"] = bool(overlay.get("trailAuto", True))
    flags["strategy.trailing"] = trailing_enabled(overlay)
    flags["strategy.trailRecalc"] = flags["strategy.trailRecalc"] and flags["strategy.trailing"]
    if "exitEnabled" in overlay:
        flags["strategy.exits"] = bool(overlay["exitEnabled"])
    if "histEnabled" in overlay:
        flags["core.historic"] = bool(overlay["histEnabled"])
    return flags
