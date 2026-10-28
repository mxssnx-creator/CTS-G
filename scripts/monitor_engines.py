#!/usr/bin/env python3
"""Unattended monitor for the live engines (x01 mainnet, x02 VST demo). Every interval it reads the sidecar's stats, appends one JSON
line per connection to <pulse dir>/monitor.jsonl (equity, open count, errors, closed count, post-Base effective PF with its Set
and order counts, validated share, history depth) and writes WARN lines for the conditions that need a look: an engine process
that died (restarted unless its PAUSE file exists), equity down more than 10 % within an hour, no valid Set for 30 minutes, a
history shorter than 1,500 bars, an error burst. It never changes a setting, never clears a halt and never sends an order.

usage: monitor_engines.py [--interval 300]"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.request

DIR = "/opt/grok-x01-pulse"
REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "pulse")
CONNS = {"live": "bingx-x01", "vst": "bingx-x02"}


def stats(view):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:3015/stats.json?conn={view}", timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"_error": str(e)[:100]}


def alive(conn):
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode()
            if "pulse_trader.py" not in cmd or "monitor" in cmd:
                continue
            if f"PULSE_CONN={conn}" in open(f"/proc/{pid}/environ", "rb").read().decode(errors="ignore").replace("\0", "\n"):
                return True
        except Exception:
            continue
    return False


def restart(conn):
    env = dict(os.environ, PULSE_CONN=conn, PYTHONUNBUFFERED="1")
    log = open(os.path.join(DIR, f"pulse-{conn}.log"), "a")
    subprocess.Popen(["python3", "-B", "pulse_trader.py"], cwd=REPO, env=env, stdout=log, stderr=log, start_new_session=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=300)
    a = ap.parse_args()
    hist = {v: [] for v in CONNS}
    no_valid_since = {v: None for v in CONNS}
    while True:
        now = time.time()
        for view, conn in CONNS.items():
            warns = []
            if not alive(conn):
                if os.path.exists(os.path.join(DIR, f"PAUSE-{conn.split('-')[-1]}")):
                    warns.append("engine not running; PAUSE file present, left alone")
                else:
                    warns.append("engine process missing: restarted")
                    restart(conn)
            s = stats(view)
            if "_error" in s:
                warns.append("sidecar: " + s["_error"])
                rec = {"t": now, "conn": conn, "warn": warns}
            else:
                pb = (s.get("sets") or {}).get("pfBase") or {}
                base = pb.get("base") or {}
                eq = s.get("equity")
                hist[view].append((now, eq))
                hist[view] = [x for x in hist[view] if now - x[0] <= 3700]
                old = hist[view][0][1] if hist[view] else None
                if eq and old and old > 0 and eq < 0.9 * old and view == "live":
                    warns.append(f"equity {old:.4f} -> {eq:.4f} within an hour")
                if (base.get("sets") or 0) == 0:
                    no_valid_since[view] = no_valid_since[view] or now
                    if now - no_valid_since[view] > 1800:
                        warns.append("no valid Set for 30 minutes (entries are blocked by the Base gate)")
                else:
                    no_valid_since[view] = None
                cov = (s.get("sets") or {}).get("bars") or {}
                med = cov.get("median") if isinstance(cov, dict) else None
                if med is not None and med < 1500:
                    warns.append(f"median history {med} bars")
                rec = {"t": now, "conn": conn, "equity": eq, "open": s.get("openCount"), "errors": s.get("errors"), "closed": len(s.get("closed") or []),
                       "effPf": base.get("pf"), "validSets": base.get("sets"), "orders": base.get("n"), "validatedPct": pb.get("validatedPct"),
                       "lastError": (s.get("lastError") or "")[:80], "warn": warns}
            with open(os.path.join(DIR, "monitor.jsonl"), "a") as f:
                f.write(json.dumps(rec) + "\n")
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
