#!/usr/bin/env bash
# Bring up the BingX VST (bingx-x02) demo engine on this machine.
# Places DEMO orders on the VST account only (host pinned by server/pulse/conn_guard.py).
# Reads BINGX_API_KEY / BINGX_API_SECRET from the environment; never prints them.
set -euo pipefail

REPO="${REPO:-/home/user/CTS-G}"
PULSE_DIR="${PULSE_DIR:-/opt/grok-x01-pulse}"
STATE_DIR="${STATE_DIR:-/var/lib/cts-g}"
LOG_DIR="${LOG_DIR:-/var/log/cts-g}"
CONN="bingx-x02"

mkdir -p "$STATE_DIR/redis" "$LOG_DIR" "$PULSE_DIR"

: "${BINGX_API_KEY:?BINGX_API_KEY is not set in this environment}"
: "${BINGX_API_SECRET:?BINGX_API_SECRET is not set in this environment}"

# 1. Redis on loopback, dump directory kept out of the repo
if ! redis-cli -h 127.0.0.1 -p 6379 ping >/dev/null 2>&1; then
  redis-server --daemonize yes --bind 127.0.0.1 --port 6379 \
    --dir "$STATE_DIR/redis" --save "" --appendonly no --logfile "$LOG_DIR/redis.log"
  sleep 1
fi
redis-cli -h 127.0.0.1 -p 6379 ping

# 2. VST connection: pinned base URL, testnet flag set, credentials from the environment
redis-cli -h 127.0.0.1 -p 6379 HSET "connection:$CONN" \
  api_key "$BINGX_API_KEY" api_secret "$BINGX_API_SECRET" \
  base_url "https://open-api-vst.bingx.com" is_testnet 1 >/dev/null
echo "stored connection:$CONN fields: $(redis-cli -h 127.0.0.1 -p 6379 HKEYS "connection:$CONN" | tr '\n' ' ')"

# 3. Runtime tree from the repo (overlay has symbolCap 80 and PF windows 6)
cp -a "$REPO/server/pulse/." "$PULSE_DIR/"
find "$PULSE_DIR" -name __pycache__ -type d -prune -exec rm -rf {} +

# 4. Local HTTP token for the sidecar's mutating routes (generated once, mode 600)
TOKEN_FILE="$STATE_DIR/pulse-http.env"
if [ ! -f "$TOKEN_FILE" ]; then
  umask 077
  printf 'PULSE_HTTP_TOKEN=%s\n' "$(openssl rand -hex 24)" > "$TOKEN_FILE"
fi
set -a
# shellcheck disable=SC1090
. "$TOKEN_FILE"
set +a

# 5. Engine dependency: the WebSocket feed needs websocket-client (bingx_fast.py); without it the engine halts
python3 -c "import websocket" 2>/dev/null || python3 -m pip install --quiet --break-system-packages websocket-client

# 6. Start the engine and the HTTP sidecar on 127.0.0.1:3015
cd "$PULSE_DIR"
if ! pgrep -f "python3 -O pulse_trader.py" >/dev/null; then
  PULSE_CONN="$CONN" PYTHONUNBUFFERED=1 nohup python3 -O pulse_trader.py >>"$LOG_DIR/$CONN.log" 2>&1 &
  echo "started pulse_trader pid $!"
fi
if ! pgrep -f "python3 -O pulse_http.py" >/dev/null; then
  PULSE_HTTP_TOKEN="$PULSE_HTTP_TOKEN" PULSE_HALT_ON_INACTIVE=0 PYTHONUNBUFFERED=1 nohup python3 -O pulse_http.py >>"$LOG_DIR/pulse-http.log" 2>&1 &
  echo "started pulse_http pid $!"
fi

# 6. Wait for stats from the VST connection and summarise (no secrets printed)
STATS="$LOG_DIR/stats-vst.json"
for _ in $(seq 1 45); do
  if curl -sf "http://127.0.0.1:3015/stats.json?conn=vst" -o "$STATS"; then break; fi
  sleep 2
done
if [ ! -s "$STATS" ]; then
  echo "no stats yet; last engine log lines:"
  tail -n 40 "$LOG_DIR/$CONN.log" || true
  exit 1
fi
python3 - "$STATS" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))
print("connection:", s.get("connection"), "| mode:", s.get("mode"), "| running:", s.get("running"))
print("equity:", s.get("equity"), "| symbolCap:", s.get("symbolCap"))
print("trackPrefix:", (s.get("engine") or {}).get("trackPrefix"))
PY
echo "engine log: $LOG_DIR/$CONN.log"
