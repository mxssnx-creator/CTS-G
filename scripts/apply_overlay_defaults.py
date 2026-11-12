#!/usr/bin/env python3
"""Apply optimized overlay defaults (2026-11-12): paper-historic validation, max SL 1.0, positive-leaning packs."""
import json
from pathlib import Path

COMMON = {
    "histEnabled": True,
    "setUseHistoricGate": True,
    "useHistoricGate": True,
    "histKeepLastComplete": False,
    "histLookbackAuto": True,
    "histLookbackBars": 960,
    "histLookbackMax": 4320,
    "histMinBars": 120,
    "histRefreshS": 60,
    "realStagePaper": True,
    "stagesEnabled": True,
    "setGateMinTrades": 1,
    "setGateWindow": 40,
    "setGateMinN": 0,
    "maxSlRatio": 1.0,
    "slMaxPct": 1.0,
    "setSlRatios": [0.5, 0.75, 1.0],
    "slMaxByRange": {"long": 0.75, "wide": 0.75, "normal": 0.75, "short": 0.6, "minimal": 0.5, "micro": 0.4},
    "setMinStep": 3,
    "minStep": 8,
    "shortRangeStepMin": 3,
    "shortRangeStepMax": 8,
    "minSlDistancePct": 0.12,
    "slMinPct": 0.25,
    "tpMinPct": 0.4,
    "microMinSlDistancePct": 0.07,
    "microMinTrailDistancePct": 0.05,
    "trailArmPct": 0.36,
    "trailGivePct": 0.12,
    "trailGiveMin": 0.08,
    "trailArmMax": 1.2,
    "trailGiveMax": 0.45,
    "trailMinStep": 2,
    "trailingMinStep": 4,
    "trailStepMax": 18,
    "holdAuto": True,
    "holdAutoMin": 8,
    "holdAutoMax": 40,
    "minPfStepAuto": True,
    "minPfStepAutoHigh": 1.2,
    "minPfStepAutoN": 8,
    "minPfStepRatio": 0.15,
    "minPf": 1.15,
    "setMinPf": 1.15,
    "minPfMicro": 1.01,
    "minPfMicroN": 2,
    "minPfShort": 1.12,
    "minPfShortN": 12,
    "minPfSignals": 2.2,
    "mainGateN": 20,
    "realGateN": 12,
    "pfWindow": 10,
    "setPfWindow": 10,
    "exitSetsByPack": {"indications": False},
    "volumeByPack": {"general": 1.25, "indications": 0.75},
    "volumeByPackKind": {"general:base": 1.5, "general:exit": 1.25, "indications:base": 0.7},
}

X01 = {
    **COMMON,
    "symbolCap": 30,
    "volumeFactor": 0.01,
    "targetNotional": 0.05,
    "sizeMaxNotional": 1.5,
    "sizePctOfBalance": 2,
}

X02 = {**COMMON}

def apply(path: Path, patch: dict) -> None:
    d = json.loads(path.read_text())
    d.update(patch)
    path.write_text(json.dumps(d, indent=2) + chr(10))
    print(f"updated {path}")

root = Path(__file__).resolve().parents[1]
apply(root / "server/pulse/overlay-bingx-x01.json", X01)
apply(root / "server/pulse/overlay-bingx-x02.json", X02)
