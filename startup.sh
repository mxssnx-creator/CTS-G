#!/bin/sh
set -eu
cd /workspace
export CTS_DATA_DIR="${CTS_DATA_DIR:-/workspace/.cts-local}"
export PULSE_HOST="${PULSE_HOST:-127.0.0.1}"
export PULSE_PORT="${PULSE_PORT:-3015}"
mkdir -p "$CTS_DATA_DIR"
if [ ! -f "$CTS_DATA_DIR/overlay-bingx-x02.json" ]; then
  cp server/pulse/overlay-bingx-x01.json server/pulse/overlay-bingx-x02.json "$CTS_DATA_DIR/"
fi
# Prefer the live desk when it answers, so the preview shows the running books.
if [ -z "${PULSE_URL:-}" ] && curl -sf -o /dev/null --max-time 3 "http://152.53.114.112:3102/connections.json"; then
  PULSE_URL="http://152.53.114.112:3102"
fi
export PULSE_URL="${PULSE_URL:-http://127.0.0.1:3015}"
if [ "$PULSE_URL" = "http://127.0.0.1:3015" ] || [ "$PULSE_URL" = "http://127.0.0.1:${PULSE_PORT}" ]; then
  if ! curl -sf -o /dev/null --max-time 2 "http://127.0.0.1:${PULSE_PORT}/connections.json"; then
    CTS_DATA_DIR="$CTS_DATA_DIR" PULSE_HOST="$PULSE_HOST" PULSE_PORT="$PULSE_PORT" \
      python3 /workspace/server/pulse/pulse_http.py >>/tmp/pulse-http.log 2>&1 &
  fi
fi
# GET snapshot sync follows the desk the preview is reading.
node scripts/preview.mjs stop || true
if ! grep -q sync-live-stats /proc/*/comm 2>/dev/null; then
  PULSE_URL="$PULSE_URL" node scripts/sync-live-stats.mjs >>/tmp/sync-live-stats.log 2>&1 &
fi
if curl -sf -o /dev/null --max-time 2 http://127.0.0.1:8080/; then
  exit 0
fi
PULSE_URL="$PULSE_URL" npm run dev >>/tmp/app-startup.log 2>&1 &
