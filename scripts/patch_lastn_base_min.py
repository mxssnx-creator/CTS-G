#!/usr/bin/env python3
"""Enable Base/Main minimal last-N and per-range baseMin/mainMin independence."""
from pathlib import Path
engine = Path(__file__).resolve().parents[1] / "server/pulse/set_engine.py"
text = engine.read_text()
changed = False
old = """        for key, lo, hi in (("baseLong", 1, 200), ("baseShort", 0, 200), ("mainLong", 1, 200), ("realN", 1, 200), ("realNegN", 1, 200)):
            if key in o:
                try:
                    v = int(round(float(o[key])))
                except (TypeError, ValueError):
                    continue
                got[key] = max(lo, min(hi, v))"""
new = """        for key, lo, hi in (("baseLong", 1, 200), ("baseShort", 0, 200), ("mainLong", 1, 200), ("realN", 1, 200), ("realNegN", 1, 200),
                              ("baseMin", 0, 5), ("mainMin", 0, 5)):
            if key in o:
                try:
                    v = int(round(float(o[key])))
                except (TypeError, ValueError):
                    continue
                got[key] = max(lo, min(hi, v))"""
if '("baseMin", 0, 5)' not in text:
    if old not in text:
        raise SystemExit("anchor last_n keys missing")
    text = text.replace(old, new, 1)
    changed = True
if "Per-range last-N independence" not in text:
    old2 = """        w = self.win(st)
        return StageParams(base_long=w.base_long, base_short=w.base_short, base_min_n=self.base_min_n, base_min_positive=self.base_min_positive,"""
    new2 = """        w = self.win(st)
        # Per-range last-N independence: baseMin / mainMin from lastNByRange (or pack lanePolicy) else global setGateMinN / mainMinN
        _rng = tp_range_of(self.step_bucket(st)) if st is not None else ""
        _ln = {**self.last_n_by_range.get(_rng, {}), **policy.get("lastN", {})}
        _base_min = int(_ln["baseMin"]) if "baseMin" in _ln else self.base_min_n
        _main_min = int(_ln["mainMin"]) if "mainMin" in _ln else self.main_min_n
        _base_min = max(0, min(BASE_MIN_N_MAX, _base_min))
        _main_min = max(0, min(MAIN_MIN_N_MAX, _main_min)) if self.main_on else 0
        return StageParams(base_long=w.base_long, base_short=w.base_short, base_min_n=_base_min, base_min_positive=self.base_min_positive,"""
    if old2 not in text:
        raise SystemExit("anchor stage_params missing")
    text = text.replace(old2, new2, 1)
    changed = True
if "main_min_n=_main_min" not in text:
    old3 = "full_window=self.stage_full, main_min_n=self.main_min_n, main_min_positive=self.main_min_positive,"
    new3 = "full_window=self.stage_full, main_min_n=_main_min, main_min_positive=self.main_min_positive,"
    if old3 not in text:
        raise SystemExit("anchor main_min_n missing")
    text = text.replace(old3, new3, 1)
    changed = True
engine.write_text(text)
print("patched" if changed else "already present")
