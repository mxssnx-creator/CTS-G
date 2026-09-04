#!/usr/bin/env python3
"""Stable server-side storage paths for CTS runtime state.

The project checkout is disposable.  On a managed server, state is kept in
CTS_DATA_DIR when set, otherwise /var/lib/cts.  Existing installations using
/opt/grok-x01-pulse are discovered and copied forward once, so reinstalling
code does not reset settings, presets, reports, or engine state.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Tuple

PROJECT_DIR = Path(__file__).resolve().parent
ENV_DATA_DIR = (os.environ.get("CTS_DATA_DIR") or os.environ.get("CTS_STATE_DIR") or "").strip()
PRIMARY_DIR = Path(ENV_DATA_DIR).expanduser() if ENV_DATA_DIR else Path("/var/lib/cts")
LEGACY_DIRS = tuple(
    Path(p)
    for p in (
        os.environ.get("CTS_LEGACY_DATA_DIR", "").strip(),
        "/opt/grok-x01-pulse",
        "/opt/cts",
        str(PROJECT_DIR),
    )
    if p
)

# Do not copy stop/pause flags or logs: those are process-control artifacts,
# not durable application state, and copying one could keep a new install down.
_COPY_NAMES = (
    "universe.json",
    "user-presets.json",
    "hist-calc.json",
    "hist-calc-req.json",
    "hist-calc-checkpoint.json",
    "overlay.json",
    "overlay-bingx-x01.json",
    "overlay-bingx-x02.json",
)
_COPY_PREFIXES = (
    "stats-",
    "trades-",
    "block-state-",
    "open-",
    "cts-settings-",
    "errors-",
    "lev-set-",
    "start-eq-",
    "results-export-",
    "history-",
)


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

# Runtime evidence is intentionally bounded.  The exact line cap is shared by
# engine logs, HTTP logs, error JSONL and trade JSONL so an active installation
# cannot grow without limit while still retaining a useful recent tail.
MAX_RETAINED_LINES = min(
    1000,
    max(32, _safe_int(os.environ.get("CTS_MAX_RETAINED_LINES", "1000"), 1000)),
)
MAX_RETAINED_LINE_BYTES = 16 * 1024
MAX_RETAINED_FILE_BYTES = 8 * 1024 * 1024
_APPEND_LOCK = threading.RLock()
_APPEND_COUNTS: Dict[str, int] = {}


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".cts-write-test"
        probe.touch(exist_ok=True)
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def data_dir() -> Path:
    """Return the durable data root, preferring the explicit env override."""
    if ENV_DATA_DIR:
        if not PRIMARY_DIR.is_absolute() or not _writable(PRIMARY_DIR):
            raise RuntimeError("Explicit CTS_DATA_DIR must be absolute and writable")
        return PRIMARY_DIR
    candidates = [PRIMARY_DIR, *LEGACY_DIRS]
    for candidate in candidates:
        if _writable(candidate):
            return candidate
    # A last-resort path keeps offline tests and restricted containers usable.
    fallback = Path(tempfile.gettempdir()) / "cts-g"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


DATA_DIR = data_dir()


def _managed_names(source: Path) -> Iterable[str]:
    try:
        for item in source.iterdir():
            if item.name in _COPY_NAMES or item.name.startswith(_COPY_PREFIXES):
                yield item.name
    except OSError:
        return


def migrate_legacy_state() -> List[str]:
    """Copy recognized legacy state into the primary directory exactly once."""
    if ENV_DATA_DIR:
        # Explicit test/container dirs should never absorb unrelated host data.
        return []
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    copied: List[str] = []
    for source in LEGACY_DIRS:
        if source == DATA_DIR or not source.is_dir():
            continue
        for name in _managed_names(source):
            src = source / name
            dst = DATA_DIR / name
            if not src.is_file() or dst.exists():
                continue
            try:
                shutil.copy2(src, dst)
                copied.append(name)
            except OSError:
                continue
    return copied


MIGRATED_FILES = migrate_legacy_state()


def path_for(name: str) -> str:
    """Return a durable path for a managed file."""
    return str(DATA_DIR / name)


def atomic_write(path: str, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"), allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _tail_lines_locked(
    target: Path,
    *,
    max_lines: int = MAX_RETAINED_LINES,
    max_bytes: int = MAX_RETAINED_FILE_BYTES,
) -> int:
    """Rewrite a text file to a bounded tail while using bounded RAM.

    ``readline`` is capped so a corrupt or unexpectedly verbose single line
    cannot allocate unbounded memory.  The final file is limited by both line
    count and bytes; the newest complete lines win.
    """
    if not target.is_file():
        return 0
    line_limit = min(1000, max(1, _safe_int(max_lines, MAX_RETAINED_LINES)))
    byte_limit = max(1024, _safe_int(max_bytes, MAX_RETAINED_FILE_BYTES))
    lines: Deque[bytes] = deque(maxlen=line_limit)
    try:
        with target.open("rb") as handle:
            while True:
                raw = handle.readline(MAX_RETAINED_LINE_BYTES + 1)
                if not raw:
                    break
                # If the bounded read stopped in the middle of an oversized
                # line, consume only the remainder of that one line before
                # moving on.  Keep a bounded prefix and a normal newline.
                if len(raw) > MAX_RETAINED_LINE_BYTES and not raw.endswith(b"\n"):
                    while True:
                        remainder = handle.readline(MAX_RETAINED_LINE_BYTES + 1)
                        if not remainder or remainder.endswith(b"\n"):
                            break
                line = raw[:MAX_RETAINED_LINE_BYTES]
                if not line.endswith(b"\n"):
                    line += b"\n"
                lines.append(line)
    except OSError:
        return 0

    selected: List[bytes] = []
    total = 0
    for line in reversed(lines):
        if selected and total + len(line) > byte_limit:
            break
        if not selected and len(line) > byte_limit:
            line = line[-byte_limit:]
            if b"\n" in line[:-1]:
                line = line[line.rfind(b"\n", 0, -1) + 1 :]
        selected.append(line)
        total += len(line)
    payload = b"".join(reversed(selected))
    try:
        mode = target.stat().st_mode & 0o777
        fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, mode)
            os.replace(tmp_name, target)
        except Exception:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass
            raise
    except OSError:
        return 0
    return len(selected)


def retain_last_lines(
    path: str,
    *,
    max_lines: int = MAX_RETAINED_LINES,
    max_bytes: int = MAX_RETAINED_FILE_BYTES,
) -> int:
    """Keep at most the newest bounded text lines in ``path``."""
    with _APPEND_LOCK:
        return _tail_lines_locked(Path(path), max_lines=max_lines, max_bytes=max_bytes)


def append_bounded_lines(
    path: str,
    lines: Iterable[str],
    *,
    max_lines: int = MAX_RETAINED_LINES,
    max_bytes: int = MAX_RETAINED_FILE_BYTES,
    compact_every: int = 128,
) -> int:
    """Append lines and periodically compact the file to the shared tail cap."""
    line_limit = min(1000, max(1, _safe_int(max_lines, MAX_RETAINED_LINES)))
    values: Deque[str] = deque(maxlen=line_limit)
    for line in lines:
        if line is None:
            continue
        value = str(line)
        values.append(value if value.endswith("\n") else value + "\n")
    if not values:
        return 0
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    key = str(target)
    with _APPEND_LOCK:
        known = _APPEND_COUNTS.get(key)
        if known is None:
            known = _tail_lines_locked(target, max_lines=line_limit, max_bytes=max_bytes)
        try:
            with target.open("a", encoding="utf-8") as handle:
                handle.writelines(values)
        except OSError:
            return 0
        pending = known + len(values)
        try:
            too_large = target.stat().st_size > max_bytes
        except OSError:
            too_large = False
        # ``known`` is the retained line count from the last write. Compact
        # immediately on a cap/byte breach; this prevents the application
        # writers from accumulating an unbounded tail between timer passes.
        # Keep compact_every as a compatibility knob for callers that want
        # extra normalization, but never allow it to weaken the hard cap.
        normalize = pending >= max(1, int(compact_every)) and pending >= line_limit
        if pending > line_limit or too_large or normalize:
            kept = _tail_lines_locked(target, max_lines=line_limit, max_bytes=max_bytes)
            _APPEND_COUNTS[key] = kept
            return kept
        _APPEND_COUNTS[key] = min(line_limit, pending)
        return pending


def append_bounded_line(
    path: str,
    line: str,
    *,
    max_lines: int = MAX_RETAINED_LINES,
    max_bytes: int = MAX_RETAINED_FILE_BYTES,
) -> int:
    """Append one line using the shared bounded writer."""
    return append_bounded_lines(path, (line,), max_lines=max_lines, max_bytes=max_bytes)


def read_jsonl(
    path: str,
    *,
    cutoff: float = 0.0,
    max_rows: int = MAX_RETAINED_LINES,
) -> List[Dict[str, Any]]:
    """Read valid JSONL rows without allowing history to grow in RAM."""
    limit = min(1000, max(1, _safe_int(max_rows, MAX_RETAINED_LINES)))
    out: Deque[Dict[str, Any]] = deque(maxlen=limit)
    try:
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    ts = float(row.get("t") or 0) if isinstance(row, dict) else 0.0
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(row, dict) and ts >= cutoff:
                    out.append(row)
    except OSError:
        pass
    return list(out)


def append_jsonl(path: str, rows: Iterable[Dict[str, Any]], *, cutoff: float) -> int:
    """Merge rows into a bounded JSONL history by stable event identity."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    current: Dict[str, Dict[str, Any]] = {}
    for row in read_jsonl(str(target), cutoff=cutoff):
        key = str(row.get("eventId") or "")
        if key:
            current[key] = row
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            retained = float(row.get("t") or 0) >= cutoff
        except (TypeError, ValueError):
            retained = False
        if not retained:
            continue
        key = str(row.get("eventId") or "")
        if key:
            current[key] = row
    ordered = sorted(current.values(), key=lambda row: float(row.get("t") or 0))[-MAX_RETAINED_LINES:]
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in ordered:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    return len(ordered)


