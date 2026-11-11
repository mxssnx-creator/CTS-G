#!/usr/bin/env bash
# Start (or restart what is missing of) the live stack: sidecar, both engines, the unattended monitor. Safe to run repeatedly: a
# process that already runs is left alone. A fresh container has no websocket-client (the engine halts without it), so it is
# installed first. Credentials are read by the engine from its cred files; nothing is printed.
set -u
REPO="${REPO:-/home/user/CTS-G}"
DIR=/opt/grok-x01-pulse
cd "$REPO/server/pulse" || exit 1
python3 -c "import websocket" 2>/dev/null || python3 -m pip install --quiet --break-system-packages websocket-client
running() { ps -eo cmd | grep -v grep | grep -q "$1"; }
running "python3 -B pulse_http.py" || (PYTHONUNBUFFERED=1 nohup python3 -B pulse_http.py >> "$DIR/http.log" 2>&1 &)
for conn in x02 x01; do
  n=0
  for p in $(pgrep -f "^python3 -B pulse_trader.py"); do
    tr '\0' '\n' < "/proc/$p/environ" 2>/dev/null | grep -q "^PULSE_CONN=bingx-$conn$" && n=$((n + 1))
  done
  [ "$n" -gt 0 ] || (PULSE_CONN="bingx-$conn" PYTHONUNBUFFERED=1 nohup python3 -B pulse_trader.py >> "$DIR/pulse-bingx-$conn.log" 2>&1 &)
done
cd "$REPO" && { running "scripts/monitor_engines.py" || (nohup python3 -B scripts/monitor_engines.py --interval 300 > /dev/null 2>&1 &); }
sleep 3
ps -eo pid,cmd | grep -E "pulse_|monitor_engines" | grep -v grep | cut -c1-80
