#!/usr/bin/env python3
"""Export completed-bar experimental Python Signals for the offline window replay.

The output is a research event archive, not a live trading adapter. It keeps
the feed hash and exact source/range/timeframe/symbol identities.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server/pulse"))
from signal_sources import SOURCES, TIMEFRAMES, onsets, validate_bars  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feed", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--start", required=True, type=int, help="UTC epoch milliseconds")
    parser.add_argument("--end", required=True, type=int, help="exclusive UTC epoch milliseconds")
    parser.add_argument("--symbols", required=True, nargs="+")
    parser.add_argument("--sources", nargs="+", default=["volume-momentum"])
    parser.add_argument("--timeframes", nargs="+", type=int, default=[1, 5, 15])
    args = parser.parse_args()
    if (args.out.exists() or args.start < 0 or args.start % 60000 or args.end % 60000 or args.end <= args.start or
            not args.symbols or len(args.symbols) != len(set(args.symbols)) or
            not args.sources or len(args.sources) != len(set(args.sources)) or any(s not in SOURCES for s in args.sources) or
            not args.timeframes or len(args.timeframes) != len(set(args.timeframes)) or any(t not in TIMEFRAMES for t in args.timeframes)):
        parser.error("invalid window, duplicate or unsupported identity, or output already exists")
    payload = args.feed.read_bytes()
    feed = json.loads(gzip.decompress(payload) if args.feed.suffix == ".gz" else payload)
    streams = []
    for symbol in sorted(args.symbols):
        try:
            rows = [[r[k] for k in ("t", "o", "h", "l", "c", "v")]
                    for r in feed["candles"][symbol + "|1"] if args.start <= r["t"] < args.end]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"missing one-minute bars for {symbol}") from exc
        validate_bars(rows)
        if len(rows) != (args.end-args.start)//60000 or rows[0][0] != args.start or rows[-1][0]+60000 != args.end:
            raise ValueError(f"incomplete selected history for {symbol}")
        for source in args.sources:
            for tf in args.timeframes:
                for label in ("short", "medium"):
                    events = onsets(rows, source, label, tf)
                    streams.append({"identity": {"symbol": symbol, "source": source, "sourceRange": label,
                                                 "tf": tf, "coordination": "source"},
                                    "events": sorted(events.items())})
    source_file = ROOT / "server/pulse/signal_sources.py"
    archive = {"schema": "cts-g-signal-events-v1", "series": streams,
               "provenance": {"feedSha256": hashlib.sha256(payload).hexdigest(),
                              "sourceSha256": hashlib.sha256(source_file.read_bytes()).hexdigest(),
                              "symbols": sorted(args.symbols), "start": args.start, "end": args.end,
                              "source": "experimental Python completed-bar Signals; offline only"}}
    encoded = json.dumps(archive, separators=(",", ":"), allow_nan=False).encode()
    args.out.write_bytes(gzip.compress(encoded, mtime=0) if args.out.suffix == ".gz" else encoded)
    print(json.dumps({"series": len(streams), "events": sum(len(s["events"]) for s in streams)}))


if __name__ == "__main__":
    main()
