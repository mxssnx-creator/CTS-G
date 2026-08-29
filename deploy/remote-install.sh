#!/usr/bin/env bash
# Clone CTS-G on a remote Linux host and run install-linux.sh.
#
#   sudo ./deploy/remote-install.sh
#   ./deploy/remote-install.sh --host 152.53.114.112 --user root --identity ~/.ssh/id_ed25519
#
# Defaults: host 152.53.114.112, dest /opt/cts-g, desk :3102,
# git https://github.com/mxssnx-creator/CTS-G.git (user xssnet <mxssnx@gmail.com>).
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
CTS-G remote Linux install

Usage: ./deploy/remote-install.sh [options]

  --host HOST       SSH host (default: 152.53.114.112)
  --user USER       SSH user (default: root)
  --port N          SSH port (default: 22)
  --identity FILE   SSH private key
  --desk-port N     Desk listen port on the server (default: 3102)
  --branch NAME     Git branch (default: main)
  --repo URL        Git remote (default: https://github.com/mxssnx-creator/CTS-G.git)
  --start-live      Start grok-pulse@bingx-x01 after install
  -h, --help

The server must accept SSH. Git clone happens ON the server from GitHub.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) REMOTE_HOST="${2:-}"; shift 2 ;;
    --user) REMOTE_USER="${2:-}"; shift 2 ;;
    --port) SSH_PORT="${2:-}"; shift 2 ;;
    --identity) IDENTITY="${2:-}"; shift 2 ;;
    --desk-port) DESK_PORT="${2:-}"; shift 2 ;;
    --branch) BRANCH="${2:-}"; shift 2 ;;
    --repo) REPO_URL="${2:-}"; shift 2 ;;
    --start-live) START_LIVE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

ssh_cmd=(ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=12 -p "$SSH_PORT")
[[ -n "$IDENTITY" ]] && ssh_cmd+=(-i "$IDENTITY")
ssh_cmd+=("${REMOTE_USER}@${REMOTE_HOST}")

log "remote install ${REMOTE_USER}@${REMOTE_HOST}:${SSH_PORT} → $CTS_G_ROOT  desk :$DESK_PORT"

LIVE_FLAG=""
[[ "$START_LIVE" -eq 1 ]] && LIVE_FLAG="--start-live"

# Quote for remote bash -lc
remote=$(cat <<EOF
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
if command -v apt-get >/dev/null; then
  apt-get update -y
  apt-get install -y --no-install-recommends git ca-certificates curl
fi
if [[ -d $CTS_G_ROOT/.git ]]; then
  git -C $CTS_G_ROOT remote set-url origin '$REPO_URL' || git -C $CTS_G_ROOT remote add origin '$REPO_URL'
  git -C $CTS_G_ROOT fetch --prune origin
  git -C $CTS_G_ROOT checkout $BRANCH
  git -C $CTS_G_ROOT reset --hard origin/$BRANCH
else
  rm -rf $CTS_G_ROOT
  git clone --branch $BRANCH '$REPO_URL' $CTS_G_ROOT
fi
git -C $CTS_G_ROOT config user.name '$GIT_USER_NAME'
git -C $CTS_G_ROOT config user.email '$GIT_USER_EMAIL'
chmod 755 $CTS_G_ROOT/deploy/*.sh
$CTS_G_ROOT/deploy/install-linux.sh --from-dir $CTS_G_ROOT --desk-port $DESK_PORT $LIVE_FLAG
EOF
)

"${ssh_cmd[@]}" "bash -lc $(printf '%q' "$remote")"
log "remote install finished — desk http://${REMOTE_HOST}:${DESK_PORT}/"
