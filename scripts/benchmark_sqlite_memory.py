"""Bounded, synthetic local storage benchmark; never starts an exchange engine."""
import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

PULSE = str(Path(__file__).resolve().parents[1] / "server/pulse")
sys.path.insert(0, PULSE)
from sqlite_memory import DiskStatisticsStore, MemoryStatisticsStore


def timings(values):
    ordered = sorted(values)
    return {"medianMs": statistics.median(values), "p95Ms": ordered[int((len(values) - 1) * .95)],
            "maxMs": max(values), "samples": len(values)}


def measure(cls):
    with tempfile.TemporaryDirectory(prefix="cts-sqlite-bench-") as root:
        store = cls(root, "bingx-x02", {"systemTradeMaxRows": 250, "systemDbMaxMb": 8,
                                      "systemSqliteMemory": int(cls is MemoryStatisticsStore)})
        try:
            writes, reads = [], []
            stamp = time.time() - 60
            for i in range(2000):
                start = time.perf_counter()
                assert store.record_trade(dict(t=stamp+i/10000, conn="bingx-x02", symbol="TEST-USDT", side="LONG",
                    qty=1, entry=100, pnl=2 if i % 2 else -1, close_fill_id=f"bench-{i}", ours=True, exchange_confirmed=True), 1000)
                writes.append((time.perf_counter() - start) * 1000)
            store.maintain()
            for _ in range(250):
                start = time.perf_counter()
                result = store.status()
                reads.append((time.perf_counter() - start) * 1000)
                assert result["totals"]["n"] == 2000 and result["totals"]["realized"] == 1000
            start = time.perf_counter()
            store.backup()
            backup_ms = (time.perf_counter() - start) * 1000
            start = time.perf_counter()
            store.checkpoint(force=True)
            checkpoint_ms = (time.perf_counter() - start) * 1000
            code = """
import json, os, time
from sqlite_memory import memory_request
values=[]
for _ in range(100):
    start=time.perf_counter()
    result=memory_request(os.environ['QA_ROOT'], 'bingx-x02', {'action':'status'})
    assert result['totals']['n']==2000
    values.append((time.perf_counter()-start)*1000)
print(json.dumps(values))
"""
            child = subprocess.run([sys.executable, "-c", code], env={**os.environ, "QA_ROOT": root, "PYTHONPATH": PULSE},
                                   capture_output=True, text=True, check=True, timeout=15)
            result = store.status()
            return {"confirmedSyntheticFills": 2000, "retainedTradeRows": result["dbRows"]["trades"],
                    "writes": timings(writes), "ownerReads": timings(reads),
                    "crossProcessReads": timings(json.loads(child.stdout)), "backupMs": backup_ms,
                    "checkpointMs": checkpoint_ms if cls is MemoryStatisticsStore else None,
                    "dbBytes": result["dbBytes"], "memoryBytes": result.get("memoryBytes"),
                    "journalBytes": result.get("journalBytes")}
        finally:
            store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    report = {"scope": "Local synthetic statistics benchmark; filesystem-dependent, no exchange throughput claim",
              "disk": measure(DiskStatisticsStore), "memory": measure(MemoryStatisticsStore)}
    Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
