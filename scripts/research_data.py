#!/usr/bin/env python3
"""Fetch and cache public BingX history for the walk-forward research.

The cache lives outside Git (``.research-data/bingx`` by default):

- ``1h/<SYMBOL>.npz``: columns t (open time ms), o, h, l, c, v for 1h bars.
- ``1m/<SYMBOL>.npz``: the same columns for 1m bars.
- ``funding/<SYMBOL>.npz``: t (funding time ms) and rate.
- ``manifest.json``: per file sha256, bar count, first/last time and gaps.

Every request goes through ``hist_calc._public_json``, which paces public
calls to one per 1.05 s, so a fetch never exceeds the documented quote limit.
Fetches resume: an existing file is extended from its last bar, never
re-downloaded.

Usage::

    python scripts/research_data.py --tf 1h --since 2024-04-01 --symbols 70
    python scripts/research_data.py --tf 1m --days 30 --symbols 50
    python scripts/research_data.py --funding --since 2024-04-01 --symbols 70
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server", "pulse"))

import hist_calc  # noqa: E402

DEFAULT_OUT = os.path.join(ROOT, ".research-data", "bingx")
TICKER_URL = "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"
FUNDING_URL = "https://open-api.bingx.com/openApi/swap/v2/quote/fundingRate"
TF_MS = {"1m": 60_000, "1h": 3_600_000}
# BingX returns at most 1,000 bars per call (the newest ones when a range is longer).
PAGE = 1000
# Synthetic stock / FX contracts are not crypto perpetuals.
SKIP_PREFIXES = ("NCCO", "NCS", "NCFX")
COLUMNS = ("t", "o", "h", "l", "c", "v")


def _utc_ms(day: str) -> int:
    return int(dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def ranked_symbols(n: int) -> List[str]:
    """Top-n USDT perpetuals by 24h quote volume, majors first."""
    body = hist_calc._public_json(TICKER_URL, timeout=20.0)
    rows = body.get("data") if isinstance(body, dict) else []
    ranked: List[Tuple[float, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym.endswith("-USDT") or sym.startswith(SKIP_PREFIXES):
            continue
        try:
            qv = float(row.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        if qv > 0:
            ranked.append((qv, sym))
    ranked.sort(reverse=True)
    return [sym for _qv, sym in ranked[:n]]


def _kline_rows(data: Any) -> List[Tuple[int, List[float]]]:
    out: List[Tuple[int, List[float]]] = []
    for raw in data if isinstance(data, list) else []:
        try:
            if isinstance(raw, dict):
                t = int(raw.get("time") or 0)
                bar = [float(raw["open"]), float(raw["high"]), float(raw["low"]), float(raw["close"]), float(raw.get("volume") or 0)]
            else:
                t = int(raw[0])
                bar = [float(raw[1]), float(raw[2]), float(raw[3]), float(raw[4]), float(raw[5])]
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if t > 0 and min(bar[:4]) > 0:
            out.append((t, bar))
    return out


def fetch_range(symbol: str, tf: str, start_ms: int, end_ms: int) -> Dict[int, List[float]]:
    """All completed bars with open time in [start_ms, end_ms)."""
    step = TF_MS[tf]
    got: Dict[int, List[float]] = {}
    cursor = start_ms
    empty_pages = 0
    while cursor < end_ms:
        page_end = min(end_ms, cursor + PAGE * step)
        params = {"symbol": symbol, "interval": tf, "startTime": str(cursor), "endTime": str(page_end - 1), "limit": str(PAGE)}
        rows: List[Tuple[int, List[float]]] = []
        for attempt in range(3):
            try:
                body = hist_calc._public_json(f"{hist_calc.KLINE_URL_V3}?{urllib.parse.urlencode(params)}", timeout=20.0)
                rows = _kline_rows(body.get("data") if isinstance(body, dict) else body)
                break
            except Exception:
                time.sleep(2.0 * (attempt + 1))
        for t, bar in rows:
            if start_ms <= t < end_ms:
                got[t] = bar
        if not rows:
            empty_pages += 1
            # Before listing BingX returns nothing; skip ahead page by page.
            if empty_pages > 40:
                break
        else:
            empty_pages = 0
        cursor = page_end
    return got


def fetch_funding(symbol: str, start_ms: int, end_ms: int) -> Dict[int, float]:
    got: Dict[int, float] = {}
    cursor = start_ms
    while cursor < end_ms:
        params = {"symbol": symbol, "startTime": str(cursor), "endTime": str(end_ms), "limit": "1000"}
        try:
            body = hist_calc._public_json(f"{FUNDING_URL}?{urllib.parse.urlencode(params)}", timeout=20.0)
        except Exception:
            break
        rows = body.get("data") if isinstance(body, dict) else []
        newest = 0
        for row in rows if isinstance(rows, list) else []:
            try:
                t = int(row.get("fundingTime") or 0)
                rate = float(row.get("fundingRate") or 0)
            except (TypeError, ValueError, AttributeError):
                continue
            if start_ms <= t < end_ms:
                got[t] = rate
                newest = max(newest, t)
        if not rows or newest <= cursor:
            break
        cursor = newest + 1
    return got


def _path(out: str, tf: str, symbol: str) -> str:
    return os.path.join(out, tf, f"{symbol}.npz")


def load_bars(out: str, tf: str, symbol: str) -> Optional[Dict[str, np.ndarray]]:
    path = _path(out, tf, symbol)
    if not os.path.exists(path):
        return None
    with np.load(path) as z:
        return {k: z[k].copy() for k in z.files}


def save_bars(out: str, tf: str, symbol: str, rows: Dict[int, List[float]]) -> str:
    path = _path(out, tf, symbol)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ts = sorted(rows)
    arr = np.array([rows[t] for t in ts], dtype=np.float64).reshape(-1, 5)
    tmp = path + ".tmp.npz"
    np.savez_compressed(tmp, t=np.array(ts, dtype=np.int64), o=arr[:, 0], h=arr[:, 1], l=arr[:, 2], c=arr[:, 3], v=arr[:, 4])
    os.replace(tmp, path)
    return path


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _gaps(t: np.ndarray, step: int) -> int:
    if len(t) < 2:
        return 0
    return int(np.sum(np.diff(t) != step))


def update_manifest(out: str) -> Dict[str, Any]:
    manifest: Dict[str, Any] = {"generatedAt": int(time.time() * 1000), "files": {}}
    for tf in ("1h", "1m", "funding"):
        folder = os.path.join(out, tf)
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".npz") or name.endswith(".tmp.npz"):
                continue
            path = os.path.join(folder, name)
            with np.load(path) as z:
                t = z["t"]
                entry = {"n": int(len(t)), "first": int(t[0]) if len(t) else 0, "last": int(t[-1]) if len(t) else 0, "sha256": _sha256(path)}
                if tf in TF_MS:
                    entry["gaps"] = _gaps(t, TF_MS[tf])
            manifest["files"][f"{tf}/{name}"] = entry
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
    return manifest


def run(args: argparse.Namespace) -> int:
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    symbols = [s.strip().upper() for s in args.symbol_list.split(",") if s.strip()] if args.symbol_list else ranked_symbols(args.symbols)
    with open(os.path.join(out, f"symbols-{'funding' if args.funding else args.tf}.json"), "w") as f:
        json.dump(symbols, f)
    now_ms = int(time.time() * 1000)
    started = time.time()
    for k, sym in enumerate(symbols, 1):
        if args.funding:
            start = _utc_ms(args.since)
            old = load_bars(out, "funding", sym)
            rows: Dict[int, float] = {}
            if old is not None and len(old["t"]):
                rows = {int(t): float(r) for t, r in zip(old["t"], old["rate"])}
                start = int(old["t"][-1]) + 1
            rows.update(fetch_funding(sym, start, now_ms))
            path = os.path.join(out, "funding", f"{sym}.npz")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            ts = sorted(rows)
            np.savez_compressed(path + ".tmp.npz", t=np.array(ts, dtype=np.int64), rate=np.array([rows[t] for t in ts], dtype=np.float64))
            os.replace(path + ".tmp.npz", path)
            n = len(ts)
        else:
            step = TF_MS[args.tf]
            end = now_ms // step * step  # exclude the bar still in progress
            start = _utc_ms(args.since) if args.since else end - int(args.days * 86_400_000) // step * step
            old = load_bars(out, args.tf, sym)
            rows_b: Dict[int, List[float]] = {}
            if old is not None and len(old["t"]):
                rows_b = {int(t): [float(old[c][i]) for c in COLUMNS[1:]] for i, t in enumerate(old["t"])}
                first, last = int(old["t"][0]), int(old["t"][-1])
                if start < first:
                    rows_b.update(fetch_range(sym, args.tf, start, first))
                start = last + step
            if start < end:
                rows_b.update(fetch_range(sym, args.tf, start, end))
            if rows_b:
                save_bars(out, args.tf, sym, rows_b)
            n = len(rows_b)
        print(f"[{k}/{len(symbols)}] {sym} n={n} elapsed={time.time() - started:.0f}s", flush=True)
    update_manifest(out)
    print("done", flush=True)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--tf", choices=sorted(TF_MS), default="1h")
    ap.add_argument("--since", default="", help="UTC start day YYYY-MM-DD")
    ap.add_argument("--days", type=float, default=30, help="window length when --since is empty (fractions allowed)")
    ap.add_argument("--symbols", type=int, default=70, help="top-n by 24h quote volume")
    ap.add_argument("--symbol-list", default="", help="comma separated symbols (overrides --symbols)")
    ap.add_argument("--funding", action="store_true", help="fetch funding-rate history instead of bars")
    ap.add_argument("--manifest-only", action="store_true")
    args = ap.parse_args(argv)
    if args.manifest_only:
        update_manifest(os.path.abspath(args.out))
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
