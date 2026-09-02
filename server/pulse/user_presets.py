#!/usr/bin/env python3
"""Named system presets shared across Live / VST (overall book)."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

MAX_PRESETS = 24
NAME_PREFIX = "Preset-"

WriteFn = Callable[[str, Dict[str, Any]], None]


def overview(ov: Optional[Dict[str, Any]]) -> str:
    ov = ov or {}
    sl = ov.get("slToTpRatio")
    try:
        sl_s = f"{float(sl):.1f}"
    except Exception:
        sl_s = "—"
    lo = ov.get("setMinStep")
    hi = ov.get("setStepMax")
    step = f"{lo}–{hi}" if lo is not None and hi is not None else "—"
    trail_on = bool(ov.get("stratTrailing", True))
    if trail_on:
        trail = f"{ov.get('trailArmPct', '—')}:{ov.get('trailGivePct', '—')}"
    else:
        trail = "off"
    block = "Block ON" if ov.get("blockEnabled", True) and ov.get("stratBlock", True) else "Block OFF"
    dca_on = bool(ov.get("dcaEnabled")) and ov.get("stratDca") is not False
    dca = "DCA ON" if dca_on else "DCA OFF"
    try:
        pf = float(ov.get("setMinPf") or ov.get("minPf") or 0)
        pf_s = f"{pf:.2f}"
    except Exception:
        pf_s = "—"
    dd = ov.get("setMaxDdTimeS") if ov.get("setMaxDdTimeS") is not None else ov.get("maxDdTimeS")
    try:
        dd_s = f"{int(float(dd))}s"
    except Exception:
        dd_s = "—"
    return f"SL {sl_s} · step {step} · trail {trail} · {block} · {dca} · PF {pf_s} · DDt {dd_s}"


def next_name(rows: List[Dict[str, Any]]) -> str:
    used = {str(r.get("name") or "") for r in rows}
    n = 1
    while f"{NAME_PREFIX}{n}" in used:
        n += 1
    return f"{NAME_PREFIX}{n}"


def normalize_name(raw: Any, rows: List[Dict[str, Any]], exclude_id: Optional[str] = None) -> str:
    s = re.sub(r"\s+", " ", str(raw or "").strip())
    if not s:
        s = next_name(rows)
    if not s.lower().startswith("preset-"):
        s = NAME_PREFIX + s
    s = s[:56]
    used = {str(r.get("name") or "") for r in rows if str(r.get("id") or "") != str(exclude_id or "")}
    if s not in used:
        return s
    base = s
    n = 2
    while f"{base}-{n}" in used:
        n += 1
    return f"{base}-{n}"[:56]


def _public(row: Dict[str, Any], include_overlay: bool = True) -> Dict[str, Any]:
    ov = row.get("overlay") if isinstance(row.get("overlay"), dict) else {}
    out = {
        "id": row.get("id"),
        "name": row.get("name"),
        "hint": row.get("hint") or overview(ov),
        "overview": row.get("overview") or overview(ov),
        "updated": row.get("updated") or 0,
        "created": row.get("created") or 0,
        "system": True,
    }
    if include_overlay:
        out["overlay"] = ov
        calc = row.get("calcOpt")
        if isinstance(calc, dict):
            out["calcOpt"] = calc
    return out


class UserPresetStore:
    def __init__(
        self,
        path: str,
        write_overlay: Optional[WriteFn] = None,
        lane_ids: Optional[List[str]] = None,
    ) -> None:
        self.path = path
        self.write_overlay = write_overlay
        self.lane_ids = list(lane_ids or [])
        self.lock = threading.Lock()

    def _read(self) -> List[Dict[str, Any]]:
        try:
            with open(self.path) as f:
                data = json.load(f)
        except Exception:
            return []
        if isinstance(data, dict):
            rows = data.get("presets")
        else:
            rows = data
        if not isinstance(rows, list):
            return []
        out = []
        for r in rows:
            if isinstance(r, dict) and r.get("id") and r.get("name"):
                out.append(r)
        return out

    def _write(self, rows: List[Dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        blob = {"presets": rows, "updated": time.time(), "system": True}
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(blob, f, separators=(",", ":"))
        os.replace(tmp, self.path)

    def list(self) -> List[Dict[str, Any]]:
        with self.lock:
            return [_public(r) for r in self._read()]

    def save(
        self,
        overlay: Dict[str, Any],
        name: str = "",
        calc_opt: Optional[Dict[str, Any]] = None,
        preset_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not isinstance(overlay, dict):
            overlay = {}
        with self.lock:
            rows = self._read()
            if preset_id:
                for r in rows:
                    if r.get("id") == preset_id:
                        r["overlay"] = overlay
                        r["name"] = normalize_name(name or r.get("name"), rows, exclude_id=preset_id)
                        r["overview"] = overview(overlay)
                        r["hint"] = r["overview"]
                        if isinstance(calc_opt, dict):
                            r["calcOpt"] = calc_opt
                        r["updated"] = time.time()
                        self._write(rows)
                        return _public(r)
            if len(rows) >= MAX_PRESETS:
                raise ValueError(f"max {MAX_PRESETS} system presets")
            now = time.time()
            row = {
                "id": "up-" + uuid.uuid4().hex[:10],
                "name": normalize_name(name, rows),
                "overlay": overlay,
                "overview": overview(overlay),
                "hint": overview(overlay),
                "calcOpt": calc_opt if isinstance(calc_opt, dict) else {},
                "created": now,
                "updated": now,
            }
            rows.append(row)
            self._write(rows)
            return _public(row)

    def rename(self, preset_id: str, name: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            rows = self._read()
            for r in rows:
                if r.get("id") == preset_id:
                    r["name"] = normalize_name(name, rows, exclude_id=preset_id)
                    r["updated"] = time.time()
                    self._write(rows)
                    return _public(r)
        return None

    def delete(self, preset_id: str) -> bool:
        with self.lock:
            rows = self._read()
            nxt = [r for r in rows if r.get("id") != preset_id]
            if len(nxt) == len(rows):
                return False
            self._write(nxt)
            return True

    def get(self, preset_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            for r in self._read():
                if r.get("id") == preset_id:
                    return _public(r)
        return None

    def apply(self, preset_id: str, apply_all: bool = True) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        row = self.get(preset_id)
        if not row:
            return None, []
        applied: List[str] = []
        ov = row.get("overlay") if isinstance(row.get("overlay"), dict) else {}
        if apply_all and self.write_overlay:
            for cid in self.lane_ids:
                try:
                    self.write_overlay(cid, ov)
                    applied.append(cid)
                except Exception:
                    pass
        return row, applied


def self_test(tmp: Optional[str] = None) -> List[Tuple[str, bool, str]]:
    import tempfile

    out: List[Tuple[str, bool, str]] = []
    def rec(n: str, ok: bool, d: str = "") -> None:
        out.append((n, bool(ok), d))

    path = tmp or os.path.join(tempfile.mkdtemp(), "user-presets.json")
    written: List[Tuple[str, Dict[str, Any]]] = []

    def write(cid: str, ov: Dict[str, Any]) -> None:
        written.append((cid, dict(ov)))

    store = UserPresetStore(path, write_overlay=write, lane_ids=["live-a", "vst-b"])
    rec("up-empty", store.list() == [], str(store.list()))
    ov = {
        "slToTpRatio": 0.3,
        "setMinStep": 12,
        "setStepMax": 18,
        "stratTrailing": True,
        "trailArmPct": 0.3,
        "trailGivePct": 0.1,
        "blockEnabled": True,
        "stratBlock": True,
        "dcaEnabled": False,
        "stratDca": False,
        "setMinPf": 1.12,
        "setMaxDdTimeS": 900,
    }
    rec("up-overview", "SL 0.3" in overview(ov) and "Block ON" in overview(ov) and "DCA OFF" in overview(ov), overview(ov))
    row = store.save(ov, name="")
    rec("up-auto-name", str(row.get("name")) == "Preset-1", str(row.get("name")))
    rec("up-prefix", str(row.get("name") or "").startswith("Preset-"), str(row.get("name")))
    rec("up-hint", "step 12–18" in str(row.get("overview")), str(row.get("overview")))
    row2 = store.save(ov, name="Tight Live")
    rec("up-prefix-custom", row2.get("name") == "Preset-Tight Live", str(row2.get("name")))
    rec("up-count", len(store.list()) == 2, str(len(store.list())))
    renamed = store.rename(row["id"], "Core Book")
    rec("up-rename", bool(renamed) and renamed.get("name") == "Preset-Core Book", str((renamed or {}).get("name")))
    loaded, applied = store.apply(row["id"], apply_all=True)
    rec("up-load", bool(loaded) and loaded.get("overlay", {}).get("slToTpRatio") == 0.3, str((loaded or {}).get("name")))
    rec("up-apply-all", applied == ["live-a", "vst-b"], str(applied))
    rec("up-apply-ov", written[0][1].get("slToTpRatio") == 0.3 if written else False, str(written[:1]))
    rec("up-delete", store.delete(row2["id"]) and len(store.list()) == 1, str(len(store.list())))
    rec("up-delete-miss", store.delete("nope") is False)
    rec("up-norm-empty", normalize_name("", []) == "Preset-1")
    rec("up-norm-dup", normalize_name("Preset-1", [{"id": "x", "name": "Preset-1"}]) == "Preset-1-2")
    return out


if __name__ == "__main__":
    rows = self_test()
    bad = [r for r in rows if not r[1]]
    print("fail" if bad else "ok", len(bad), "/", len(rows))
    for n, ok, d in rows:
        print(("OK  " if ok else "FAIL") + n, d)
