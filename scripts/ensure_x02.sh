#!/usr/bin/env bash
# Demo (VST) connection x02: restores its credentials into Redis from /var/lib/cts-g/cred-bingx-x02.json when missing and starts the engine when it is not running.
# Never touches x01. Idempotent.
set -u
REPO="${REPO:-/home/user/CTS-G}"; CRED=/var/lib/cts-g/cred-bingx-x02.json; DIR=/opt/grok-x01-pulse
redis-cli -h 127.0.0.1 -p 6379 ping >/dev/null 2>&1 || { bash "$REPO/scripts/ensure_x01.sh" >/dev/null 2>&1; }
if [ -r "$CRED" ] && [ -z "$(redis-cli -h 127.0.0.1 -p 6379 HGET connection:bingx-x02 api_key 2>/dev/null)" ]; then
python3 - "$CRED" <<'PY' 2>/dev/null
import json, subprocess, sys
c = json.load(open(sys.argv[1]))
subprocess.run(["redis-cli", "-h", "127.0.0.1", "-p", "6379", "HSET", "connection:bingx-x02", "api_key", c["api_key"], "api_secret", c["api_secret"],
                "base_url", c.get("base_url", "https://open-api-vst.bingx.com"), "is_testnet", "1"], check=True, capture_output=True)
PY
fi
for p in $(pgrep -f pulse_trader.py); do tr '\0' '\n' < /proc/$p/environ 2>/dev/null | grep -q "^PULSE_CONN=bingx-x02$" && { echo "x02 engine running"; exit 0; }; done
cd "$REPO" && PULSE_CONN=bingx-x02 PYTHONUNBUFFERED=1 nohup python3 -B server/pulse/pulse_trader.py >> "$DIR/x02_live.log" 2>&1 &
echo "x02 engine started (pid $!)"
