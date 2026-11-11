#!/usr/bin/env bash
# Prepare (and, only with START=1, start) a Bybit V5 linear connection on this machine.
#   bybit-x01  live mainnet     https://api.bybit.com        real money
#   bybit-x02  demo trading     https://api-demo.bybit.com   virtual funds (key made in Bybit "Demo Trading")
# Usage: BYBIT_API_KEY=... BYBIT_API_SECRET=... deploy/bybit-up.sh x01|x02        (writes the private credential file only)
#        START=1 BYBIT_API_KEY=... BYBIT_API_SECRET=... deploy/bybit-up.sh x02    (also starts the engine)
# The host is pinned by server/pulse/conn_guard.py (a stored or environment base URL that contradicts it refuses to start).
# Key permissions: Unified Trading, Contract/Derivatives: Orders and Positions (read + trade), Wallet (read). No withdrawal.
# Bind the key to this machine's IP. The credentials are read from the environment and never printed.
set -euo pipefail

SLOT="${1:-}"
case "$SLOT" in x01|x02) ;; *) echo "usage: $0 x01|x02" >&2; exit 2 ;; esac
CONN="bybit-$SLOT"
REPO="${REPO:-/home/user/CTS-G}"
PULSE_DIR="${PULSE_DIR:-/opt/grok-x01-pulse}"
LOG_DIR="${LOG_DIR:-/var/log/cts-g}"

: "${BYBIT_API_KEY:?BYBIT_API_KEY is not set in this environment}"
: "${BYBIT_API_SECRET:?BYBIT_API_SECRET is not set in this environment}"

mkdir -p "$PULSE_DIR" "$LOG_DIR"
# 1. Runtime tree from the repo (the shipped bybit overlays start with the drawdown halt on and minimum-size orders)
cp -a "$REPO/server/pulse/." "$PULSE_DIR/"
find "$PULSE_DIR" -name __pycache__ -type d -prune -exec rm -rf {} +
[ -f "$PULSE_DIR/overlay-$CONN.json" ] || cp "$REPO/server/pulse/overlay-$CONN.json" "$PULSE_DIR/overlay-$CONN.json"

# 2. Private credential file (mode 600; the engine ignores a file others can read)
CRED="$PULSE_DIR/cred-$CONN.json"
( umask 077
  python3 - "$CRED" "$SLOT" <<'PY'
import json, os, sys
path, slot = sys.argv[1], sys.argv[2]
json.dump({"api_key": os.environ["BYBIT_API_KEY"], "api_secret": os.environ["BYBIT_API_SECRET"],
           "is_testnet": "1" if slot == "x02" else "0"}, open(path, "w"))
PY
)
chmod 600 "$CRED"
echo "wrote $CRED (fields: api_key api_secret is_testnet; mode $(stat -c %a "$CRED"))"

# 3. Engine dependency: the price stream needs websocket-client
python3 -c "import websocket" 2>/dev/null || python3 -m pip install --quiet --break-system-packages websocket-client

if [ "${START:-0}" != "1" ]; then
  echo "prepared $CONN. Start it with:  START=1 $0 $SLOT   (or: cd $PULSE_DIR && PULSE_CONN=$CONN python3 -O pulse_trader.py)"
  echo "restart pulse_http.py afterwards so the desk lists the Bybit lane"
  exit 0
fi

cd "$PULSE_DIR"
if ! ps -eo cmd | grep -q "^python3 -O pulse_trader.py.*$CONN"; then
  PULSE_CONN="$CONN" PYTHONUNBUFFERED=1 nohup python3 -O pulse_trader.py >>"$LOG_DIR/$CONN.log" 2>&1 &
  echo "started pulse_trader $CONN pid $!"
fi
echo "engine log: $LOG_DIR/$CONN.log"
