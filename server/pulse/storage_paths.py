#!/usr/bin/env python3
"""Stable server-side storage paths for CTS runtime state.

The project checkout is disposable. On a managed server, state is kept in
CTS_DATA_DIR when set, otherwise /var/lib/cts/instances/<name>/data. Existing
installations migrate only from an explicitly configured legacy data directory;
reinstalling code does not reset settings, presets, reports, or engine state.
"""
from __future__ import annotations

import json
import fcntl
import os
import re
import shutil
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Tuple

PROJECT_DIR = Path(__file__).resolve().parent
ENV_DATA_DIR = (os.environ.get("CTS_DATA_DIR") or "").strip()
if not ENV_DATA_DIR and os.environ.get("CTS_STATE_DIR"):
    ENV_DATA_DIR = str(Path(os.environ["CTS_STATE_DIR"]) / "data")
INSTANCE_NAME = os.environ.get("CTS_G_NAME", "cts-g")
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", INSTANCE_NAME):
    raise ValueError("invalid CTS_G_NAME")
PRIMARY_DIR = (
    Path(ENV_DATA_DIR).expanduser()
    if ENV_DATA_DIR
    else Path("/var/lib/cts/instances") / INSTANCE_NAME / "data"
)
LEGACY_DIRS = tuple(
    Path(p)
    for p in (
        os.environ.get("CTS_LEGACY_DATA_DIR", "").strip(),
    )
    if p
)

