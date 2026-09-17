#!/usr/bin/env python3
"""Credential-free remote snapshot. Never prints secrets or flattens."""
from __future__ import annotations

import glob
import json
import os
import subprocess
import time

now = time.time()
out = {
    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "units": {},
    "lanes": {},
}
for unit, key in (
    ("cts-ga-pulse@bingx-x01", "x01"),
    ("cts-ga-pulse@bingx-x02", "x02"),
    ("cts-ga-pulse-http", "http"),
):
    try:
        out["units"][key] = subprocess.check_output(["systemctl", "is-active", unit], text=True).strip()
    except Exception:
        out["units"][key] = "unknown"
for path in sorted(glob.glob("/var/lib/cts-ga/stats-bingx-x0*.json")):
    try:
        data = json.load(open(path))
    except Exception as exc:
        out["lanes"][os.path.basename(path)] = {"err": type(exc).__name__}
        continue
    ht = data.get("histTest") if isinstance(data.get("histTest"), dict) else {}
    sets = data.get("sets") if isinstance(data.get("sets"), dict) else {}
    system = data.get("system") if isinstance(data.get("system"), dict) else {}
    out["lanes"][os.path.basename(path).replace("stats-", "").replace(".json", "")] = {
        "age": round(now - os.stat(path).st_mtime, 1),
        "mode": data.get("mode"),
        "open": data.get("openCount"),
        "exch": data.get("exchangeOpenCount"),
        "liveOrd": data.get("liveOrderCount"),
        "equity": data.get("equity"),
        "avail": data.get("available"),
        "used": data.get("usedMargin"),
        "hist": ht.get("enabled"),
        "phase": ht.get("phase"),
        "intern": len(ht.get("internSymbols") or []),
        "runSets": len(ht.get("runningSets") or []),
        "valid": sets.get("validatedCount"),
        "active": sets.get("activeCount"),
        "rss": data.get("rssMb") or system.get("rssMb"),
        "detail": str(ht.get("detail") or "")[:120],
    }
try:
    info = {}
    for line in open("/proc/meminfo"):
        if line.startswith(("MemTotal", "MemAvailable")):
            info[line.split(":")[0]] = int(line.split()[1])
    out["memAvailMb"] = round(info.get("MemAvailable", 0) / 1024, 1)
    out["memTotalMb"] = round(info.get("MemTotal", 0) / 1024, 1)
except Exception:
    pass
try:
    out["logKb"] = int(subprocess.check_output(["du", "-sk", "/var/log/cts-ga"], text=True).split()[0])
except Exception:
    pass
print(json.dumps(out))
