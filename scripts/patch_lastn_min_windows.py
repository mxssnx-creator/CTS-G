#!/usr/bin/env python3
"""Add mainMinWindows / setGateMinWindows candidates 1..4 step 1; largest passing wins."""
from pathlib import Path
engine = Path(__file__).resolve().parents[1] / "server/pulse/set_engine.py"
text = engine.read_text()
changed = False
if "main_min_windows: Tuple[int, ...] = ()" not in text:
    old = "    main_min_n: int = 0             # Main: the minimal window (mainMinN, 0 = off): the newest 1-3 results of the Main Set must all be positive / negative"
    new = old + "\n    main_min_windows: Tuple[int, ...] = ()  # mainMinWindows 1..4: candidates; largest passing wins\n    base_min_windows: Tuple[int, ...] = ()  # setGateMinWindows 1..4"
    if old not in text:
        raise SystemExit("StageParams anchor missing")
    text = text.replace(old, new, 1)
    changed = True
if "self.main_min_windows" not in text:
    old = '        self.main_min_n = max(0, min(MAIN_MIN_N_MAX, int(num(ov, "mainMinN", 0)))) if self.main_on else 0'
    new = old + """
        def _min_windows(raw, fallback_n):
            ws = parse_windows(raw, ())
            ws = tuple(w for w in ws if 1 <= w <= MAIN_MIN_N_MAX)
            if ws:
                return ws
            return (fallback_n,) if fallback_n else ()
        self.main_min_windows = _min_windows(ov.get("mainMinWindows"), self.main_min_n) if self.main_on else ()
        self.base_min_windows = _min_windows(ov.get("setGateMinWindows"), self.base_min_n) if self.stages_on else ()
        if self.main_min_windows and not self.main_min_n:
            self.main_min_n = max(self.main_min_windows)
        if self.base_min_windows and not self.base_min_n:
            self.base_min_n = max(self.base_min_windows)"""
    if old not in text:
        raise SystemExit("load main_min_n missing")
    text = text.replace(old, new, 1)
    changed = True
if "main_min_windows=self.main_min_windows" not in text:
    old = "full_window=self.stage_full, main_min_n=_main_min, main_min_positive=self.main_min_positive,"
    new = "full_window=self.stage_full, main_min_n=_main_min, main_min_windows=self.main_min_windows or ((_main_min,) if _main_min else ()), base_min_windows=self.base_min_windows or ((_base_min,) if _base_min else ()), main_min_positive=self.main_min_positive,"
    if old not in text:
        raise SystemExit("stage_params missing")
    text = text.replace(old, new, 1)
    changed = True
if "    _min_ws = tuple(P.main_min_windows)" not in text:
    old = """    if P.main_min_n:
        mw = list(main[-P.main_min_n:])
        k_n = len(mw)
        k_ok = k_n >= (P.main_min_n if P.full_window or P.disjoint else 1) and sign_ok(mw, P.main_min_positive, P.main_min_both, P.main_min_avg)
    else:
        k_ok, k_n = True, 0"""
    new = """    _min_ws = tuple(P.main_min_windows) if getattr(P, "main_min_windows", ()) else ((P.main_min_n,) if P.main_min_n else ())
    if _min_ws:
        k_ok, k_n = False, 0
        for _w in sorted(_min_ws):
            mw = list(main[-_w:]) if _w > 0 else []
            need = _w if P.full_window or P.disjoint else 1
            if len(mw) >= need and sign_ok(mw, P.main_min_positive, P.main_min_both, P.main_min_avg):
                k_ok, k_n = True, len(mw)
        if not k_ok:
            k_n = max(_min_ws) if _min_ws else 0
    else:
        k_ok, k_n = True, 0"""
    if old not in text:
        raise SystemExit("verdicts block missing")
    text = text.replace(old, new, 1)
    changed = True
engine.write_text(text)
print("patched" if changed else "already present")
