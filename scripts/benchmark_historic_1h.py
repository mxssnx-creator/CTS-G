#!/usr/bin/env python3
"""Benchmark the complete one-hour historic matrix without publishing results.

Synthetic mode is the repeatable CPU canary. Pass ``--real`` to include the
public BingX fetch lane; no overlay, winner, order, or job snapshot is written.
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time
from typing import Any, Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PULSE = os.path.join(ROOT, "server", "pulse")
if PULSE not in sys.path:
    sys.path.insert(0, PULSE)

from hist_calc import run_calc  # noqa: E402

DEFAULT_SYMBOLS = ["XRP-USDT", "BCH-USDT", "SOL-USDT"]


def benchmark_once(symbols: List[str], workers: int, real: bool) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "hours": 1,
        "symbols": symbols,
        "allSymbols": False,
        "allConfigs": True,
        "trailing": True,
        "stratBlock": True,
        "stratDca": True,
        "stratIndications": True,
        "stratGeneral": True,
        "indTypeSignals": True,
        "indTypeState": True,
        "indTypeDirection": True,
        "indTypeMove": True,
        "indTypeActive": True,
        "indTypeCommon": True,
        "indTypeTrend": True,
        "indTypeBreak": True,
        "workers": workers,
        "synth": not real,
    }
    started = time.perf_counter()
    job = run_calc(body, persist=False)
    wall_ms = (time.perf_counter() - started) * 1000.0
    coverage = job.get("coverage") if isinstance(job.get("coverage"), dict) else {}
    return {
        "benchmark": "historic-1h",
        "mode": "real" if real else "synthetic",
        "symbols": symbols,
        "workers": workers,
        "wallMs": round(wall_ms, 1),
        "rssMb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1),
        "phase": job.get("phase"),
        "error": job.get("error") or "",
        "hours": job.get("hours"),
        "evaluationBars": job.get("evaluationBars"),
        "warmupBars": job.get("warmupBars"),
        "requestedBars": job.get("requestedBars"),
        "source": job.get("source"),
        "sets": coverage.get("setCount", coverage.get("product", 0)),
        "rows": job.get("rowCount", 0),
        "validatedRows": job.get("validatedCount", 0),
        "coverage": {
            "symbols": coverage.get("symbols", {}),
            "bars": coverage.get("bars", {}),
            "evaluationBars": coverage.get("evaluationBars", {}),
            "sets": coverage.get("sets", {}),
            "evaluations": coverage.get("evaluations", {}),
            "tasks": coverage.get("tasks", {}),
        },
        "replayTasks": job.get("replayTasks", {}),
        "replayTiles": job.get("replayTiles", {}),
        "timings": job.get("timings", {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true", help="fetch the selected one-hour window from BingX")
    parser.add_argument("--all-symbols", action="store_true", help="use the configured exchange universe")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS, help="symbols for the CPU canary")
    parser.add_argument("--workers", nargs="+", type=int, default=[1, 2, 4, 8], help="worker counts to compare")
    parser.add_argument("--repeat", type=int, default=1, help="repeat every worker count")
    args = parser.parse_args()

    symbols = ["*"] if args.all_symbols else [str(symbol).upper() for symbol in args.symbols]
    workers = sorted({max(1, int(value)) for value in args.workers})
    repeat = max(1, int(args.repeat))
    for worker_count in workers:
        for iteration in range(repeat):
            row = benchmark_once(symbols, worker_count, args.real)
            row["iteration"] = iteration + 1
            print(json.dumps(row, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
