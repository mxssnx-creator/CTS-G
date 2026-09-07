"""Durable, lane-isolated 1m OHLCV storage for the rolling replay worker.

The replay engine intentionally consumes the legacy ``[open, high, low, close,
volume]`` shape.  This store keeps the minute key and provenance beside that
shape so a restart, overlap, or exchange gap cannot silently become a synthetic
continuous tape.
"""
from __future__ import annotations

import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from storage_paths import atomic_write, path_for

BAR_S = 60
SOURCE_PRIORITY = {
    "seed": 0,
    "mark": 1,
    "websocket": 2,
    "exchange": 3,
}


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def minute_key(value: Any) -> int:
    """Normalize seconds, milliseconds, or an already-normalized minute key."""
    parsed = _finite(value)
    if parsed <= 0:
        return 0
    if parsed >= 100_000_000_000:
        return int(parsed // 60_000)
    if parsed >= 1_000_000_000:
        return int(parsed // 60)
    # Small values are useful in deterministic tests and are already minute
    # keys.  Treating them as seconds would make their ordering surprising.
    return int(parsed)


def _bar(values: Sequence[Any]) -> Optional[List[float]]:
    if len(values) < 5:
        return None
    try:
        opening, high, low, close, volume = (_finite(v) for v in values[:5])
    except TypeError:
        return None
    if opening <= 0 or high <= 0 or low <= 0 or close <= 0:
        return None
    if high + 1e-12 < max(opening, close) or low - 1e-12 > min(opening, close):
        return None
    if low > high:
        return None
    return [opening, high, low, close, max(0.0, volume)]


def parse_row(row: Any) -> Optional[Tuple[int, List[float], bool, str]]:
    """Parse BingX rows and already-normalized rows without losing timestamps."""
    if isinstance(row, dict):
        if row.get("bar") is not None:
            parsed = _bar(row.get("bar") or [])
            if parsed:
                return minute_key(row.get("minute") or row.get("timestamp") or row.get("time") or 0), parsed, bool(row.get("closed", True)), str(row.get("source") or "")
            return None
        raw_time = next(
            (row.get(key) for key in ("timestamp", "time", "t", "openTime", "startTime", "ts") if row.get(key) is not None),
            0,
        )
        values = [
            row.get("open", row.get("o")),
            row.get("high", row.get("h")),
            row.get("low", row.get("l")),
            row.get("close", row.get("c")),
            row.get("volume", row.get("v", 0)),
        ]
        parsed = _bar(values)
        if not parsed:
            return None
        return minute_key(raw_time), parsed, bool(row.get("closed", True)), str(row.get("source") or "")
    if not isinstance(row, (list, tuple)):
        return None
    if len(row) >= 6:
        timestamp = minute_key(row[0])
        parsed = _bar(row[1:6])
        if timestamp and parsed:
            return timestamp, parsed, True, ""
    parsed = _bar(row[:5])
    if parsed:
        return 0, parsed, True, ""
    return None


def parse_exchange_rows(rows: Any) -> List[Dict[str, Any]]:
    """Return timestamped, validated exchange records for a store merge."""
    if not isinstance(rows, list):
        return []
    out: List[Dict[str, Any]] = []
    for row in rows:
        parsed = parse_row(row)
        if not parsed or parsed[0] <= 0:
            continue
        timestamp, bar, closed, row_source = parsed
        out.append({"minute": timestamp, "bar": bar, "closed": closed, "source": row_source or "exchange"})
    out.sort(key=lambda item: int(item["minute"]))
    return out


def _safe_connection(connection: str) -> str:
    value = "".join(ch for ch in str(connection or "default") if ch.isalnum() or ch in "._-")
    return value or "default"


class HistoryStore:
    """Atomic, bounded, lane-isolated store of timestamped 1m bars."""

    def __init__(
        self,
        connection: str,
        *,
        retention_bars: int = 600,
        path: Optional[str] = None,
        checkpoint_path: Optional[str] = None,
    ) -> None:
        self.connection = _safe_connection(connection)
        self.retention_bars = max(120, int(retention_bars or 600))
        self.path = path or path_for(f"history-1m-{self.connection}.json")
        self.checkpoint_path = checkpoint_path or path_for(f"history-checkpoint-{self.connection}.json")
        self._lock = threading.RLock()
        self._rows: Dict[str, Dict[int, Dict[str, Any]]] = {}
        self._loaded = False
        self._dirty = False
        self._last_persist_mono = 0.0
        self._persist_interval_s = 3.0
        self._load()

    def _load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            try:
                if not os.path.exists(self.path):
                    return
                import json

                with open(self.path, encoding="utf-8") as handle:
                    payload = json.load(handle)
                symbols = payload.get("symbols") if isinstance(payload, dict) else {}
                if not isinstance(symbols, dict):
                    return
                for symbol, rows in symbols.items():
                    if not isinstance(rows, dict):
                        continue
                    target: Dict[int, Dict[str, Any]] = {}
                    for raw_minute, record in rows.items():
                        try:
                            key = int(raw_minute)
                        except (TypeError, ValueError):
                            continue
                        if not isinstance(record, dict):
                            continue
                        bar = _bar(record.get("bar") or [])
                        if not bar or key <= 0:
                            continue
                        target[key] = {
                            "bar": bar,
                            "source": str(record.get("source") or "exchange"),
                            "quality": str(record.get("quality") or "confirmed"),
                            "closed": bool(record.get("closed", True)),
                            "receivedAt": _finite(record.get("receivedAt"), 0.0),
                        }
                    if target:
                        self._rows[str(symbol)] = self._compact_rows(target)
            except (OSError, ValueError, TypeError):
                self._rows = {}

    def _compact_rows(self, rows: Dict[int, Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
        if len(rows) <= self.retention_bars:
            return rows
        keep = sorted(rows)[-self.retention_bars :]
        return {key: rows[key] for key in keep}

    def _persist_locked(self) -> None:
        payload = {
            "version": 2,
            "connection": self.connection,
            "updatedAt": time.time(),
            "retentionBars": self.retention_bars,
            "symbols": {
                symbol: {str(minute): record for minute, record in sorted(rows.items())}
                for symbol, rows in sorted(self._rows.items())
                if rows
            },
        }
        atomic_write(self.path, payload)

    def _maybe_persist_locked(self, *, force: bool = False) -> bool:
        """Write the on-disk snapshot at most once per interval unless forced.

        A 500-symbol lane is tens of megabytes of JSON. Dumping it after every
        merge holds the GIL long enough to starve systemd watchdog pings.
        """
        if not self._dirty:
            return False
        now = time.monotonic()
        interval = max(0.25, float(getattr(self, "_persist_interval_s", 3.0) or 3.0))
        if not force and self._last_persist_mono > 0.0 and (now - self._last_persist_mono) < interval:
            return False
        self._persist_locked()
        self._dirty = False
        self._last_persist_mono = now
        return True

    def flush(self) -> bool:
        """Persist any deferred bars. Safe to call from a heartbeat thread."""
        with self._lock:
            return self._maybe_persist_locked(force=True)

    def _candidate_source(self, source: str, row_source: str = "") -> str:
        value = str(row_source or source or "exchange").strip().lower()
        return value if value in SOURCE_PRIORITY else str(source or "exchange").strip().lower() or "exchange"

    def merge(
        self,
        symbol: str,
        rows: Iterable[Any],
        *,
        source: str = "exchange",
        quality: str = "confirmed",
        closed: bool = True,
        persist: bool = True,
        received_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Merge overlapping rows, preferring exchange-confirmed evidence."""
        name = str(symbol or "").strip().upper()
        if not name:
            return {"inserted": 0, "replaced": 0, "ignored": 0, "watermark": 0}
        now = float(received_at or time.time())
        inserted = replaced = ignored = 0
        with self._lock:
            target = self._rows.setdefault(name, {})
            for raw in rows:
                parsed = parse_row(raw)
                if not parsed:
                    ignored += 1
                    continue
                minute, bar, row_closed, row_source = parsed
                if minute <= 0:
                    ignored += 1
                    continue
                candidate_source = self._candidate_source(source, row_source)
                candidate = {
                    "bar": bar,
                    "source": candidate_source,
                    "quality": str(quality or "confirmed"),
                    "closed": bool(row_closed and closed),
                    "receivedAt": now,
                }
                previous = target.get(minute)
                if previous is None:
                    target[minute] = candidate
                    inserted += 1
                    continue
                old_rank = SOURCE_PRIORITY.get(str(previous.get("source") or ""), 0)
                new_rank = SOURCE_PRIORITY.get(candidate_source, 0)
                if bool(previous.get("closed", True)) and not candidate["closed"]:
                    ignored += 1
                    continue
                if new_rank < old_rank or (new_rank == old_rank and _finite(previous.get("receivedAt"), 0) > now):
                    ignored += 1
                    continue
                if previous.get("bar") == candidate["bar"] and previous.get("source") == candidate_source and previous.get("closed") == candidate["closed"]:
                    ignored += 1
                    continue
                target[minute] = candidate
                replaced += 1
            self._rows[name] = self._compact_rows(target)
            if inserted or replaced:
                self._dirty = True
                if persist:
                    self._maybe_persist_locked(force=False)
            watermark = max(self._rows[name], default=0)
        return {"inserted": inserted, "replaced": replaced, "ignored": ignored, "watermark": watermark}

    def merge_bar(
        self,
        symbol: str,
        timestamp: Any,
        bar: Sequence[Any],
        *,
        source: str = "mark",
        quality: str = "observed",
        closed: bool = True,
        persist: bool = True,
    ) -> Dict[str, Any]:
        return self.merge(
            symbol,
            [{"timestamp": timestamp, "open": bar[0], "high": bar[1], "low": bar[2], "close": bar[3], "volume": bar[4] if len(bar) > 4 else 0, "closed": closed}],
            source=source,
            quality=quality,
            closed=closed,
            persist=persist,
        )

    def records(
        self,
        symbol: str,
        *,
        start: Optional[int] = None,
        end: Optional[int] = None,
        source: Optional[str] = None,
        closed_only: bool = True,
    ) -> List[Dict[str, Any]]:
        name = str(symbol or "").strip().upper()
        with self._lock:
            rows = self._rows.get(name, {})
            out: List[Dict[str, Any]] = []
            for minute in sorted(rows):
                if start is not None and minute < int(start):
                    continue
                if end is not None and minute > int(end):
                    continue
                record = rows[minute]
                if closed_only and not bool(record.get("closed", True)):
                    continue
                if source is not None:
                    if source == "exchange" and str(record.get("source")) != "exchange":
                        continue
                    if source != "exchange" and str(record.get("source")) != source:
                        continue
                out.append({"minute": minute, **record, "bar": list(record.get("bar") or [])})
            return out

    def window_records(
        self,
        symbol: str,
        *,
        bars: int,
        end: Optional[int] = None,
        source: Optional[str] = "exchange",
        closed_only: bool = True,
    ) -> List[Dict[str, Any]]:
        last = int(end or (int(time.time()) // BAR_S - 1))
        first = last - max(1, int(bars)) + 1
        return self.records(symbol, start=first, end=last, source=source, closed_only=closed_only)

    def window(
        self,
        symbol: str,
        *,
        bars: int,
        end: Optional[int] = None,
        source: Optional[str] = "exchange",
        closed_only: bool = True,
    ) -> List[List[float]]:
        return [list(record["bar"]) for record in self.window_records(symbol, bars=bars, end=end, source=source, closed_only=closed_only)]

    def missing_ranges(
        self,
        symbol: str,
        start: int,
        end: int,
        *,
        source: Optional[str] = "exchange",
        closed_only: bool = True,
        error: str = "missing exchange minute",
    ) -> List[Dict[str, Any]]:
        first = int(start)
        last = int(end)
        if last < first:
            return []
        present = {record["minute"] for record in self.records(symbol, start=first, end=last, source=source, closed_only=closed_only)}
        gaps: List[Dict[str, Any]] = []
        gap_start = 0
        for minute in range(first, last + 1):
            if minute in present:
                if gap_start:
                    gaps.append(self._gap(gap_start, minute - 1, error, source))
                    gap_start = 0
                continue
            if not gap_start:
                gap_start = minute
        if gap_start:
            gaps.append(self._gap(gap_start, last, error, source))
        return gaps

    @staticmethod
    def _gap(start: int, end: int, error: str, source: Optional[str]) -> Dict[str, Any]:
        return {"start": int(start), "end": int(end), "minutes": int(end - start + 1), "source": source or "any", "error": error}

    def coverage(
        self,
        symbol: str,
        start: int,
        end: int,
        *,
        source: Optional[str] = "exchange",
        closed_only: bool = True,
    ) -> Dict[str, Any]:
        requested = max(0, int(end) - int(start) + 1)
        records = self.records(symbol, start=start, end=end, source=source, closed_only=closed_only)
        gaps = self.missing_ranges(symbol, start, end, source=source, closed_only=closed_only)
        source_counts: Dict[str, int] = {}
        for record in self.records(symbol, start=start, end=end, source=None, closed_only=closed_only):
            key = str(record.get("source") or "unknown")
            source_counts[key] = source_counts.get(key, 0) + 1
        return {
            "symbol": str(symbol),
            "requested": requested,
            "present": len(records),
            "missing": sum(int(gap["minutes"]) for gap in gaps),
            "coveragePct": round(100.0 * len(records) / requested, 2) if requested else 100.0,
            "contiguous": not gaps,
            "gaps": gaps,
            "sourceCounts": source_counts,
            "watermark": max((int(record["minute"]) for record in records), default=0),
        }

    def watermark(self, symbol: Optional[str] = None, *, source: Optional[str] = None) -> int | Dict[str, int]:
        with self._lock:
            if symbol is not None:
                rows = self.records(symbol, source=source, closed_only=True)
                return max((int(record["minute"]) for record in rows), default=0)
            return {
                name: max((int(record["minute"]) for record in self.records(name, source=source, closed_only=True)), default=0)
                for name in sorted(self._rows)
            }

    def checkpoint(self, values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if values is not None:
            payload = {"version": 1, "connection": self.connection, "updatedAt": time.time(), **dict(values)}
            atomic_write(self.checkpoint_path, payload)
            return payload
        try:
            import json

            with open(self.checkpoint_path, encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def symbols(self) -> List[str]:
        with self._lock:
            return sorted(self._rows)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "connection": self.connection,
                "path": str(Path(self.path)),
                "retentionBars": self.retention_bars,
                "symbols": self.symbols(),
                "watermark": self.watermark(),
            }


__all__ = [
    "BAR_S",
    "HistoryStore",
    "SOURCE_PRIORITY",
    "minute_key",
    "parse_exchange_rows",
    "parse_row",
]