# Stop/pause intent is durable safety state. Migration is explicitly scoped;
# an unrelated instance must never discover another project's credentials/state.
_COPY_NAMES = (
    "STOP",
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
    "STOP-",
    "PAUSE-",
    "RUN-",
    "pending-",
    "events-",
    "live-position-cost-",
    "credentials-",
    "stats-",
    "trades-",
    "block-state-",
    "open-",
    "cts-settings-",
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
    if _writable(PRIMARY_DIR):
        return PRIMARY_DIR
    if ENV_DATA_DIR:
        raise RuntimeError("configured CTS data directory is not writable")
    # A last-resort path keeps offline tests and restricted containers usable.
    fallback = Path(tempfile.gettempdir()) / (INSTANCE_NAME + "-data")
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


DATA_DIR = data_dir()


def _positive_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


LOG_MAX_LINES = _positive_int("CTS_LOG_MAX_LINES", 1000, 100, 1000)
LOG_MAX_BYTES = _positive_int("CTS_LOG_MAX_BYTES", 8 * 1024 * 1024, 1024 * 1024, 64 * 1024 * 1024)
ENV_LOG_DIR = os.environ.get("CTS_LOG_DIR", "").strip()
LOG_DIR = Path(ENV_LOG_DIR).expanduser() if ENV_LOG_DIR else DATA_DIR / "logs"
if not _writable(LOG_DIR):
    LOG_DIR = DATA_DIR / "logs"
    LOG_DIR.mkdir(parents=True, exist_ok=True)

try:
    REDIS_DB = int(os.environ.get("CTS_REDIS_DB", "1"))
except (TypeError, ValueError):
    REDIS_DB = 1
if REDIS_DB < 0 or REDIS_DB > 15:
    REDIS_DB = 1



def _managed_names(source: Path) -> Iterable[str]:
    try:
        for item in source.iterdir():
            if item.name in _COPY_NAMES or item.name.startswith(_COPY_PREFIXES):
                yield item.name
    except OSError:
        return


def migrate_legacy_state() -> List[str]:
    """Copy recognized legacy state into the primary directory exactly once."""
    if DATA_DIR != PRIMARY_DIR and ENV_DATA_DIR:
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
            if src.is_symlink() or not src.is_file() or dst.exists():
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
    if Path(name).name != name or name in ("", ".", ".."):
        raise ValueError("managed state requires a filename")
    return str(DATA_DIR / name)


def log_path(name: str) -> str:
    """Return a bounded diagnostic-log path outside authoritative state."""
    safe_name = Path(name).name
    return str(LOG_DIR / safe_name)


def redis_cli_args(*args: object) -> List[str]:
    """Build a redis-cli command scoped to this installation's logical DB."""
    return ["redis-cli", "-n", str(REDIS_DB), *(str(value) for value in args)]


def trim_text_log(path: str, *, max_lines: int = LOG_MAX_LINES, max_bytes: int = LOG_MAX_BYTES) -> None:
    retain_last_lines(path, max_lines=max_lines, max_bytes=max_bytes)


def append_log(path: str, line: str) -> None:
    append_bounded_line(path, line, max_lines=LOG_MAX_LINES, max_bytes=LOG_MAX_BYTES)


def atomic_write(path: str, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def _trim_handle(handle: Any, max_lines: int, max_bytes: int) -> int:
    """Bounded suffix read; keep inode so open stdout writers remain attached."""
    max_lines = min(1000, max(1, int(max_lines)))
    max_bytes = min(MAX_RETAINED_FILE_BYTES, max(1024, int(max_bytes)))
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    offset = max(0, size - max_bytes)
    handle.seek(offset)
    tail = handle.read(max_bytes)
    if offset:
        end = tail.find(b"\n")
        tail = tail[end + 1:] if end >= 0 else b""
    lines = tail.splitlines(keepends=True)[-max_lines:]
    payload = b"".join(lines)
    if offset or payload != tail:
        handle.seek(0)
        handle.write(payload)
        handle.truncate()
        handle.flush()
    return len(lines)


def retain_last_lines(
    path: str, *, max_lines: int = MAX_RETAINED_LINES,
    max_bytes: int = MAX_RETAINED_FILE_BYTES,
) -> int:
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        return 0
    with _APPEND_LOCK:
        try:
            fd = os.open(target, os.O_RDWR | os.O_NOFOLLOW)
            with os.fdopen(fd, "r+b") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                return _trim_handle(handle, max_lines, max_bytes)
        except OSError:
            return 0


def append_bounded_lines(
    path: str, lines: Iterable[str], *, max_lines: int = MAX_RETAINED_LINES,
    max_bytes: int = MAX_RETAINED_FILE_BYTES, compact_every: int = 128,
) -> int:
    """Serialized across processes; both caps hold after every append batch."""
    del compact_every
    values: Deque[bytes] = deque(maxlen=min(1000, max(1, int(max_lines))))
    for line in lines:
        if line is not None:
            value = str(line).rstrip("\n")[:MAX_RETAINED_LINE_BYTES]
            values.append((value + "\n").encode("utf-8"))
    if not values:
        return 0
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _APPEND_LOCK:
        try:
            fd = os.open(target, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "r+b") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                handle.seek(0, os.SEEK_END)
                handle.writelines(values)
                handle.flush()
                return _trim_handle(handle, max_lines, max_bytes)
        except OSError:
            return 0


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
        "logDir": str(LOG_DIR),
        "logMaxLines": LOG_MAX_LINES,
        "logMaxBytes": LOG_MAX_BYTES,
        "redisDb": REDIS_DB,
        "primaryConfigured": bool(ENV_DATA_DIR),
        "persistent": DATA_DIR == PRIMARY_DIR,
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
    "LOG_DIR",
    "LOG_MAX_LINES",
    "LOG_MAX_BYTES",
    "REDIS_DB",
    "path_for",
    "log_path",
    "redis_cli_args",
    "trim_text_log",
    "append_log",
    "atomic_write",
    "read_jsonl",
    "append_jsonl",
    "storage_info",
    "MAX_RETAINED_LINES",
    "MAX_RETAINED_LINE_BYTES",
    "MAX_RETAINED_FILE_BYTES",
    "append_bounded_line",
    "append_bounded_lines",
    "retain_last_lines",
    "self_test",
    "ensure_storage_info",
]
