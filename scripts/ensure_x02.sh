#!/usr/bin/env bash
# Demo (VST) connection x02: restores its credentials into Redis from /var/lib/cts-g/cred-bingx-x02.json when missing and starts the engine when it is not running.
# Never touches x01. Idempotent.
set -u
REPO="${REPO:-/home/user/CTS-G}"; CRED=/var/lib/cts-g/cred-bingx-x02.json; DIR=/opt/grok-x01-pulse
if ! redis-cli -h 127.0.0.1 -p 6379 ping >/dev/null 2>&1; then
  mkdir -p /var/lib/cts-g/redis /var/log/cts-g
  redis-server --daemonize yes --bind 127.0.0.1 --port 6379 --dir /var/lib/cts-g/redis --save "" --appendonly no --logfile /var/log/cts-g/redis.log >/dev/null 2>&1
  sleep 1
fi
python3 -c "import websocket, httpx, orjson" 2>/dev/null || python3 -m pip install --quiet --break-system-packages -r "$REPO/requirements.txt"
if [ -r "$CRED" ] && [ -z "$(redis-cli -h 127.0.0.1 -p 6379 HGET connection:bingx-x02 api_key 2>/dev/null)" ]; then
python3 - "$CRED" <<'PY' 2>/dev/null
import json, subprocess, sys
c = json.load(open(sys.argv[1]))
subprocess.run(["redis-cli", "-h", "127.0.0.1", "-p", "6379", "HSET", "connection:bingx-x02", "api_key", c["api_key"], "api_secret", c["api_secret"],
                "base_url", c.get("base_url", "https://open-api-vst.bingx.com"), "is_testnet", "1"], check=True, capture_output=True)
PY
fi
# Owner stop (2026-11-15, "stop x02"): while /var/lib/cts-g/x02-stopped-by-owner exists nothing starts x02 (its open positions stay on the venue with their stops).
if [ -e "${X02_OWNER_OFF:-/var/lib/cts-g/x02-stopped-by-owner}" ]; then echo "x02 stopped by the owner; not started"; exit 0; fi
# The engine reads $DIR/overlay-bingx-x02.json and only seeds it from $DIR/overlay.json, which a fresh host does not have: started without it the first
# history pass runs the engine defaults (the whole ranked universe, the minimum volume factor) and a reload a minute later cannot shrink a pass already
# in flight. Seed the repo's overlay when the host has none (an existing overlay holds the live settings and is never overwritten).
mkdir -p "$DIR"
[ -e "$DIR/overlay-bingx-x02.json" ] || cp "$REPO/server/pulse/overlay-bingx-x02.json" "$DIR/overlay-bingx-x02.json"
for p in $(pgrep -f pulse_trader.py); do tr '\0' '\n' < /proc/$p/environ 2>/dev/null | grep -q "^PULSE_CONN=bingx-x02$" && { echo "x02 engine running"; exit 0; }; done
cd "$REPO" && PULSE_CONN=bingx-x02 PYTHONUNBUFFERED=1 nohup python3 -B server/pulse/pulse_trader.py >> "$DIR/x02_live.log" 2>&1 &
echo "x02 engine started (pid $!)"
