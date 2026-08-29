#!/usr/bin/env bash
# Unattended remote install. --port is the desk port; --ssh-port is SSH.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=linux-common.sh
source "$HERE/linux-common.sh"

REMOTE_USER="${REMOTE_USER:-root}"
SSH_PORT="${SSH_PORT:-22}"
IDENTITY="${IDENTITY:-}"
START_LIVE=0

usage() {
  cat <<'EOF'
CTS-G remote Linux install (unattended)

Usage: ./deploy/remote-install.sh [options]

  --name NAME       Install name on the server (default: cts-g)
  --port N          Desk listen port (default: 3102)
  --host HOST       SSH host (default: 152.53.114.112)
  --user USER       SSH user (default: root)
  --ssh-port N      SSH port (default: 22)
  --identity FILE   SSH private key
  --branch NAME     Git branch (default: main)
  --repo URL        Git remote
  --start-live      Start Live engine
  --yes             No-op (never prompts)
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name|-n) apply_name "${2:-}"; shift 2 ;;
    --port|-p|--desk-port) DESK_PORT="${2:-}"; shift 2 ;;
    --host) REMOTE_HOST="${2:-}"; PUBLIC_HOST="${2:-}"; shift 2 ;;
    --user) REMOTE_USER="${2:-}"; shift 2 ;;
    --ssh-port) SSH_PORT="${2:-}"; shift 2 ;;
    --identity) IDENTITY="${2:-}"; shift 2 ;;
    --branch) BRANCH="${2:-}"; shift 2 ;;
    --repo) REPO_URL="${2:-}"; shift 2 ;;
    --start-live) START_LIVE=1; shift ;;
    --yes|-y) shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

ssh_cmd=(ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=12 -p "$SSH_PORT")
[[ -n "$IDENTITY" ]] && ssh_cmd+=(-i "$IDENTITY")
ssh_cmd+=("${REMOTE_USER}@${REMOTE_HOST}")

log "remote ${REMOTE_USER}@${REMOTE_HOST}:${SSH_PORT} → $CTS_G_ROOT  desk :$DESK_PORT  name=$CTS_G_NAME"

LIVE_FLAG=""
[[ "$START_LIVE" -eq 1 ]] && LIVE_FLAG="--start-live"

remote=$(cat <<EOF
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a GIT_TERMINAL_PROMPT=0
if command -v git >/dev/null && command -v curl >/dev/null; then
  true
elif command -v apt-get >/dev/null; then
  apt-get update -y -qq
  apt-get install -y -qq --no-install-recommends git ca-certificates curl
fi
if [[ -d $CTS_G_ROOT/.git ]]; then
  git -C $CTS_G_ROOT remote set-url origin '$REPO_URL' || git -C $CTS_G_ROOT remote add origin '$REPO_URL'
  git -C $CTS_G_ROOT fetch --prune origin
  git -C $CTS_G_ROOT checkout -f $BRANCH || git -C $CTS_G_ROOT checkout -B $BRANCH
  git -C $CTS_G_ROOT reset --hard origin/$BRANCH
elif [[ -f $CTS_G_ROOT/server/pulse/pulse_trader.py ]]; then
  true
else
  mkdir -p $(dirname $CTS_G_ROOT)
  git clone --branch $BRANCH --depth 1 '$REPO_URL' $CTS_G_ROOT
fi
chmod 755 $CTS_G_ROOT/deploy/*.sh
$CTS_G_ROOT/deploy/install-linux.sh --yes --name $CTS_G_NAME --port $DESK_PORT --host $REMOTE_HOST --from-dir $CTS_G_ROOT $LIVE_FLAG
EOF
)

"${ssh_cmd[@]}" "bash -lc $(printf '%q' "$remote")"
echo
log "remote install finished"
echo "  Desk UI    http://${REMOTE_HOST}:${DESK_PORT}/"
echo "  Pulse      http://${REMOTE_HOST}:${PULSE_PORT}/stats.json?conn=overall"
echo "  GitHub     $REPO_URL"
