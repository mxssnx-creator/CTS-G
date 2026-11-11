#!/usr/bin/env bash
# systemd ExecStart for the CTS-G desk (Vite on 0.0.0.0:3102).
set -euo pipefail
ROOT="${CTS_G_ROOT:-/opt/cts-g}"
LOG_DIR="${LOG_DIR:-/var/log/cts-g}"
cd "$ROOT"

export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-3102}"
export PULSE_URL="${PULSE_URL:-http://127.0.0.1:3015}"
export CTS_URL="${CTS_URL:-http://127.0.0.1}"
export PATH="${ROOT}/node_modules/.bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

mkdir -p "$LOG_DIR"
if [[ -f "$ROOT/scripts/sync-live-stats.mjs" ]]; then
  nohup node "$ROOT/scripts/sync-live-stats.mjs" >>"$LOG_DIR/sync-live-stats.log" 2>&1 &
fi

VITE="${ROOT}/node_modules/.bin/vite"
if [[ ! -e "$VITE" ]]; then
  echo "vite missing in $ROOT — run npm ci" >&2
  exit 127
fi
# Absolute vite path: systemd PATH does not include node_modules/.bin.
exec node scripts/with-app-env.mjs "$VITE" dev --host "$HOST" --port "$PORT"
