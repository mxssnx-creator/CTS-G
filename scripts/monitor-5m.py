#!/usr/bin/env python3
"""5-minute remote monitor + bounded auto-fix. Never flattens. Never arms 8581."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "monitor-5m"
KEY_CANDIDATES = [
    os.environ.get("CTS_BACKUP_SSH_KEY", ""),
    "/workspace/attachments/snet-ln-deb01.txt",
    str(Path.home() / ".ssh" / "snet-ln-deb01.txt"),
]
HOST = os.environ.get("CTS_BACKUP_HOST", "152.53.114.112")
MAJORS = {
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT", "DOGE-USDT",
    "ADA-USDT", "BCH-USDT", "AVAX-USDT", "LINK-USDT", "LTC-USDT", "DOT-USDT",
    "UNI-USDT", "ATOM-USDT", "NEAR-USDT", "APT-USDT", "ARB-USDT", "SUI-USDT",
    "INJ-USDT", "AAVE-USDT", "FIL-USDT", "OP-USDT", "TRX-USDT", "XLM-USDT",
    "ETC-USDT", "LDO-USDT", "HBAR-USDT", "TIA-USDT", "WLD-USDT", "JUP-USDT",
    "RENDER-USDT", "FET-USDT", "TAO-USDT", "SEI-USDT", "WIF-USDT", "1000PEPE-USDT",
    "STX-USDT", "IMX-USDT", "GRT-USDT", "ALGO-USDT", "VET-USDT", "EOS-USDT",
    "THETA-USDT", "AXS-USDT", "SAND-USDT", "MANA-USDT", "CRV-USDT", "MKR-USDT",
    "SNX-USDT", "COMP-USDT",
}
UNITS = (
    "cts-ga-pulse-http",
    "cts-ga-desk",
    "cts-ga-pulse@bingx-x01",
    "cts-ga-pulse@bingx-x02",
)
SL_MIN = 0.4


def ssh_key() -> str:
    for raw in KEY_CANDIDATES:
        if raw and Path(raw).is_file():
            return raw
    raise SystemExit("no ssh key")


def ssh(script: str, timeout: int = 40) -> str:
    key = ssh_key()
    proc = subprocess.run(
        [
            "ssh", "-i", key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=12", "-o", "IdentitiesOnly=yes", f"root@{HOST}",
            script,
        ],
        text=True, capture_output=True, timeout=timeout,
    )
    if proc.returncode != 0:
        return json.dumps({"error": (proc.stderr or proc.stdout or "")[-800:]})
    return proc.stdout or ""


REMOTE_PY = r"""
python3 - <<'PY'
import json, os, glob, time, subprocess, urllib.request
now = time.time()
MAJORS = set(%s)
UNITS = %s
SL_MIN = %s
out = {"at": time.strftime("%%Y-%%m-%%dT%%H:%%M:%%SZ", time.gmtime()), "units": {}, "fix": [], "issues": []}
for u in UNITS:
    st = subprocess.check_output(["systemctl", "is-active", u], text=True).strip()
    out["units"][u] = st
    if st != "active":
        subprocess.run(["systemctl", "start", u], check=False)
        out["fix"].append("started " + u + " was=" + st)
        out["issues"].append("unit-down " + u)
sha = subprocess.check_output(["git", "-C", "/opt/cts-ga", "rev-parse", "--short", "HEAD"], text=True).strip()
out["sha"] = sha
for name in ("overlay-bingx-x01.json", "overlay-bingx-x02.json"):
    path = "/var/lib/cts-ga/" + name
    blob = json.load(open(path))
    changed = []
    for key, floor in (("slMinPct", SL_MIN), ("indStopMinPct", SL_MIN)):
        try:
            cur = float(blob.get(key) or 0)
        except Exception:
            cur = 0
        if cur < floor:
            blob[key] = floor
            changed.append(key)
    try:
        sl = float(blob.get("slPct") or 0)
        if sl and sl < float(blob.get("slMinPct") or SL_MIN):
            blob["slPct"] = blob["slMinPct"]
            changed.append("slPct")
    except Exception:
        pass
    if changed:
        tmp = path + ".tmp"
        open(tmp, "w").write(json.dumps(blob, indent=2) + "\n")
        os.replace(tmp, path)
        out["fix"].append("floor " + name + " " + ",".join(changed))
        out["issues"].append("sl-floor " + name)
    out[name] = {"slMin": blob.get("slMinPct"), "indStopMin": blob.get("indStopMinPct"), "tpMin": blob.get("tpMinPct")}
lanes = {}
for path in sorted(glob.glob("/var/lib/cts-ga/stats-bingx-x0*.json")):
    d = json.load(open(path))
    ht = d.get("histTest") or {}
    sets = d.get("sets") or {}
    intern_syms = [str(s).upper() for s in (ht.get("internSymbols") or [])]
    junk = [s for s in intern_syms if s.endswith("-USDT") and s not in MAJORS]
    intern_n = int(sets.get("internSetCount") or 0)
    valid_n = int(sets.get("validatedCount") or 0)
    row = {
        "age": round(now - os.stat(path).st_mtime, 1),
        "running": d.get("running"),
        "halted": d.get("halted"),
        "open": d.get("openCount"),
        "exch": d.get("exchangeOpenCount"),
        "eq": d.get("walletEquity") or d.get("equity"),
        "intern": intern_n,
        "validated": valid_n,
        "proc": sets.get("processingCount"),
        "active": sets.get("activeCount"),
        "ht": ht.get("phase"),
        "internSym": len(intern_syms),
        "junk": junk[:8],
        "detail": str(ht.get("detail") or "")[:160],
    }
    if junk:
        out["issues"].append("intern-junk " + os.path.basename(path) + " " + ",".join(junk[:6]))
    if intern_n and valid_n and intern_n == valid_n and intern_n > 100:
        out["issues"].append("intern-aliased-validated " + os.path.basename(path))
    if d.get("halted"):
        out["issues"].append("halted " + os.path.basename(path))
    if d.get("running") is not True:
        out["issues"].append("not-running " + os.path.basename(path))
    lanes[os.path.basename(path).replace("stats-", "").replace(".json", "")] = row
out["lanes"] = lanes
journal = subprocess.run(
    ["journalctl", "-u", "cts-ga-pulse@bingx-x01", "-u", "cts-ga-pulse@bingx-x02", "-u", "cts-ga-pulse-http",
     "--since", "8 min ago", "--no-pager", "-o", "cat"],
    capture_output=True, text=True,
).stdout
hits = [ln[:180] for ln in journal.splitlines() if any(k in ln.lower() for k in ("traceback", "exception", "fatal", "oom killer"))]
out["journalHits"] = hits[-12:]
if hits:
    out["issues"].append("journal " + str(len(hits)))
print(json.dumps(out))
PY
""" % (repr(sorted(MAJORS)), repr(list(UNITS)), repr(SL_MIN))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    raw = ssh(REMOTE_PY, timeout=50)
    try:
        blob = json.loads(raw)
    except Exception:
        blob = {"error": raw[-800:], "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    (OUT / f"{stamp}.json").write_text(json.dumps(blob, indent=2) + "\n", encoding="utf-8")
    (OUT / "latest.json").write_text(json.dumps(blob, indent=2) + "\n", encoding="utf-8")
    issues = blob.get("issues") or []
    print(json.dumps({"at": blob.get("at"), "sha": blob.get("sha"), "issues": issues, "fix": blob.get("fix"), "lanes": blob.get("lanes"), "units": blob.get("units")}, default=str))
    return 1 if issues or blob.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
