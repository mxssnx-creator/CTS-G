#!/bin/sh
set -eu
cd /workspace
export PULSE_URL="${PULSE_URL:-http://152.53.114.112:3102}"
node scripts/preview.mjs stop || true
if ! grep -q sync-live-stats /proc/*/comm 2>/dev/null; then
  PULSE_URL="$PULSE_URL" node scripts/sync-live-stats.mjs >>/tmp/sync-live-stats.log 2>&1 &
fi
if curl -sf -o /dev/null --max-time 2 http://127.0.0.1:8080/; then
  exit 0
fi
npm run dev >>/tmp/app-startup.log 2>&1 &