#!/usr/bin/env python3
"""Apply paper-historic validation patch to set_engine.py (Base may validate from simulated closes)."""
from pathlib import Path
root = Path(__file__).resolve().parents[1]
engine = root / "server/pulse/set_engine.py"
text = engine.read_text()
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
new = """        # Pre-historic / paper path: a Set already scored on simulated closes may validate
        # while the full lookback snapshot is still building. Only block on \"history\" when
        # this Set has no paper sample yet (gate_n == 0). Live fills are not required.
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
if old not in text:
    if "Pre-historic / paper path" in text:
        print("already applied")
    else:
        raise SystemExit("anchor not found in set_engine.py")
else:
    engine.write_text(text.replace(old, new, 1))
    print("applied paper-historic validation to set_engine.py")
