#!/usr/bin/env sh
# Preview only. Never starts an exchange engine.
set -eu
if curl --max-time 2 --silent --fail http://127.0.0.1:8080/ >/dev/null 2>&1; then
  exit 0
fi
cd /workspace/CTS-G
mkdir -p /workspace/screenshots
export NODE_OPTIONS="--import=/workspace/CTS-G/scripts/workspace-network.mjs ${NODE_OPTIONS:-}"
if ! curl --max-time 2 --silent --fail http://127.0.0.1:8099/system.json >/dev/null 2>&1; then
  nohup python3 scripts/qa_set_overview.py --output /tmp/cts-qa-overview.json --serve 8099 > /tmp/cts-qa-fixture.log 2>&1 < /dev/null &
fi
nohup env PULSE_URL=http://127.0.0.1:8099 npm run dev > /tmp/cts-preview.log 2>&1 < /dev/null &
