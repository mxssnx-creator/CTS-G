#!/usr/bin/env bash
# Idempotent: start the live x01 engine if, and only if, it is not running (owner standing rule: restart at once when it is down).
# It starts nothing else, and it never touches the drawdown halt, the start equity, EQ_MIN, the STOP / PAUSE / reset files or any setting; the engine
# restores its open positions from the venue itself. Safe to run repeatedly (a SessionStart hook runs it). ENSURE_X01_DRY=1 only reports.
set -u
REPO="${REPO:-/home/user/CTS-G}"
DIR="${PULSE_DIR:-/opt/grok-x01-pulse}"
running=0
for p in $(pgrep -f "[p]ulse_trader.py" 2>/dev/null); do
  tr '\0' '\n' < "/proc/$p/environ" 2>/dev/null | grep -q "^PULSE_CONN=bingx-x01$" && running=$((running + 1))
done
if [ "$running" -gt 0 ]; then
  echo "x01 engine running ($running process)"
  exit 0
fi
if [ "${ENSURE_X01_DRY:-0}" = "1" ]; then
  echo "x01 engine missing (dry run, nothing started)"
  exit 0
fi
cd "$REPO" || exit 1
python3 -c "import websocket" 2>/dev/null || python3 -m pip install --quiet --break-system-packages websocket-client
PULSE_CONN=bingx-x01 PYTHONUNBUFFERED=1 nohup python3 -B server/pulse/pulse_trader.py >> "$DIR/x01_live.log" 2>&1 &
echo "x01 engine started (pid $!)"
