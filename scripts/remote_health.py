#!/usr/bin/env python3
"""Remote CTS-G health snapshot + bounded auto-fix. Never flattens positions."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

UNITS = (
    "cts-ga-pulse-http",
    "cts-ga-desk",
    "cts-ga-pulse@bingx-x01",
    "cts-ga-pulse@bingx-x02",
)
DATA = Path("/var/lib/cts-ga")
PULSE = "http://127.0.0.1:3015"
FLOORS = {
    "minStep": 7,
    "setMinStep": 7,
    "trailingMinStep": 7,
    "slMinPct": 0.4,
    "indStopMinPct": 0.4,
    "minPf": 1.15,
    "histTestMinPf": 1.15,
    "symbolCap": 50,
    "histTestTargetCount": 50,
    "histTestValidateCap": 250,
}


def run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, text=True, capture_output=True)
    return (proc.stdout or proc.stderr or "").strip()


def get_json(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"error": str(exc)}


def patch_overlays() -> list[str]:
    notes = []
    for name in ("overlay-bingx-x01.json", "overlay-bingx-x02.json"):
        path = DATA / name
        if not path.is_file():
            notes.append(f"missing {name}")
            continue
        blob = json.loads(path.read_text(encoding="utf-8"))
        changed = []
        for key, floor in FLOORS.items():
            cur = blob.get(key)
            try:
                num = float(cur) if not isinstance(floor, str) else cur
            except (TypeError, ValueError):
                num = 0
            need = floor
            if isinstance(floor, float):
                if float(num or 0) < floor:
                    blob[key] = floor
                    changed.append(key)
            else:
                if int(num or 0) < int(floor):
                    blob[key] = int(floor)
                    changed.append(key)
        if name.endswith("x01.json"):
            blob["histTestEnabled"] = True
            blob["symbolsAll"] = True
            blob["symbolsDynamic"] = True
            blob["symbolCap"] = 50
        if changed:
            tmp = str(path) + ".tmp"
            Path(tmp).write_text(json.dumps(blob, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, path)
            notes.append(f"patched {name} {changed}")
        else:
            notes.append(f"ok {name}")
    return notes


def ensure_units() -> list[str]:
    notes = []
    for unit in UNITS:
        state = run(["systemctl", "is-active", unit])
        if state != "active":
            subprocess.run(["systemctl", "start", unit], check=False)
            notes.append(f"started {unit} was={state}")
        else:
            notes.append(f"active {unit}")
    return notes


def lane_brief(conn: str) -> dict:
    d = get_json(f"{PULSE}/stats.json?conn={conn}")
    opens = d.get("open") if isinstance(d.get("open"), list) else []
    miss = 0
    for row in opens:
        if not isinstance(row, dict):
            continue
        if not (row.get("sl") or row.get("slOid")) or not (row.get("tp") or row.get("tpOid")):
            miss += 1
    load = d.get("load") if isinstance(d.get("load"), dict) else {}
    sets = d.get("sets") if isinstance(d.get("sets"), dict) else {}
    ht = d.get("histTest") if isinstance(d.get("histTest"), dict) else {}
    return {
        "error": d.get("error"),
        "running": d.get("running"),
        "paused": d.get("paused"),
        "halted": d.get("halted"),
        "open": d.get("openCount"),
        "scan": len(d.get("symbols") or []),
        "cap": d.get("symbolCap"),
        "missSlTp": miss,
        "load": load.get("level"),
        "tf15m": load.get("tf15m"),
        "rss": load.get("rssMb"),
        "setsActive": sets.get("activeCount"),
        "ht": ht.get("phase") or ht.get("enabled"),
        "equity": d.get("walletEquity") or d.get("equity"),
    }


def main() -> int:
    fix = "--fix" in sys.argv
    notes = []
    if fix:
        notes.extend(ensure_units())
        notes.extend(patch_overlays())
    ht = get_json(f"{PULSE}/hist-test.json")
    out = {
        "units": {u: run(["systemctl", "is-active", u]) for u in UNITS},
        "live": lane_brief("live"),
        "vst": lane_brief("vst"),
        "hist": {
            "phase": ht.get("phase"),
            "pct": ht.get("pct"),
            "detail": ht.get("detail"),
            "filled": ht.get("filled"),
            "target": ht.get("targetCount"),
            "running": ht.get("running"),
            "error": ht.get("error"),
        },
        "fix": notes,
    }
    print(json.dumps(out, default=str))
    live = out["live"]
    bad = (
        out["units"].get("cts-ga-pulse@bingx-x01") != "active"
        or out["units"].get("cts-ga-pulse-http") != "active"
        or live.get("running") is not True
        or live.get("halted") is True
        or int(live.get("missSlTp") or 0) > 0
        or live.get("load") in ("critical",)
        or (live.get("tf15m") is False and int(live.get("scan") or 0) <= 64)
    )
    return 2 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
