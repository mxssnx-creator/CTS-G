#!/usr/bin/env python3
"""Apply system defaults: 32h lookback, paper-historic, maxSl 0.8, minPf 1.3, x01 25 symbols, min volume."""
import json
import re
from pathlib import Path

root = Path(__file__).resolve().parents[1]
engine = root / "server/pulse/set_engine.py"
text = engine.read_text()

text2, n = re.subn(r"LOOKBACK_DEFAULT = \d+", "LOOKBACK_DEFAULT = 1920  # 32 h of 1m bars", text, count=1)
text = text2
print(f"LOOKBACK_DEFAULT replacements: {n}")

text = text.replace(
    "gb.lookback_default_ok = (LOOKBACK_DEFAULT == 4320 and SetBook().lookback == 4320)",
    "gb.lookback_default_ok = (LOOKBACK_DEFAULT == 1920 and SetBook().lookback == 1920)",
)

old = """        if self._history_snapshot_promoting:
            return "history"
        if self._lookback_pending:
            if not self._history_snapshot_usable():
                return "history"
            oldest = min(st.evaluated_at, self._history_snapshot_oldest_at)
            if oldest <= 0 or time.time() - oldest > self._snapshot_max_age():
                return "stale"
        w = self.win(st)
        if st.gate_n < self.base_need(st):
            return "n<min"""
new = """        # Pre-historic / paper path: Base may validate from simulated closes while lookback grows.
        # Live fills not required once gate_n > 0. Stages/Real paper stay logical; Real->Live mirrors valid execution only.
        if self._history_snapshot_promoting and st.gate_n <= 0:
            return "history"
        if self._lookback_pending:
            if not self._history_snapshot_usable() and st.gate_n <= 0:
                return "history"
            if self._history_snapshot_usable():
                oldest = min(st.evaluated_at, self._history_snapshot_oldest_at)
                if oldest <= 0 or time.time() - oldest > self._snapshot_max_age():
                    return "stale"
        w = self.win(st)
        if st.gate_n < self.base_need(st):
            return "n<min"""
if "Pre-historic / paper path" not in text and "Base may validate from simulated" not in text:
    if old not in text:
        raise SystemExit("paper-historic anchor not found")
    text = text.replace(old, new, 1)
    print("paper-historic path applied")
else:
    print("paper-historic path already present")

engine.write_text(text)
print("wrote", engine)

COMMON = {
    "histEnabled": True,
    "setUseHistoricGate": True,
    "useHistoricGate": True,
    "histKeepLastComplete": False,
    "histLookbackAuto": True,
    "histLookbackBars": 1920,
    "histLookbackMax": 4320,
    "histMinBars": 120,
    "histRefreshS": 60,
    "realStagePaper": True,
    "stagesEnabled": True,
    "setGateMinTrades": 3,
    "setGateWindow": 40,
    "setGateMinN": 0,
    "maxSlRatio": 0.8,
    "slMaxPct": 0.8,
    "setSlRatios": [0.5, 0.65, 0.8],
    "slMaxByRange": {"long": 0.8, "wide": 0.8, "normal": 0.75, "short": 0.6, "minimal": 0.5, "micro": 0.4},
    "setMinStep": 5,
    "minStep": 8,
    "shortRangeStepMin": 5,
    "shortRangeStepMax": 8,
    "minSlDistancePct": 0.12,
    "slMinPct": 0.25,
    "tpMinPct": 0.4,
    "microMinSlDistancePct": 0.07,
    "microMinTrailDistancePct": 0.05,
    "trailArmPct": 0.36,
    "trailGivePct": 0.12,
    "trailGiveMin": 0.08,
    "holdAuto": True,
    "holdAutoMin": 8,
    "holdAutoMax": 40,
    "minPfStepAuto": True,
    "minPf": 1.3,
    "setMinPf": 1.3,
    "minPfMicro": 1.2,
    "minPfMicroN": 2,
    "minPfSignals": 2.5,
    "mainGateN": 20,
    "realGateN": 12,
    "exitSetsByPack": {"indications": False},
    "volumeByPack": {"general": 1.5, "indications": 0.0},
    "volumeByPackKind": {"general:base": 1.5, "general:exit": 0.4, "general:trail": 0.2, "indications:base": 0.0},
    "stratIndications": False,
    "maxEntrySlipPct": 2.0,
}
X01 = {**COMMON, "symbolCap": 25, "volumeFactor": 0.01, "targetNotional": 0.05, "sizeMaxNotional": 0.4, "sizePctOfBalance": 5}
X02 = {**COMMON, "symbolCap": 70}
for name, patch in [("overlay-bingx-x01.json", X01), ("overlay-bingx-x02.json", X02)]:
    path = root / "server/pulse" / name
    d = json.loads(path.read_text())
    d.update(patch)
    path.write_text(json.dumps(d, indent=2) + "\n")
    print("updated", path.name, "cap", d.get("symbolCap"), "maxSl", d.get("maxSlRatio"), "vol", d.get("volumeFactor"))
print("done")