def self_test() -> List[Tuple[str, bool, str]]:
    """Exercise the bounded text/JSONL paths entirely inside a temp dir."""
    with tempfile.TemporaryDirectory(prefix="cts-storage-test-") as root:
        log_path = Path(root) / "engine.log"
        log_path.write_text("".join(f"line-{i}\n" for i in range(1205)), encoding="utf-8")
        kept = retain_last_lines(str(log_path))
        lines = log_path.read_text(encoding="utf-8").splitlines()
        rows: List[Tuple[str, bool, str]] = []
        rows.append(("tail-count", kept == 1000 and len(lines) == 1000, f"kept={kept} lines={len(lines)}"))
        rows.append(("tail-last", lines[-1:] == ["line-1204"], str(lines[-1:])))

        for i in range(1205, 1305):
            append_bounded_line(str(log_path), f"line-{i}")
        lines = log_path.read_text(encoding="utf-8").splitlines()
        rows.append(("append-hard-cap", len(lines) <= 1000 and lines[-1:] == ["line-1304"], f"lines={len(lines)}"))

        jsonl_path = Path(root) / "events.jsonl"
        jsonl_path.write_text(
            "".join(json.dumps({"t": i, "eventId": str(i)}) + "\n" for i in range(1205)),
            encoding="utf-8",
        )
        parsed = read_jsonl(str(jsonl_path))
        rows.append(("jsonl-hard-cap", len(parsed) == 1000 and parsed[0].get("t") == 205, f"rows={len(parsed)}"))

        huge_path = Path(root) / "huge.log"
        huge_path.write_text("old\n" + ("x" * (MAX_RETAINED_FILE_BYTES + 1024)) + "\nnew\n", encoding="utf-8")
        retain_last_lines(str(huge_path))
        rows.append(("byte-cap", huge_path.stat().st_size <= MAX_RETAINED_FILE_BYTES, str(huge_path.stat().st_size)))
        return rows


