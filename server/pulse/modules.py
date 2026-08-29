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
    "strategy.dca": True,
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


def resolve(overlay: Dict[str, Any]) -> Dict[str, bool]:
    flags = dict(DEFAULT_FLAGS)
    mods = overlay.get("modules") if isinstance(overlay.get("modules"), dict) else {}
    for k, v in mods.items():
        flags[str(k)] = bool(v)
    if "blockEnabled" in overlay:
        flags["strategy.block"] = bool(overlay["blockEnabled"])
    if "dcaEnabled" in overlay:
        flags["strategy.dca"] = bool(overlay["dcaEnabled"])
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
    if "stratTrailing" in overlay:
        flags["strategy.trailing"] = bool(overlay["stratTrailing"])
        if "trailAuto" not in overlay:
            flags["strategy.trailRecalc"] = bool(overlay["stratTrailing"])
    if "exitEnabled" in overlay:
        flags["strategy.exits"] = bool(overlay["exitEnabled"])
    if "histEnabled" in overlay:
        flags["core.historic"] = bool(overlay["histEnabled"])
    return flags
