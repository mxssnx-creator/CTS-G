#!/usr/bin/env bash
# Bound high-churn CTS and host diagnostic text logs. Authoritative state,
# credentials, databases, reports and backups are intentionally never scanned.
set -euo pipefail
umask 027

MAX_LINES="${CTS_LOG_MAX_LINES:-1000}"
MAX_BYTES="${CTS_LOG_MAX_BYTES:-8388608}"
SCAN_ROOT="${CTS_LOG_SCAN_ROOT:-/}"
SKIP_JOURNAL="${CTS_LOG_SKIP_JOURNAL:-0}"
JOURNAL_MAX_USE="${CTS_JOURNAL_MAX_USE:-256M}"

[[ "$MAX_LINES" =~ ^[0-9]+$ ]] && (( MAX_LINES >= 100 && MAX_LINES <= 10000 )) \
  || { echo "CTS_LOG_MAX_LINES must be 100..10000" >&2; exit 2; }
[[ "$MAX_BYTES" =~ ^[0-9]+$ ]] && (( MAX_BYTES >= 1048576 && MAX_BYTES <= 67108864 )) \
  || { echo "CTS_LOG_MAX_BYTES must be 1..64 MiB" >&2; exit 2; }
[[ "$SCAN_ROOT" == /* && "$SCAN_ROOT" != *".."* ]] \
  || { echo "CTS_LOG_SCAN_ROOT must be an absolute path without '..'" >&2; exit 2; }

if command -v flock >/dev/null 2>&1; then
  LOCK_FILE="${CTS_LOG_LOCK_FILE:-/run/lock/cts-log-retention.lock}"
  if [[ "$SCAN_ROOT" != "/" ]]; then LOCK_FILE="${SCAN_ROOT%/}/cts-log-retention.lock"; fi
  mkdir -p "$(dirname "$LOCK_FILE")"
  exec 9>"$LOCK_FILE"
  flock -n 9 || exit 0
fi

rooted() {
  local path="$1"
  if [[ "$SCAN_ROOT" == "/" ]]; then printf '%s' "$path"; else printf '%s%s' "${SCAN_ROOT%/}" "$path"; fi
}

trim_log() {
  local file="$1" line_count byte_count temp byte_temp
  [[ -f "$file" && ! -L "$file" ]] || return 0
  line_count="$(wc -l < "$file" 2>/dev/null || printf '0')"
  byte_count="$(stat -c %s "$file" 2>/dev/null || printf '0')"
  [[ "$line_count" =~ ^[0-9]+$ && "$byte_count" =~ ^[0-9]+$ ]] || return 0
  if (( line_count <= MAX_LINES && byte_count <= MAX_BYTES )); then return 0; fi

  temp="$(mktemp "$(dirname "$file")/.cts-log-trim.XXXXXX")"
  tail -n "$MAX_LINES" -- "$file" > "$temp"
  if (( $(stat -c %s "$temp") > MAX_BYTES )); then
    byte_temp="${temp}.bytes"
    tail -c "$MAX_BYTES" -- "$temp" > "$byte_temp"
    mv -f -- "$byte_temp" "$temp"
  fi
  # Keep the inode so a long-running process cannot continue filling an
  # unlinked old file after retention runs.
  cp -- "$temp" "$file"
  rm -f -- "$temp"
  printf '[cts-log-retention] trimmed %s from %s lines/%s bytes\n' "$file" "$line_count" "$byte_count"
}

scan_logs() {
  local root="$1"
  [[ -d "$root" ]] || return 0
  while IFS= read -r -d '' file; do trim_log "$file"; done < <(
    find "$root" -xdev -type f \
      \( -name '*.log' -o -name '*.out' -o -name '*.err' -o -name 'errors-*.jsonl' \) \
      -print0 2>/dev/null
  )
}

# /var/log contains diagnostics only for the selected narrow extensions.
scan_logs "$(rooted /var/log)"

# Canonical CTS runtime logs. Never descend through data, Redis, credentials,
# reports or backups. The data/logs suffix is a restricted-container fallback.
shopt -s nullglob
for log_dir in \
  "$(rooted /var/lib/cts/instances)"/*/logs \
  "$(rooted /var/lib/cts/instances)"/*/data/logs \
  "$(rooted /var/lib)"/cts-*/logs \
  "$(rooted /opt)"/cts-*/logs \
  "$(rooted /opt)"/cts-*/.agent-logs \
  "$(rooted /opt)"/*-pulse/logs; do
  scan_logs "$log_dir"
done

# Legacy Pulse wrote diagnostics directly at its replaceable code root.
for project_root in "$(rooted /opt)"/grok-*-pulse "$(rooted /opt)"/cts-*-pulse; do
  [[ -d "$project_root" ]] || continue
  while IFS= read -r -d '' file; do trim_log "$file"; done < <(
    find "$project_root" -maxdepth 1 -type f \
      \( -name '*.log' -o -name '*.out' -o -name '*.err' -o -name 'errors-*.jsonl' \) \
      -print0 2>/dev/null
  )
done
shopt -u nullglob

if [[ "$SCAN_ROOT" == "/" && "$SKIP_JOURNAL" != "1" ]] && command -v journalctl >/dev/null 2>&1; then
  journalctl --vacuum-time=7d --vacuum-size="$JOURNAL_MAX_USE" >/dev/null
fi
