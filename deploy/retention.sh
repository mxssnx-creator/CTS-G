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

case "$MAX_LINES" in
  ''|*[!0-9]*) MAX_LINES=1000 ;;
esac
(( MAX_LINES > 0 && MAX_LINES <= 1000 )) || MAX_LINES=1000

[[ "${1:---once}" == "--once" ]] || {
  printf 'usage: %s --once\n' "$0" >&2
  exit 2
}

PYTHONPATH="$PULSE_DIR:$CTS_G_ROOT/server/pulse${PYTHONPATH:+:$PYTHONPATH}" \
  python3 - "$CTS_DATA_DIR" "$LOG_DIR" "$PULSE_DIR" "$MAX_LINES" <<'PY'
from __future__ import annotations

import os
import json
import sys
from pathlib import Path

from storage_paths import MAX_RETAINED_FILE_BYTES, retain_last_lines
from system_settings import normalize_system_settings

data_dir, log_dir, pulse_dir = (Path(x) for x in sys.argv[1:4])
max_lines = int(sys.argv[4])
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
        candidates = root.rglob("*")
    except OSError:
        continue
    for path in candidates:
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in suffixes:
            continue
        files.append(path)

trimmed = 0
for path in sorted(set(files)):
    try:
        before = path.stat().st_size
        selected = [value for lane, value in limits.items() if lane in path.name] or list(limits.values())
        kept = retain_last_lines(
            str(path),
            max_lines=min(max_lines, min(value["systemLogMaxLines"] for value in selected)),
            max_bytes=min(MAX_RETAINED_FILE_BYTES, int(min(value["systemLogMaxMb"] for value in selected)*1048576)),
        )
        after = path.stat().st_size
    except OSError:
        continue
    trimmed += 1
    if before != after:
        print(f"retained {path} lines={kept} bytes={before}->{after}")

print(f"retention complete files={trimmed} maxLines={max_lines} maxBytes={MAX_RETAINED_FILE_BYTES}")
PY
