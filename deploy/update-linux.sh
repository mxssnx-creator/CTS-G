#!/usr/bin/env bash
# CTS-G Linux update: refresh code, keep Live/VST overlays + open positions, restart units.
#
#   sudo ./deploy/update-linux.sh
#   sudo ./deploy/update-linux.sh --force
#   sudo ./deploy/update-linux.sh --from-dir /path/to/CTS-G
#   sudo ./deploy/update-linux.sh --no-restart
#
# Never flattens exchange positions. Overlay/open/block/trade files in
# /opt/grok-x01-pulse are preserved.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=linux-common.sh
source "$HERE/linux-common.sh"

FROM_DIR=""
FORCE=0
NO_RESTART=0
START_LIVE=0

usage() {
  cat <<'EOF'
CTS-G Linux update

Usage: sudo ./deploy/update-linux.sh [options]

  --from-dir PATH   Update /opt/cts-g from this checkout instead of git pull
  --repo URL        Origin URL if retargeting the clone
  --branch NAME     Branch to fast-forward (default: main)
  --desk-port N     Desk listen port (default: 3102)
  --force           git reset --hard origin/BRANCH (discards local edits in /opt/cts-g)
  --no-restart      Sync files only; leave systemd units running
  --start-live      Ensure grok-pulse@bingx-x01 is started after restart
  -h, --help        Show this help

Preserved under /opt/grok-x01-pulse:
  overlay-*.json  open-*.json  block-state-*.json  trades-*.jsonl
  STOP / PAUSE  errors  lev-set  cts-settings  stats  logs
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from-dir) FROM_DIR="${2:-}"; shift 2 ;;
    --repo) REPO_URL="${2:-}"; shift 2 ;;
    --branch) BRANCH="${2:-}"; shift 2 ;;
    --desk-port) DESK_PORT="${2:-}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --no-restart) NO_RESTART=1; shift ;;
    --start-live) START_LIVE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

require_linux
require_root

[[ -d "$CTS_G_ROOT" ]] || die "$CTS_G_ROOT missing — run deploy/install-linux.sh first"
[[ -d "$PULSE_DIR" ]] || die "$PULSE_DIR missing — run deploy/install-linux.sh first"

log "CTS-G Linux update  root=$CTS_G_ROOT  pulse=$PULSE_DIR"

if [[ -n "$FROM_DIR" ]]; then
  [[ -f "$FROM_DIR/server/pulse/pulse_trader.py" ]] || die "not a CTS-G tree: $FROM_DIR"
  if [[ "$(cd "$FROM_DIR" && pwd)" != "$(cd "$CTS_G_ROOT" && pwd)" ]]; then
    sync_app_tree "$FROM_DIR"
  else
    log "from-dir is the install root — using files in place"
  fi
elif [[ -d "$CTS_G_ROOT/.git" ]]; then
  configure_git "$CTS_G_ROOT"
  git -C "$CTS_G_ROOT" fetch --prune "$GIT_REMOTE"
  git -C "$CTS_G_ROOT" checkout "$BRANCH"
  if [[ "$FORCE" -eq 1 ]]; then
    git -C "$CTS_G_ROOT" reset --hard "$GIT_REMOTE/$BRANCH"
    log "hard reset to $GIT_REMOTE/$BRANCH"
  else
    git -C "$CTS_G_ROOT" merge --ff-only "$GIT_REMOTE/$BRANCH" \
      || die "fast-forward failed; retry with --force if you want to discard local edits"
    log "fast-forwarded $BRANCH"
  fi
else
  die "no git clone at $CTS_G_ROOT and no --from-dir given"
fi

chmod 755 "$CTS_G_ROOT/deploy/"*.sh
configure_git "$CTS_G_ROOT"
seed_env
sync_pulse_tree
npm_install_desk
install_units

if [[ "$NO_RESTART" -eq 1 ]]; then
  log "files updated, units not restarted (--no-restart)"
else
  # Restart HTTP + desk + VST always. Live only if it was already active or asked.
  live_was_on=0
  if systemctl is-active --quiet "grok-pulse@${LIVE_SLOT}.service"; then
    live_was_on=1
  fi
  start_live="$START_LIVE"
  [[ "$live_was_on" -eq 1 ]] && start_live=1
  start_stack "$start_live"
  health_report
fi

log "update complete — open positions were not flattened"
