#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -d .venv/bin ]; then
  PATH="$PWD/.venv/bin:$PATH"
  export PATH
fi
if [ -n "${PULSE_STATE_DIR:-}" ] && ! curl -sf -o /dev/null --max-time 2 http://127.0.0.1:3015/stats.json; then
  python3 -u scripts/local_sidecar.py --state "$PULSE_STATE_DIR" >>/tmp/cts-g-local-sidecar.log 2>&1 &
fi
if curl -sf -o /dev/null --max-time 2 http://127.0.0.1:8080/; then
  exit 0
fi
npm run dev -- --host "${CTSG_DEV_HOST:-127.0.0.1}" >>/tmp/cts-g-app-startup.log 2>&1 &
