"""Completed 1h bars for the hourly lane.

The hourly lane reads BingX v3 klines at ``interval=1h``. Only completed bars
are kept: a bar whose open time plus one hour lies in the future is still
forming and would leak the current hour into a decision. 4h bars are never
fetched; ``ao_indications`` builds them from these completed 1h bars so the
4h agreement matches CTS-A-O exactly.

Bars persist to ``hourly-bars-<conn>.json`` so a restart resumes without
refetching the whole window.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

H_MS = 3_600_000
KLINE_PATH = "/openApi/swap/v3/quote/klines"
# BingX returns at most 1,000 bars per kline call (the newest ones).
PAGE_MAX = 1000

Row = Tuple[int, float, float, float, float, float]


def parse_klines(data: Any, now_ms: int) -> List[Row]:
    """(t, o, h, l, c, v) rows of completed bars, oldest first."""
    rows: Dict[int, Row] = {}
    for raw in data if isinstance(data, list) else []:
        try:
            if isinstance(raw, dict):
                t = int(raw.get("time") or 0)
                row = (t, float(raw["open"]), float(raw["high"]), float(raw["low"]), float(raw["close"]), float(raw.get("volume") or 0.0))
            else:
                t = int(raw[0])
                row = (t, float(raw[1]), float(raw[2]), float(raw[3]), float(raw[4]), float(raw[5]))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if t <= 0 or t % H_MS or min(row[1:5]) <= 0 or t + H_MS > now_ms:
            continue
        rows[t] = row
    return [rows[t] for t in sorted(rows)]


class HourlyBars:
    """Per-symbol completed 1h bars with bounded history."""

    def __init__(self, keep: int = 1500, min_bars: int = 1200) -> None:
        self.keep = max(1, int(keep))
        self.min_bars = max(1, int(min_bars))
        self._rows: Dict[str, Dict[int, Row]] = {}
        self._lock = threading.Lock()

    def configure(self, keep: Optional[int] = None, min_bars: Optional[int] = None) -> None:
        if min_bars is not None:
            self.min_bars = max(1, int(min_bars))
        if keep is not None:
            self.keep = max(self.min_bars, int(keep))

    def symbols(self) -> List[str]:
        with self._lock:
            return sorted(self._rows)

    def count(self, sym: str) -> int:
        with self._lock:
            return len(self._rows.get(sym) or {})

    def last_t(self, sym: str) -> int:
        with self._lock:
            rows = self._rows.get(sym) or {}
            return max(rows) if rows else 0

    def ready(self, sym: str) -> bool:
        return self.count(sym) >= self.min_bars

    def merge(self, sym: str, rows: Iterable[Row]) -> int:
        """Insert or replace rows by open time; returns the number of new bars."""
        with self._lock:
            book = self._rows.setdefault(sym, {})
            added = 0
            for row in rows:
                if row[0] not in book:
                    added += 1
                book[row[0]] = row
            if len(book) > self.keep:
                for t in sorted(book)[: len(book) - self.keep]:
                    del book[t]
            return added

    def drop(self, keep_symbols: Iterable[str]) -> None:
        keep = set(keep_symbols)
        with self._lock:
            for sym in [s for s in self._rows if s not in keep]:
                del self._rows[sym]

    def gaps(self, sym: str, last: Optional[int] = None) -> int:
        """Missing hours inside the newest ``last`` bars (default: min_bars)."""
        with self._lock:
            ts = sorted(self._rows.get(sym) or {})
        if len(ts) < 2:
            return 0
        ts = ts[-(last or self.min_bars):]
        return int((ts[-1] - ts[0]) // H_MS + 1 - len(ts))

    def arrays(self, sym: str) -> Dict[str, np.ndarray]:
        """Column arrays t, o, h, l, c, v (t as int64 open time ms)."""
        with self._lock:
            rows = [self._rows[sym][t] for t in sorted(self._rows.get(sym) or {})]
        if not rows:
            empty = np.zeros(0, dtype=np.float64)
            return {"t": np.zeros(0, dtype=np.int64), "o": empty, "h": empty, "l": empty, "c": empty, "v": empty}
        arr = np.array(rows, dtype=np.float64)
        return {"t": arr[:, 0].astype(np.int64), "o": arr[:, 1], "h": arr[:, 2], "l": arr[:, 3], "c": arr[:, 4], "v": arr[:, 5]}

    # ── fetching ───────────────────────────────────────────────
    @staticmethod
    def boot_requests(sym: str, bars: int, now_ms: int) -> List[Dict[str, str]]:
        """Paged kline params covering the newest ``bars`` completed hours."""
        end = now_ms // H_MS * H_MS  # open time of the bar still forming
        start = end - int(bars) * H_MS
        out = []
        cursor = start
        while cursor < end:
            page_end = min(end, cursor + PAGE_MAX * H_MS)
            out.append({"symbol": sym, "interval": "1h", "startTime": str(cursor), "endTime": str(page_end - 1), "limit": str(PAGE_MAX)})
            cursor = page_end
        return out

    @staticmethod
    def refresh_request(sym: str, limit: int = 3) -> Dict[str, str]:
        return {"symbol": sym, "interval": "1h", "limit": str(max(2, int(limit)))}

    def boot(self, sym: str, public: Callable[[str, Dict[str, str]], Any], now_ms: int, bars: Optional[int] = None) -> int:
        """Fetch the full window for one symbol through ``public(path, params)``."""
        added = 0
        for params in self.boot_requests(sym, bars or self.keep, now_ms):
            body = public(KLINE_PATH, params)
            added += self.merge(sym, parse_klines(body.get("data") if isinstance(body, dict) else None, now_ms))
        return added

    def refresh(self, sym: str, body: Any, now_ms: int) -> int:
        return self.merge(sym, parse_klines(body.get("data") if isinstance(body, dict) else None, now_ms))

    # ── persistence ────────────────────────────────────────────
    def to_json(self) -> Dict[str, Any]:
        with self._lock:
            return {"version": 1, "symbols": {s: [list(self._rows[s][t]) for t in sorted(self._rows[s])] for s in self._rows}}

    def save(self, path: str) -> None:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_json(), f, separators=(",", ":"))
        os.replace(tmp, path)

    def load(self, path: str, now_ms: Optional[int] = None) -> int:
        try:
            with open(path, encoding="utf-8") as f:
                blob = json.load(f)
        except (OSError, ValueError):
            return 0
        if not isinstance(blob, dict) or blob.get("version") != 1:
            return 0
        n = 0
        for sym, rows in (blob.get("symbols") or {}).items():
            good: List[Row] = []
            for r in rows if isinstance(rows, list) else []:
                try:
                    row = (int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
                except (IndexError, TypeError, ValueError):
                    continue
                if now_ms is None or row[0] + H_MS <= now_ms:
                    good.append(row)
            n += self.merge(str(sym), good)
        return n


def window_arrays(store: HourlyBars, symbols: Sequence[str], bars: int) -> Dict[str, Dict[str, np.ndarray]]:
    """Newest ``bars`` completed bars per ready symbol."""
    out = {}
    for sym in symbols:
        a = store.arrays(sym)
        if len(a["t"]) >= bars:
            out[sym] = {k: v[-bars:] for k, v in a.items()}
    return out
