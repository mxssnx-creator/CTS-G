#!/usr/bin/env python3
"""Apply defaults: maxSl 0.8, full last-N long/short/last, min windows 1-4."""
import json, re, math
from pathlib import Path
root = Path(__file__).resolve().parents[1]
import runpy
runpy.run_path(str(root / "scripts/patch_max_sl_ratio.py"))
runpy.run_path(str(root / "scripts/patch_lastn_base_min.py"))
runpy.run_path(str(root / "scripts/patch_lastn_min_windows.py"))
engine = root / "server/pulse/set_engine.py"
text = engine.read_text()
text, n = re.subn(r"LOOKBACK_DEFAULT = \d+", "LOOKBACK_DEFAULT = 1920  # 32 h of 1m bars", text, count=1)
print("LOOKBACK_DEFAULT", n)
engine.write_text(text)
COMMON = {
  "stagesEnabled": True, "setGateWindow": 40, "setGateShortN": 16, "setGateMinTrades": 5,
  "setGateMinN": 2, "setGateMinWindows": [1, 2, 3, 4], "setGateMinMode": "positive",
  "mainGateEnabled": True, "mainGateN": 20, "mainShortEnabled": True, "mainShortWindows": [5, 8, 12, 16],
  "mainShortMin": 3, "mainMinN": 2, "mainMinWindows": [1, 2, 3, 4], "mainMinMode": "positive",
  "mainNegEnabled": True, "mainNegWindows": [5, 10], "realGateEnabled": True, "realGateN": 12,
  "lastNByRange": {
    "micro": {"baseLong": 30, "baseShort": 12, "baseMin": 2, "mainLong": 15, "mainShort": [4, 6, 8, 12], "mainMin": 2, "negWindows": [5, 10], "realN": 8, "realNegN": 8},
    "minimal": {"baseLong": 35, "baseShort": 14, "baseMin": 2, "mainLong": 18, "mainShort": [5, 8, 12], "mainMin": 2, "negWindows": [5, 10], "realN": 10, "realNegN": 10},
    "short": {"baseLong": 40, "baseShort": 16, "baseMin": 2, "mainLong": 20, "mainShort": [5, 8, 12, 16], "mainMin": 2, "negWindows": [5, 10], "realN": 12, "realNegN": 12},
    "normal": {"baseLong": 40, "baseShort": 18, "baseMin": 2, "mainLong": 22, "mainShort": [6, 10, 14], "mainMin": 2, "negWindows": [5, 10], "realN": 12, "realNegN": 12},
    "long": {"baseLong": 50, "baseShort": 20, "baseMin": 2, "mainLong": 25, "mainShort": [8, 12, 16], "mainMin": 2, "negWindows": [5, 10], "realN": 15, "realNegN": 15},
    "wide": {"baseLong": 50, "baseShort": 20, "baseMin": 2, "mainLong": 25, "mainShort": [8, 12, 16], "mainMin": 2, "negWindows": [5, 10], "realN": 15, "realNegN": 15}
  },
  "histEnabled": True, "setUseHistoricGate": True, "histLookbackBars": 1440,
  "maxSlRatio": 0.8, "slMaxPct": 0.8, "setSlRatios": [0.5, 0.65, 0.8],
  "slMaxByRange": {"micro": 0.4, "minimal": 0.5, "short": 0.6, "normal": 0.75, "long": 0.8, "wide": 0.8},
  "minPf": 1.25, "setMinPf": 1.25, "minPfMicro": 1.12, "minPfStepAuto": True, "minPfByStep": True,
  "minPfByRange": {"micro": 1.12, "minimal": 1.18, "short": 1.2, "normal": 1.25, "long": 1.3, "wide": 1.3},
  "groupGate": True,
  "tpRanges": {"micro": True, "minimal": True, "short": True, "normal": True, "long": True, "wide": True},
  "trailRanges": {"micro": False, "minimal": False, "short": False, "normal": False, "long": False, "wide": False},
  "stratIndications": False, "strategy.coord": False, "pauseMode": "off",
  "volumeByPack": {"general": 1.5, "indications": 0.0},
  "exitSetsByPack": {"indications": False, "general": False},
  "modules": {"strategy.coord": False, "strategy.trailing": False, "strategy.indications": False},
  "realStagePaper": True, "holdAuto": True, "setMinStep": 5,
}
X01 = {**COMMON, "symbolCap": 25, "volumeFactor": 0.05, "targetNotional": 0.08, "sizeMaxNotional": 0.5, "sizePctOfBalance": 10}
X02 = {**COMMON, "symbolCap": 70, "volumeFactor": 0.05}
for name, patch in [("overlay-bingx-x01.json", X01), ("overlay-bingx-x02.json", X02)]:
    path = root / "server/pulse" / name
    d = json.loads(path.read_text()); d.update(patch)
    path.write_text(json.dumps(d, indent=2) + "\n")
    print("updated", path.name, d["maxSlRatio"], d["setSlRatios"])
print("done")
