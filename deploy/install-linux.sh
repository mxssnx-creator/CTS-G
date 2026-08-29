#!/usr/bin/env bash
# CTS-G first-time Linux install: packages, trees, systemd, Redis, desk + engines.
#
#   sudo ./deploy/install-linux.sh
#   sudo ./deploy/install-linux.sh --from-dir /path/to/CTS-G
#   sudo ./deploy/install-linux.sh --clone --branch main
#   sudo ./deploy/install-linux.sh --start-live
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=linux-common.sh
source "$HERE/linux-common.sh"

FROM_DIR=""
DO_CLONE=0
START_LIVE=0
NO_START=0

usage() {
  cat <<'EOF'
CTS-G Linux install

Usage: sudo ./deploy/install-linux.sh [options]

  --from-dir PATH   Copy this checkout into /opt/cts-g (default: this repo)
  --clone           git clone REPO_URL into /opt/cts-g instead of copying
  --repo URL        Git remote (default: https://github.com/mxssnx-creator/CTS-G.git)
  --branch NAME     Branch to clone/update (default: main)
  --desk-port N     Desk listen port (default: 3102)
  --start-live      Also start grok-pulse@bingx-x01 (Live)
  --no-start        Install units but do not start services
  -h, --help        Show this help

Env: CTS_G_ROOT=/opt/cts-g  PULSE_DIR=/opt/grok-x01-pulse
     REPO_URL  BRANCH  DESK_PORT  GIT_USER_NAME  GIT_USER_EMAIL  REMOTE_HOST
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from-dir) FROM_DIR="${2:-}"; shift 2 ;;
    --clone) DO_CLONE=1; shift ;;
    --repo) REPO_URL="${2:-}"; shift 2 ;;
    --branch) BRANCH="${2:-}"; shift 2 ;;
    --desk-port) DESK_PORT="${2:-}"; shift 2 ;;
    --start-live) START_LIVE=1; shift ;;
    --no-start) NO_START=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

require_linux
require_root

log "CTS-G Linux install  root=$CTS_G_ROOT  pulse=$PULSE_DIR  branch=$BRANCH"

ensure_base_packages
ensure_node
ensure_dirs
ensure_redis

SRC=""
if [[ "$DO_CLONE" -eq 1 ]]; then
  if [[ -d "$CTS_G_ROOT/.git" ]]; then
    log "existing clone at $CTS_G_ROOT — fetch $BRANCH"
    git -C "$CTS_G_ROOT" remote set-url origin "$REPO_URL" 2>/dev/null || true
    git -C "$CTS_G_ROOT" fetch --prune origin
    git -C "$CTS_G_ROOT" checkout "$BRANCH"
    git -C "$CTS_G_ROOT" reset --hard "origin/$BRANCH"
  else
    if [[ -e "$CTS_G_ROOT" && -n "$(ls -A "$CTS_G_ROOT" 2>/dev/null || true)" ]]; then
      die "$CTS_G_ROOT is not empty; move it aside or use --from-dir"
    fi
    git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$CTS_G_ROOT"
  fi
else
  SRC="${FROM_DIR:-$(script_repo_root)}"
  [[ -f "$SRC/server/pulse/pulse_trader.py" ]] || die "not a CTS-G tree: $SRC"
  if [[ "$(cd "$SRC" && pwd)" != "$(cd "$CTS_G_ROOT" 2>/dev/null && pwd || true)" ]]; then
    sync_app_tree "$SRC"
  else
    log "already running from $CTS_G_ROOT — skip copy"
  fi
fi

[[ -f "$CTS_G_ROOT/deploy/linux-common.sh" ]] || die "install did not land deploy/ in $CTS_G_ROOT"
chmod 755 "$CTS_G_ROOT/deploy/"*.sh

configure_git "$CTS_G_ROOT"
seed_env
sync_pulse_tree
npm_install_desk
install_units
enable_stack

if [[ "$NO_START" -eq 1 ]]; then
  log "units enabled, not started (--no-start)"
else
  start_stack "$START_LIVE"
  health_report
fi

print_keys_help
log "install complete"
