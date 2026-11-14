#!/usr/bin/env python3
"""Patch set_engine so maxSlRatio caps every range and the ratio grid."""
from pathlib import Path
engine = Path(__file__).resolve().parents[1] / "server/pulse/set_engine.py"
text = engine.read_text()
changed = False
if "Global maxSlRatio: hard cap" not in text:
    old = '        self.sl_max_by_range = sl_max_by_range_of(ov.get("slMaxByRange"))'
    new = (
        '        self.sl_max_by_range = sl_max_by_range_of(ov.get("slMaxByRange"))\n'
        '        # Global maxSlRatio: hard cap on every SL ratio and every range limit (owner 2026-11-14).\n'
        '        try:\n'
        '            _msr = float(ov.get("maxSlRatio") or 0)\n'
        '        except (TypeError, ValueError):\n'
        '            _msr = 0.0\n'
        '        if math.isfinite(_msr) and _msr > 0:\n'
        '            self.max_sl_ratio = max(0.2, min(3.5, _msr))\n'
        '            _names = {n for n, _lo, _hi in TP_RANGES}\n'
        '            for _rn in _names:\n'
        '                _cur = self.sl_max_by_range.get(_rn)\n'
        '                if _cur is None:\n'
        '                    self.sl_max_by_range[_rn] = self.max_sl_ratio\n'
        '                else:\n'
        '                    self.sl_max_by_range[_rn] = min(float(_cur), self.max_sl_ratio)\n'
        '        else:\n'
        '            self.max_sl_ratio = 0.0'
    )
    if old not in text:
        raise SystemExit("anchor missing: sl_max_by_range_of")
    text = text.replace(old, new, 1)
    changed = True
if "Enforce maxSlRatio on the ratio grid itself" not in text:
    old = "        self.sl_ratios = sorted(set(ratios)) or list(SL_RATIOS)"
    new = (
        "        self.sl_ratios = sorted(set(ratios)) or list(SL_RATIOS)\n"
        "        # Enforce maxSlRatio on the ratio grid itself (not only per-range at build time)\n"
        '        if getattr(self, "max_sl_ratio", 0) and self.max_sl_ratio > 0:\n'
        "            self.sl_ratios = [r for r in self.sl_ratios if r <= self.max_sl_ratio + 1e-9] or [self.max_sl_ratio]"
    )
    if old not in text:
        raise SystemExit("anchor missing: sl_ratios")
    text = text.replace(old, new, 1)
    changed = True
if "self.max_sl_ratio: float = 0.0" not in text:
    old = "        self.sl_max_by_range: Dict[str, float] = dict(SL_MAX_BY_RANGE_DEFAULT)"
    if old in text:
        text = text.replace(old, old + "\n        self.max_sl_ratio: float = 0.0", 1)
        changed = True
engine.write_text(text)
print("patched" if changed else "already present")
