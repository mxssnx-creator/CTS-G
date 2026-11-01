#!/usr/bin/env bash
# Unattended remote install. --port is the desk port; --ssh-port is SSH.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=linux-common.sh
source "$HERE/linux-common.sh"
# shellcheck source=git-update.sh
source "$HERE/git-update.sh"

REMOTE_USER="${REMOTE_USER:-root}"
SSH_PORT="${SSH_PORT:-22}"
IDENTITY="${IDENTITY:-}"
START_LIVE=0
NO_VST=0

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
  --no-vst          Disable/stop x02; verify its position protection first
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
    --no-vst) NO_VST=1; shift ;;
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
VST_FLAG=""
[[ "$NO_VST" -eq 1 ]] && VST_FLAG="--no-vst"

# Quote every local value embedded in the remote script. The updater definition is
# embedded as code from this checkout; the remote checkout might be an older version.
printf -v CTS_REMOTE_ROOT_Q '%q' "$CTS_G_ROOT"
printf -v CTS_REMOTE_PARENT_Q '%q' "$(dirname "$CTS_G_ROOT")"
printf -v CTS_REMOTE_REPO_Q '%q' "$REPO_URL"
printf -v CTS_REMOTE_BRANCH_Q '%q' "$BRANCH"
printf -v CTS_REMOTE_NAME_Q '%q' "$CTS_G_NAME"
printf -v CTS_REMOTE_PORT_Q '%q' "$DESK_PORT"
printf -v CTS_REMOTE_HOST_Q '%q' "$REMOTE_HOST"

remote=$(cat <<EOF
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a GIT_TERMINAL_PROMPT=0
$(declare -f cts_git_update)
if command -v git >/dev/null && command -v curl >/dev/null; then
  true
elif command -v apt-get >/dev/null; then
  apt-get update -y -qq
  apt-get install -y -qq --no-install-recommends git ca-certificates curl
fi
if [[ -d $CTS_REMOTE_ROOT_Q/.git ]]; then
  git -C $CTS_REMOTE_ROOT_Q remote set-url origin $CTS_REMOTE_REPO_Q || git -C $CTS_REMOTE_ROOT_Q remote add origin $CTS_REMOTE_REPO_Q
  git -C $CTS_REMOTE_ROOT_Q fetch --prune origin
  cts_git_update $CTS_REMOTE_ROOT_Q origin $CTS_REMOTE_BRANCH_Q 0 || { echo 'Git update failed; local work was not reset.' >&2; exit 1; }
elif [[ -f $CTS_REMOTE_ROOT_Q/server/pulse/pulse_trader.py ]]; then
  true
else
  mkdir -p $CTS_REMOTE_PARENT_Q
  git clone --branch $CTS_REMOTE_BRANCH_Q --depth 1 $CTS_REMOTE_REPO_Q $CTS_REMOTE_ROOT_Q
fi
chmod 755 $CTS_REMOTE_ROOT_Q/deploy/*.sh
$CTS_REMOTE_ROOT_Q/deploy/install-linux.sh --yes --name $CTS_REMOTE_NAME_Q --port $CTS_REMOTE_PORT_Q --host $CTS_REMOTE_HOST_Q --from-dir $CTS_REMOTE_ROOT_Q $LIVE_FLAG $VST_FLAG
EOF
)

"${ssh_cmd[@]}" "bash -lc $(printf '%q' "$remote")"
echo
log "remote install finished"
echo "  Desk UI    http://${REMOTE_HOST}:${DESK_PORT}/"
echo "  Pulse      http://${REMOTE_HOST}:${PULSE_PORT}/stats.json?conn=overall"
echo "  GitHub     $REPO_URL"
