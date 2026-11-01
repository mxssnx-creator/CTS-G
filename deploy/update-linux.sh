#!/usr/bin/env bash
# CTS-G Linux update — unattended. Keeps overlays and open positions.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=linux-common.sh
source "$HERE/linux-common.sh"
# shellcheck source=git-update.sh
source "$HERE/git-update.sh"

FROM_DIR=""
FORCE=0
NO_RESTART=0
START_LIVE=0
NO_VST=0
PORT_EXPLICIT=0

usage() {
  cat <<'EOF'
CTS-G Linux update (unattended)

Usage: sudo ./deploy/update-linux.sh [options]

  --name NAME       Install name (default: cts-g) → /opt/NAME
  --port N          Desk listen port (default: 3102)
  --host HOST       Public hostname/IP for result URLs
  --from-dir PATH   Update from this checkout instead of git pull
  --repo URL        Origin URL
  --branch NAME     Branch (default: main)
  --force           git reset --hard origin/BRANCH
  --no-restart      Sync files only
  --start-live      Ensure Live engine is started
  --no-vst          Disable x02 auto-start; stop x02 when restarting services
  --yes             No-op (update never prompts)
  -h, --help

Never flattens exchange positions or cancels x02 orders. Stopping x02 stops its
position manager: verify its protection first. With --no-restart, --no-vst
disables auto-start but leaves an already running x02 alone.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name|-n) apply_name "${2:-}"; shift 2 ;;
    --port|-p|--desk-port) DESK_PORT="${2:-}"; PORT_EXPLICIT=1; shift 2 ;;
    --host) PUBLIC_HOST="${2:-}"; REMOTE_HOST="${2:-}"; shift 2 ;;
    --from-dir) FROM_DIR="${2:-}"; shift 2 ;;
    --repo) REPO_URL="${2:-}"; shift 2 ;;
    --branch) BRANCH="${2:-}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --no-restart) NO_RESTART=1; shift ;;
    --start-live) START_LIVE=1; shift ;;
    --no-vst) NO_VST=1; shift ;;
    --yes|-y) shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

require_linux
require_root

[[ -d "$CTS_G_ROOT" ]] || die "$CTS_G_ROOT missing — run deploy/install-linux.sh first"
[[ -d "$PULSE_DIR" ]] || die "$PULSE_DIR missing — run deploy/install-linux.sh first"

log "update  name=$CTS_G_NAME  root=$CTS_G_ROOT  desk=:${DESK_PORT}"

ensure_base_packages
ensure_node

if [[ -n "$FROM_DIR" ]]; then
  [[ -f "$FROM_DIR/server/pulse/pulse_trader.py" ]] || die "not a CTS-G tree: $FROM_DIR"
  if [[ "$(cd "$FROM_DIR" && pwd)" != "$(cd "$CTS_G_ROOT" && pwd)" ]]; then
    sync_app_tree "$FROM_DIR"
  else
    skip "app tree (from-dir is install root)"
  fi
elif [[ -d "$CTS_G_ROOT/.git" ]]; then
  configure_git "$CTS_G_ROOT"
  git -C "$CTS_G_ROOT" fetch --prune "$GIT_REMOTE"
  cts_git_update "$CTS_G_ROOT" "$GIT_REMOTE" "$BRANCH" "$FORCE" \
    || die "Git update failed. Resolve local changes/divergence; without --force no hard reset is attempted."
  [[ "$FORCE" -eq 1 ]] && ok "hard reset $GIT_REMOTE/$BRANCH" || ok "fast-forward $BRANCH"
else
  die "no git clone at $CTS_G_ROOT and no --from-dir given"
fi

chmod 755 "$CTS_G_ROOT/deploy/"*.sh
configure_git "$CTS_G_ROOT"
seed_env
sync_pulse_tree
npm_install_desk
install_units
if [[ "$NO_VST" -eq 1 ]]; then
  disable_vst_autostart
fi

if [[ "$NO_RESTART" -eq 1 ]]; then
  skip "restart"
  print_results
else
  live_was_on=0
  if systemctl is-active --quiet "grok-pulse@${LIVE_SLOT}.service"; then
    live_was_on=1
  fi
  start_live="$START_LIVE"
  [[ "$live_was_on" -eq 1 ]] && start_live=1
  start_vst=preserve
  [[ "$NO_VST" -eq 1 ]] && start_vst=0
  start_stack "$start_live" "$start_vst"
  health_report
fi

log "update complete — open positions were not flattened"
if [[ ${#STEPS_FAIL[@]} -gt 0 ]]; then
  exit 1
fi
exit 0
