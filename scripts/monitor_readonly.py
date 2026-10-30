#!/usr/bin/env python3
"""Read-only Pulse health checks. Never starts engines or accesses order/control APIs.

Example: python scripts/monitor_readonly.py --url http://HOST:3015 --conn live --symbols 50
Use --once for a single check. Repeated checks print one JSON record per interval;
service supervision is external. Endpoint downtime is reported, never auto-repaired.
"""
from __future__ import annotations

import argparse
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_RESPONSE = 4 * 1024 * 1024


def finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def read_stats(base, conn, timeout=8, opener=urllib.request.urlopen):
    """Exactly one GET, bounded response and timeout. No credentials or mutations."""
    url = urllib.parse.urlsplit(base)
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
        raise ValueError("use an HTTP(S) status URL without embedded credentials")
    if url.path not in ("", "/", "/stats.json") or url.query or url.fragment:
        raise ValueError("use a Pulse base URL or /stats.json, without query or fragment")
    target = urllib.parse.urlunsplit((url.scheme, url.netloc, "/stats.json", "conn=" + conn, ""))
    request = urllib.request.Request(target, method="GET", headers={"Accept": "application/json", "Cache-Control": "no-cache"})
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            return {"_error": "status response exceeds 4 MiB"}
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {"_error": "status response is not an object"}
        return data
    except urllib.error.HTTPError as exc:
        return {"_error": f"HTTP {exc.code}"}
    except (OSError, ValueError) as exc:
        return {"_error": type(exc).__name__}


class HealthMonitor:
    def __init__(self, conn="live", symbols=50, stale_seconds=120):
        self.conn, self.symbols, self.stale_seconds = conn, symbols, stale_seconds
        self.cycle = self.cycle_changed_at = self.errors = self.started_at = None

    def sample(self, stats, now):
        warnings = []
        result = {"checkedAt": now, "connection": self.conn, "expectedSymbols": self.symbols,
                  "readOnly": True, "reachable": "_error" not in stats}
        if "_error" in stats:
            return {**result, "healthy": False, "warnings": ["status unavailable: " + stats["_error"]]}
        expected = "bingx-x01" if self.conn == "live" else "bingx-x02"
        if stats.get("connection") != expected:
            warnings.append("connection identity missing or unexpected")
        timestamp = finite(stats.get("now"))
        if timestamp is None or now - timestamp > self.stale_seconds or timestamp > now + 30:
            warnings.append("status timestamp missing, stale or ahead of clock")
        running = stats.get("running") is True
        if not running or stats.get("alive") is False:
            warnings.append("engine not confirmed running")
        if stats.get("halted"):
            warnings.append("engine halted")
        if stats.get("paused"):
            warnings.append("engine paused")
        cycle = finite(stats.get("cycle"))
        sets = stats.get("sets") if isinstance(stats.get("sets"), dict) else {}
        progress = sets.get("progress") if isinstance(sets.get("progress"), dict) else {}
        heartbeat = (cycle, progress.get("phase"), progress.get("barsDone"), progress.get("setsDone"))
        started = stats.get("startedAt")
        if cycle is None:
            warnings.append("cycle missing")
        elif self.cycle != heartbeat or self.started_at != started:
            self.cycle, self.cycle_changed_at, self.started_at = heartbeat, now, started
        elif running and self.cycle_changed_at is not None and now - self.cycle_changed_at >= self.stale_seconds:
            warnings.append("cycle stalled")
        count = finite(stats.get("symbolCount"))
        if count != self.symbols:
            warnings.append("symbol count differs from configured target")
        errors = finite(stats.get("errors"))
        if errors is not None and self.errors is not None and errors > self.errors:
            warnings.append("error count increased")
        if errors is not None:
            self.errors = errors
        pb = sets.get("pfBase") if isinstance(sets.get("pfBase"), dict) else {}
        def stage(key):
            stages = pb.get("stages") if isinstance(pb.get("stages"), dict) else {}
            row = stages.get(key)
            if not isinstance(row, dict):
                row = pb.get("baseOnly" if key == "base" else key) or pb.get(key)
            row = row if isinstance(row, dict) else {}
            return {"pf": row.get("pf"), "sets": row.get("sets"), "orders": row.get("n"), "source": "paper"}
        result.update(running=running, halted=bool(stats.get("halted")), paused=bool(stats.get("paused")),
                      symbolCount=count, cycle=cycle, errors=errors, openCount=stats.get("openCount"),
                      paperBase=stage("base"), paperMain=stage("main"),
                      healthy=not warnings, warnings=warnings)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--conn", choices=("live", "vst"), default="live")
    parser.add_argument("--symbols", type=int, default=50)
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--stale-seconds", type=float, default=120)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.symbols < 1 or args.interval < 5 or args.stale_seconds < 10:
        parser.error("symbols >= 1, interval >= 5 seconds and stale-seconds >= 10 required")
    monitor = HealthMonitor(args.conn, args.symbols, args.stale_seconds)
    while True:
        started = time.monotonic()
        try:
            result = monitor.sample(read_stats(args.url, args.conn), time.time())
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(result, allow_nan=False), flush=True)
        if args.once:
            return 0 if result["healthy"] else 1
        time.sleep(max(0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    raise SystemExit(main())