def storage_info() -> Dict[str, Any]:
    names = sorted(p.name for p in DATA_DIR.iterdir()) if DATA_DIR.exists() else []
    return {
        "dataDir": str(DATA_DIR),
        "primaryConfigured": bool(ENV_DATA_DIR),
        "persistent": DATA_DIR != Path(tempfile.gettempdir()) / "cts-g",
        "legacyDirs": [str(p) for p in LEGACY_DIRS if p != DATA_DIR],
        "migrated": list(MIGRATED_FILES),
        "recognizedFiles": names,
        "retentionDays": 35,
        "retentionSeconds": 35 * 24 * 60 * 60,
        "maxRetainedLines": MAX_RETAINED_LINES,
        "maxRetainedLineBytes": MAX_RETAINED_LINE_BYTES,
        "maxRetainedFileBytes": MAX_RETAINED_FILE_BYTES,
        "updatedAt": time.time(),
    }


if __name__ == "__main__":
    print(json.dumps(storage_info(), separators=(",", ":")))

def ensure_storage_info() -> Dict[str, Any]:
    """Write a non-secret manifest so operators can verify discovery."""
    info = storage_info()
    try:
        atomic_write(path_for("storage-info.json"), info)
    except OSError:
        pass
    return info


ensure_storage_info()


__all__ = [
    "DATA_DIR",
    "MAX_RETAINED_LINES",
    "MAX_RETAINED_LINE_BYTES",
    "MAX_RETAINED_FILE_BYTES",
    "path_for",
    "atomic_write",
    "read_jsonl",
    "append_jsonl",
    "append_bounded_line",
    "append_bounded_lines",
    "retain_last_lines",
    "storage_info",
    "self_test",
    "ensure_storage_info",
]
