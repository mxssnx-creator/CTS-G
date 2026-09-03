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
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List

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
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)


def read_jsonl(path: str, *, cutoff: float = 0.0) -> List[Dict[str, Any]]:
    """Read valid, retained JSONL rows without allowing one bad line to abort."""
    out: List[Dict[str, Any]] = []
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
    return out


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
    ordered = sorted(current.values(), key=lambda row: float(row.get("t") or 0))
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in ordered:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    return len(ordered)


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


__all__ = ["DATA_DIR", "path_for", "atomic_write", "read_jsonl", "append_jsonl", "storage_info", "ensure_storage_info"]
