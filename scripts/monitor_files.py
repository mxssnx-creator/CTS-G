#!/usr/bin/env python3
"""Read-only health monitor of the running engines, from the files they write (no API call, no order endpoint, nothing is started or changed).

For each connection: process alive and its memory, age of its stats file, running / halted flags, equity against the start equity, open positions, closed orders with
wins and losses, the time of the newest OPEN / CLOSE in the engine log, and ALERTS (stale stats, process missing, halted, drawdown past a limit, no order opened for
too long while Sets are valid). One JSON line per connection per interval; --once prints a single round. Exit status 1 when any alert is raised in --once mode.

usage: monitor_files.py [--dir /opt/grok-x01-pulse] [--conn bingx-x01 --conn bingx-x02] [--interval 60] [--once] [--stale-s 180] [--idle-open-min 90] [--dd-pct 50]"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

LOGS = {"bingx-x01": ["x01_live.log"], "bingx-x02": ["/var/log/cts-g/bingx-x02.log"]}


def pids_of(conn: str):
    out = []
    try:
        for p in subprocess.run(["pgrep", "-f", "[p]ulse_trader.py"], capture_output=True, text=True).stdout.split():
            try:
                env = open(f"/proc/{p}/environ", "rb").read().split(b"\0")
            except OSError:
                continue
            if f"PULSE_CONN={conn}".encode() in env:
                out.append(int(p))
    except Exception:
        pass
    return out


def rss_mb(pid: int):
    try:
        for line in open(f"/proc/{pid}/status"):
            if line.startswith("VmRSS"):
                return round(int(line.split()[1]) / 1024.0, 1)
    except OSError:
        pass
    return None


def last_event(paths, word: str):
    """Time (epoch) of the newest log line that holds ` OPEN ` / ` CLOSE `; reads only the file's tail."""
    best = None
    for path in paths:
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                f.seek(max(0, size - 400000))
                tail = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in reversed(tail):
            if f" {word} " in line and line[:4].isdigit():
                try:
                    t = time.mktime(time.strptime(line[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
                    best = t if best is None else max(best, t)
                except ValueError:
                    pass
                break
    return best


def check(conn: str, base: str, a) -> dict:
    now = time.time()
    rec = {"t": int(now), "conn": conn, "alerts": []}
    pids = pids_of(conn)
    rec["pids"] = pids
    rec["rssMb"] = rss_mb(pids[0]) if pids else None
    if not pids:
        rec["alerts"].append("process-missing")
    try:
        st = json.load(open(os.path.join(base, f"stats-{conn}.json")))
    except Exception as e:
        rec["alerts"].append(f"stats-unreadable:{type(e).__name__}")
        return rec
    age = now - float(st.get("now") or 0)
    ex = st.get("execution") or {}
    closed, opened = ex.get("closed") or {}, ex.get("open") or {}
    eq, se = st.get("equity"), st.get("sessionStartEquity") or st.get("startEquity")
    rec.update({"statsAgeS": round(age, 1), "running": st.get("running"), "halted": st.get("halted"), "uptimeS": round(float(st.get("uptimeS") or 0)),
                "equity": eq, "startEquity": se, "openPositions": opened.get("positions"), "closedOrders": closed.get("orders"),
                "wins": closed.get("wins"), "losses": closed.get("losses"), "notScored": closed.get("skippedNotScored")})
    logs = [p if os.path.isabs(p) else os.path.join(base, p) for p in LOGS.get(conn, [])]
    lo, lc = last_event(logs, "OPEN"), last_event(logs, "CLOSE")
    rec["lastOpenAgoMin"] = round((now - lo) / 60, 1) if lo else None
    rec["lastCloseAgoMin"] = round((now - lc) / 60, 1) if lc else None
    if age > a.stale_s:
        rec["alerts"].append(f"stats-stale:{int(age)}s")
    if st.get("running") is False:
        rec["alerts"].append("engine-not-running")
    if st.get("halted"):
        rec["alerts"].append("halted")
    try:
        if eq is not None and se and float(se) > 0 and (float(se) - float(eq)) / float(se) * 100 >= a.dd_pct:
            rec["alerts"].append("drawdown")
    except (TypeError, ValueError):
        pass
    if pids and float(st.get("uptimeS") or 0) > 1800 and (lo is None or now - lo > a.idle_open_min * 60) and not opened.get("positions"):
        rec["alerts"].append(f"no-open-for-{a.idle_open_min}min")
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="/opt/grok-x01-pulse")
    ap.add_argument("--conn", action="append")
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--stale-s", type=float, default=180.0)
    ap.add_argument("--idle-open-min", type=float, default=90.0)
    ap.add_argument("--dd-pct", type=float, default=50.0)
    a = ap.parse_args()
    conns = a.conn or ["bingx-x01", "bingx-x02"]
    while True:
        bad = False
        for c in conns:
            r = check(c, a.dir, a)
            bad = bad or bool(r["alerts"])
            print(json.dumps(r), flush=True)
        if a.once:
            return 1 if bad else 0
        time.sleep(max(5.0, a.interval))


if __name__ == "__main__":
    sys.exit(main())
