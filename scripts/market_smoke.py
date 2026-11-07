#!/usr/bin/env python3
"""Read-only 200-symbol candle/indication readiness check. Never authenticates or trades."""
import argparse
import concurrent.futures
import json
from pathlib import Path
import sys
import time
from fetch_audit_market import history

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server/pulse"))
import indication_extra as ix


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--metadata", required=True)
    p.add_argument("--preset", required=True)
    p.add_argument("--out", required=True)
    a = p.parse_args()
    meta = json.loads(Path(a.metadata).read_text())
    preset = json.loads(Path(a.preset).read_text())
    names = [n for e in preset["indicationConfigs"] for n in e["names"]]
    end = int(time.time() // 60) * 60000
    start = end - 720 * 60000
    def check(contract):
        symbol = contract["symbol"]
        began = time.perf_counter()
        try:
            rows = history(symbol, start, end)
            # Each worker has independent memoized signal history.
            votes = ix.extra_votes([r[1:] for r in rows], names)
            return {"symbol": symbol, "ok": True, "bars": len(rows), "signals": len(votes),
                    "seconds": round(time.perf_counter() - began, 3)}
        except Exception as e:
            return {"symbol": symbol, "ok": False, "error": str(e)}
    began = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(check, meta["universe200"]))
    out = {"mode": "public market and indication readiness; no account or order test",
           "account_authenticated": False, "orders_submitted": 0, "start_ms": start, "end_ms": end,
           "symbols": len(rows), "passed": sum(r["ok"] for r in rows), "indications": len(names),
           "seconds": round(time.perf_counter() - began, 3), "results": rows}
    Path(a.out).write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps({k: v for k, v in out.items() if k != "results"}))


if __name__ == "__main__":
    main()
