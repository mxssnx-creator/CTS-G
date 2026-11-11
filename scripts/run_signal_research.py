"""Bounded offline Signals replay, with frozen settings and no venue access."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import resource
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "pulse"))
from signal_replay import replay, canonical


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--feed", type=Path, required=True)
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--start", type=int, required=True)
    ap.add_argument("--freeze-prehistory", action="store_true", help="freeze eligible exact contexts strictly before trading, retain ongoing gates")
    args = ap.parse_args()
    if args.out.exists(): ap.error("preserve earlier research output")
    feed_bytes, config_bytes = args.feed.read_bytes(), args.config.read_bytes()
    raw = json.loads(gzip.decompress(feed_bytes) if args.feed.suffix == ".gz" else feed_bytes)
    config = json.loads(config_bytes)
    start, trade_start, end = args.start, args.start + 12*3600000, args.start + 24*3600000
    symbols = sorted(k[:-2] for k in raw["candles"] if k.endswith("|1"))[:8]
    if len(symbols) != 8: ap.error("exactly eight archived symbols required")
    data = {s: [[r["t"], r["o"], r["h"], r["l"], r["c"], r["v"]] for r in raw["candles"][s+"|1"] if start <= r["t"] < end] for s in symbols}
    files = [ROOT/"server/pulse/signal_sources.py", ROOT/"server/pulse/signal_replay.py", Path(__file__)]
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    began = time.perf_counter()
    result = replay(data, config, start, trade_start, end, freeze_prehistory=args.freeze_prehistory)
    result["provenance"] = {"feedSha256": hashlib.sha256(feed_bytes).hexdigest(), "configFileSha256": hashlib.sha256(config_bytes).hexdigest(),
        "sourceSha256": hashes, "symbols": symbols, "evaluatedHistoryHours": 12, "tradingHours": 12,
        "studyStatus": "retrospective diagnostic on previously studied data; NOT an untouched holdout"}
    result["performance"] = {"wallSeconds": time.perf_counter()-began, "peakRssKiB": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
    if any(hashlib.sha256(p.read_bytes()).hexdigest()!=hashes[str(p.relative_to(ROOT))] for p in files): raise ValueError("sources changed during replay")
    payload = canonical(result).encode()
    args.out.write_bytes(gzip.compress(payload,mtime=0) if args.out.suffix==".gz" else payload)
    print(json.dumps({"file":str(args.out), "contexts":len(result["contexts"]), "stages":result["stageProgress"][-1],
        "execution":result["modeledExecution"], "orders":result["modelCounts"], "hourly":result["hourly"]["counts"],
        "everyHourNetPositive":result["hourly"]["everyHourNetPositive"], "drawdown":result["maxEquityDrawdown"], "performance":result["performance"]}))


if __name__ == "__main__": main()
