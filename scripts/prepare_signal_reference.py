#!/usr/bin/env python3
"""Convert a CTS-A-O candle archive into the CTS-G trade_sim input format, offline."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import re
from pathlib import Path


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--feed", required=True, help="CTS-A-O signals coverage feed.json.gz")
    p.add_argument("--out", required=True, help="new output directory")
    p.add_argument("--symbols", type=int, default=8)
    p.add_argument("--pre-hours", type=int, default=72)
    p.add_argument("--forward-hours", type=int, default=12)
    p.add_argument("--warmup-hours", type=int, default=1)
    a = p.parse_args()
    if min(a.symbols, a.pre_hours, a.forward_hours) <= 0 or a.warmup_hours < 0:
        p.error("symbols and windows must be positive; warmup cannot be negative")
    feed_path, out = Path(a.feed), Path(a.out)
    if out.exists() and any(out.iterdir()):
        p.error("output directory must be empty")
    with gzip.open(feed_path, "rt", encoding="utf-8") as fh:
        feed = json.load(fh)
    lanes = {key[:-2]: rows for key, rows in feed["candles"].items() if key.endswith("|1")}
    names = sorted(lanes)[:a.symbols]
    if len(names) != a.symbols:
        p.error("archive has fewer one-minute symbols than requested")
    if any(not re.fullmatch(r"[A-Z0-9]+-USDT", symbol) for symbol in names):
        p.error("archive contains an invalid symbol name")
    end = min(int(lanes[s][-1]["t"]) for s in names) + 60_000
    count = 60 * (a.pre_hours + a.forward_hours + a.warmup_hours)
    start = end - count * 60_000
    prepared = {}
    for symbol in names:
        rows = [r for r in lanes[symbol] if start <= int(r["t"]) < end]
        times = [int(r["t"]) for r in rows]
        if len(rows) != count or any(t != start + i * 60_000 for i, t in enumerate(times)):
            p.error(f"{symbol} has missing or out-of-order minutes")
        values = [[int(r["t"])] + [float(r[k]) for k in ("o", "h", "l", "c", "v")] for r in rows]
        if any(not all(math.isfinite(v) for v in row) or min(row[1:5]) <= 0 for row in values):
            p.error(f"{symbol} has invalid prices")
        prepared[symbol] = values
    out.mkdir(parents=True, exist_ok=True)
    for symbol, rows in prepared.items():
        (out / f"{symbol}.json").write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
    checksum = hashlib.sha256(feed_path.read_bytes()).hexdigest()
    (out / "_meta.json").write_text(json.dumps({"t_end": end, "symbols": names, "feed_sha256": checksum}, indent=2), encoding="utf-8")
    print(json.dumps({"symbols": names, "minutes_per_symbol": count, "start_ms": start, "end_ms": end, "feed_sha256": checksum}))


if __name__ == "__main__":
    main()
