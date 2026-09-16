#!/usr/bin/env bash
# Keep CTS-G runtime evidence bounded without touching settings or positions.
set -euo pipefail

CTS_G_NAME="${CTS_G_NAME:-cts-g}"
ENV_FILE="${ENV_FILE:-/etc/${CTS_G_NAME}/cts-g.env}"
if [[ -r "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

CTS_G_NAME="${CTS_G_NAME:-cts-g}"
CTS_G_ROOT="${CTS_G_ROOT:-/opt/${CTS_G_NAME}}"
PULSE_DIR="${PULSE_DIR:-/opt/${CTS_G_NAME}-pulse}"
CTS_DATA_DIR="${CTS_DATA_DIR:-/var/lib/${CTS_G_NAME}}"
LOG_DIR="${LOG_DIR:-/var/log/${CTS_G_NAME}}"
MAX_LINES="${CTS_MAX_RETAINED_LINES:-1000}"
MAX_ERROR_LINES="${CTS_MAX_ERROR_LOG_LINES:-500}"

case "$MAX_LINES" in
  ''|*[!0-9]*) MAX_LINES=1000 ;;
esac
(( MAX_LINES > 0 && MAX_LINES <= 1000 )) || MAX_LINES=1000
case "$MAX_ERROR_LINES" in
  ''|*[!0-9]*) MAX_ERROR_LINES=500 ;;
esac
(( MAX_ERROR_LINES > 0 && MAX_ERROR_LINES <= 500 )) || MAX_ERROR_LINES=500

[[ "${1:---once}" == "--once" ]] || {
  printf 'usage: %s --once\n' "$0" >&2
  exit 2
}

PYTHONPATH="$PULSE_DIR:$CTS_G_ROOT/server/pulse${PYTHONPATH:+:$PYTHONPATH}" \
  python3 - "$CTS_DATA_DIR" "$LOG_DIR" "$PULSE_DIR" "$MAX_LINES" "$MAX_ERROR_LINES" <<'PY'
from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path

from storage_paths import MAX_RETAINED_FILE_BYTES, retain_last_lines
from system_settings import normalize_system_settings

data_dir, log_dir, pulse_dir = (Path(x) for x in sys.argv[1:4])
max_lines = int(sys.argv[4])
max_error_lines = int(sys.argv[5]) if len(sys.argv) > 5 else 500
max_error_lines = max(32, min(500, max_error_lines))
limits = {}
for lane in ("bingx-x01", "bingx-x02"):
    try:
        raw = json.loads((data_dir / f"overlay-{lane}.json").read_text())
    except (OSError, ValueError):
        raw = {}
    limits[lane] = normalize_system_settings(raw)
roots = []
seen = set()
for root in (data_dir, log_dir, pulse_dir):
    try:
        resolved = root.resolve()
    except OSError:
        continue
    if resolved in seen or not resolved.is_dir():
        continue
    seen.add(resolved)
    roots.append(resolved)

suffixes = (".log", ".jsonl", ".out", ".err")
files = []
for root in roots:
    try:
        # Depth-bounded walk: never scan SQLite backups or nested report trees.
        for dirpath, dirnames, filenames in os.walk(root):
            rel = Path(dirpath)
            depth = len(rel.relative_to(root).parts) if rel != root else 0
            if depth >= 3:
                dirnames[:] = []
            skip = {"backups", "node_modules", ".git", "__pycache__", "statistics"}
            dirnames[:] = [name for name in dirnames if name not in skip]
            for name in filenames:
                path = Path(dirpath) / name
                if path.suffix.lower() not in suffixes:
                    continue
                files.append(path)
                if len(files) >= 400:
                    break
            if len(files) >= 400:
                break
    except OSError:
        continue
    if len(files) >= 400:
        break

trimmed = 0
for path in sorted(set(files)):
    try:
        before = path.stat().st_size
        selected = [value for lane, value in limits.items() if lane in path.name] or list(limits.values())
        selected_max_lines = min(max_lines, min(value["systemLogMaxLines"] for value in selected))
        if path.name.startswith("errors-") or path.name.endswith(".err") or path.name.endswith(".err.log"):
            selected_max_lines = min(selected_max_lines, max_error_lines)
        kept = retain_last_lines(
            str(path),
            max_lines=selected_max_lines,
            max_bytes=min(MAX_RETAINED_FILE_BYTES, int(min(value["systemLogMaxMb"] for value in selected)*1048576)),
        )
        after = path.stat().st_size
    except OSError:
        continue
    trimmed += 1
    if before != after:
        print(f"retained {path} lines={kept} bytes={before}->{after}")

print(f"retention complete files={trimmed} maxLines={max_lines} maxErrorLines={max_error_lines} maxBytes={MAX_RETAINED_FILE_BYTES}")

prefix = str(os.environ.get("CTS_REDIS_PREFIX") or "")
ttl_s = 21600
try:
    ttl_s = int(min(value["systemRedisCalcTtlS"] for value in limits.values()))
except (KeyError, ValueError, TypeError):
    ttl_s = 21600
ttl_s = max(300, min(86400, ttl_s))
# Only expire this installation's calc cache. Never SCAN shared KN/indication keys.
patterns = []
if prefix.endswith(":"):
    patterns.append(prefix + "cts-calc:*")
patterns.append("cts-calc:*")
expired = 0
scanned = 0
try:
    for pattern in patterns:
        cur = "0"
        while scanned < 2000:
            raw = subprocess.check_output(
                ["redis-cli", "--raw", "SCAN", cur, "MATCH", pattern, "COUNT", "100"],
                text=True, timeout=2,
            )
            parts = [p for p in raw.split("\n") if p != ""]
            if not parts:
                break
            cur = parts[0]
            for key in parts[1:]:
                scanned += 1
                if "cts-calc:" not in key:
                    continue
                try:
                    t = subprocess.check_output(["redis-cli", "--raw", "TTL", key], text=True, timeout=1).strip()
                except (OSError, subprocess.SubprocessError):
                    continue
                if t == "-1":
                    subprocess.check_output(["redis-cli", "EXPIRE", key, str(ttl_s)], timeout=1)
                    expired += 1
            if cur == "0":
                break
    if expired:
        print(f"expired {expired} cts-calc keys ttl={ttl_s} scanned={scanned}")
except (OSError, subprocess.SubprocessError, FileNotFoundError):
    pass
PY
