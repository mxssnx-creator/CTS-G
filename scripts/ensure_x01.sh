#!/usr/bin/env bash
# Idempotent: start the live x01 engine if, and only if, it is not running (owner standing rule: restart at once when it is down).
# It starts nothing else, and it never touches the drawdown halt, the start equity, EQ_MIN, the STOP / PAUSE / reset files or any setting; the engine
# restores its open positions from the venue itself. Safe to run repeatedly (a SessionStart hook runs it). ENSURE_X01_DRY=1 only reports.
set -u
REPO="${REPO:-/home/user/CTS-G}"
DIR="${PULSE_DIR:-/opt/grok-x01-pulse}"
# Owner stop (2026-11-14, "Stop x1"): while /var/lib/cts-g/x01-stopped-by-owner exists nothing starts x01 (delete the file to resume the standing restart rule).
if [ -e "${X01_OWNER_OFF:-/var/lib/cts-g/x01-stopped-by-owner}" ]; then echo "x01 stopped by the owner (marker /var/lib/cts-g/x01-stopped-by-owner); not started"; exit 0; fi
running=0
for p in $(pgrep -f "[p]ulse_trader.py" 2>/dev/null); do
  tr '\0' '\n' < "/proc/$p/environ" 2>/dev/null | grep -q "^PULSE_CONN=bingx-x01$" && running=$((running + 1))
done
# The watchdog loop (scripts/x01_watchdog.sh) restarts the engine between sessions; this script makes sure exactly one runs (not in a dry run, not from the loop itself).
watchdog() {
  [ "${ENSURE_X01_DRY:-0}" = "1" ] && return 0
  [ "${ENSURE_X01_NO_WATCHDOG:-0}" = "1" ] && return 0
  w="$(cat "$DIR/x01-watchdog.pid" 2>/dev/null || true)"
  if [ -n "$w" ] && tr '\0' ' ' < "/proc/$w/cmdline" 2>/dev/null | grep -q "x01_watchdog.sh"; then
    echo "x01 watchdog running (pid $w)"
  else
    nohup bash "$REPO/scripts/x01_watchdog.sh" >> "$DIR/x01_watchdog.log" 2>&1 &
    echo "x01 watchdog started"
  fi
}
if [ "$running" -gt 0 ]; then
  echo "x01 engine running ($running process)"
  watchdog
  exit 0
fi
if [ "${ENSURE_X01_DRY:-0}" = "1" ]; then
  echo "x01 engine missing (dry run, nothing started)"
  exit 0
fi
cd "$REPO" || exit 1
# After a container restart Redis is empty and not running: the engine would start and halt with "missing bingx-x01 credentials". Bring Redis back and restore the
# x01 key from its mode-600 file (written once outside the repo; never printed). Both steps are skipped when they are already in place.
CRED_FILE="${CRED_FILE:-/var/lib/cts-g/cred-bingx-x01.json}"
if command -v redis-cli >/dev/null 2>&1; then
  if ! redis-cli -h 127.0.0.1 -p 6379 ping >/dev/null 2>&1 && command -v redis-server >/dev/null 2>&1; then
    mkdir -p /var/lib/cts-g/redis /var/log/cts-g
    redis-server --daemonize yes --bind 127.0.0.1 --port 6379 --dir /var/lib/cts-g/redis --save "" --appendonly no --logfile /var/log/cts-g/redis.log >/dev/null 2>&1
    sleep 1
  fi
  if [ -r "$CRED_FILE" ] && [ -z "$(redis-cli -h 127.0.0.1 -p 6379 HGET connection:bingx-x01 api_key 2>/dev/null)" ]; then
    python3 - "$CRED_FILE" <<'PY' 2>/dev/null
import json, subprocess, sys
c = json.load(open(sys.argv[1]))
subprocess.run(["redis-cli", "-h", "127.0.0.1", "-p", "6379", "HSET", "connection:bingx-x01", "api_key", c["api_key"], "api_secret", c["api_secret"],
                "base_url", c.get("base_url", "https://open-api.bingx.com"), "is_testnet", str(c.get("is_testnet", "0"))], check=True, capture_output=True)
PY
  fi
fi
python3 -c "import websocket" 2>/dev/null || python3 -m pip install --quiet --break-system-packages websocket-client
PULSE_CONN=bingx-x01 PYTHONUNBUFFERED=1 nohup python3 -B server/pulse/pulse_trader.py >> "$DIR/x01_live.log" 2>&1 &
echo "x01 engine started (pid $!)"
watchdog
