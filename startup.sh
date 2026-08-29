#!/bin/sh
set -eu
cd /workspace
node scripts/preview.mjs stop || true
if ! curl -sf -o /dev/null --max-time 1 http://127.0.0.1:3015/stats.json 2>/dev/null; then
  :
fi
if ! grep -q sync-live-stats /proc/*/comm 2>/dev/null; then
  node scripts/sync-live-stats.mjs >>/tmp/sync-live-stats.log 2>&1 &
fi
if curl -sf -o /dev/null --max-time 2 http://127.0.0.1:8080/; then
  exit 0
fi
npm run dev >>/tmp/app-startup.log 2>&1 &
