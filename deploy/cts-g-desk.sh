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

mkdir -p "$LOG_DIR"
if [[ -f "$ROOT/scripts/sync-live-stats.mjs" ]]; then
  nohup node "$ROOT/scripts/sync-live-stats.mjs" >>"$LOG_DIR/sync-live-stats.log" 2>&1 &
fi

# Do not use `npm run dev` — package.json pins :8080 for the Grok preview.
exec node scripts/with-app-env.mjs vite dev --host "$HOST" --port "$PORT"
