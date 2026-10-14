#!/usr/bin/env python3
"""Research periods, per-period universes and A-O cache export.

The periods match the CTS-A-O research years so results stay comparable:

- A (select): 2025-10-11 → 2026-09-26, the A-O research year (halves A1/A2).
- B (validate): 2024-10-11 → 2025-09-26, the A-O prior year.
- C (holdout): 2024-05-12 → 2024-09-25, never used for selection of the
  indication list. A-O did use it when choosing the matrix cell.
- T (tail): 2026-09-27 → end of data. No A-O or CTS-G choice has seen it.

A period's universe is the top-n symbols by quote volume (close × volume)
over the 30 days before the period starts. Only symbols whose bars cover the
period plus its look-back are eligible, so no symbol is chosen with
knowledge of the period itself.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, ".research-data", "bingx")
H_MS = 3_600_000
D_MS = 24 * H_MS


def utc_ms(day: str) -> int:
    return int(dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def day_of(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


PERIODS: Dict[str, Tuple[int, Optional[int]]] = {
    "A": (utc_ms("2025-10-11"), utc_ms("2026-09-26")),
    "A1": (utc_ms("2025-10-11"), utc_ms("2026-03-28")),
    "A2": (utc_ms("2026-03-28"), utc_ms("2026-09-26")),
    "B": (utc_ms("2024-10-11"), utc_ms("2025-09-26")),
    "C": (utc_ms("2024-05-12"), utc_ms("2024-09-25")),
    "T": (utc_ms("2026-09-27"), None),  # open end: the last fetched bar
}


def load_bars(data_dir: str = DATA_DIR, tf: str = "1h", symbols: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, np.ndarray]]:
    folder = os.path.join(data_dir, tf)
    names = sorted(n[:-4] for n in os.listdir(folder) if n.endswith(".npz") and not n.endswith(".tmp.npz"))
    if symbols is not None:
        wanted = set(symbols)
        names = [n for n in names if n in wanted]
    out: Dict[str, Dict[str, np.ndarray]] = {}
    for sym in names:
        with np.load(os.path.join(folder, f"{sym}.npz")) as z:
            out[sym] = {k: z[k].copy() for k in z.files}
    return out


def data_end(bars: Dict[str, Dict[str, np.ndarray]], step_ms: int = H_MS) -> int:
    """Close time of the newest bar any symbol has."""
    return max(int(b["t"][-1]) + step_ms for b in bars.values() if len(b["t"]))


def period_range(name: str, bars: Dict[str, Dict[str, np.ndarray]]) -> Tuple[int, int]:
    start, end = PERIODS[name]
    return start, end if end is not None else data_end(bars)


def coverage(b: Dict[str, np.ndarray], t0: int, t1: int, step_ms: int = H_MS) -> float:
    t = b["t"]
    n = int(np.count_nonzero((t >= t0) & (t < t1)))
    return n / max(1, (t1 - t0) // step_ms)


def rank_symbols(bars: Dict[str, Dict[str, np.ndarray]], start_ms: int, end_ms: int, n: int,
                 lookback_ms: int = 40 * D_MS, rank_days: int = 30, min_coverage: float = 0.98,
                 step_ms: int = H_MS) -> List[str]:
    """Top-n by 30-day quote volume before start_ms, among fully covered symbols."""
    ranked: List[Tuple[float, str]] = []
    r0 = start_ms - rank_days * D_MS
    for sym, b in bars.items():
        if coverage(b, start_ms - lookback_ms, end_ms, step_ms) < min_coverage:
            continue
        m = (b["t"] >= r0) & (b["t"] < start_ms)
        if not np.any(m):
            continue
        ranked.append((float(np.sum(b["c"][m] * b["v"][m])), sym))
    ranked.sort(reverse=True)
    return [sym for _qv, sym in ranked[:n]]


def slice_bars(b: Dict[str, np.ndarray], t0: int, t1: int) -> Dict[str, np.ndarray]:
    m = (b["t"] >= t0) & (b["t"] < t1)
    return {k: v[m] for k, v in b.items()}


def export_ao_cache(bars: Dict[str, Dict[str, np.ndarray]], symbols: List[str], t0: int, t1: int, path: str) -> int:
    """Write A-O's candle cache format {SYMBOL: [{t,o,h,l,c,v}, ...]}."""
    blob = {}
    for sym in symbols:
        b = slice_bars(bars[sym], t0, t1)
        blob[sym] = [
            {"t": int(t), "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": float(v)}
            for t, o, h, l, c, v in zip(b["t"], b["o"], b["h"], b["l"], b["c"], b["v"])
        ]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(blob, f, separators=(",", ":"))
    return sum(len(v) for v in blob.values())


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=DATA_DIR)
    ap.add_argument("--period", required=True, choices=sorted(PERIODS))
    ap.add_argument("--symbols", type=int, default=40)
    ap.add_argument("--lookback-days", type=float, default=40.0, help="history exported before the period start")
    ap.add_argument("--out", help="A-O cache JSON path")
    args = ap.parse_args(argv)
    bars = load_bars(args.data)
    start, end = period_range(args.period, bars)
    lookback = int(args.lookback_days * D_MS)
    syms = rank_symbols(bars, start, end, args.symbols, lookback_ms=lookback)
    info = {"period": args.period, "start": day_of(start), "end": day_of(end), "symbols": syms, "count": len(syms)}
    if args.out:
        info["candles"] = export_ao_cache(bars, syms, start - lookback, end, args.out)
        info["out"] = args.out
    json.dump(info, sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
