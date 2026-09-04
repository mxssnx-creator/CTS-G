#!/usr/bin/env bash
# CTS-G Linux update — unattended. Keeps overlays and open positions.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=linux-common.sh
source "$HERE/linux-common.sh"

FROM_DIR=""
FORCE=0
NO_RESTART=0
START_LIVE=0
PORT_EXPLICIT=0
PULSE_PORT_EXPLICIT=0
REDIS_DB_EXPLICIT=0
STATE_EXPLICIT=0

usage() {
  cat <<'EOF'
CTS-G Linux update (unattended)

Usage: sudo ./deploy/update-linux.sh [options]

  --name NAME       Install name (default: cts-g) → /opt/NAME
  --port N          Desk listen port (default: 3102)
  --pulse-port N    Pulse HTTP port
  --redis-db N      Independent Redis logical DB 0..15
  --state-dir PATH  Durable state root
  --host HOST       Public hostname/IP for result URLs
  --from-dir PATH   Update from this checkout instead of git pull
  --repo URL        Origin URL
  --branch NAME     Branch (default: main)
  --force           git reset --hard origin/BRANCH
  --no-restart      Sync files only
  --start-live      Ensure Live engine is started
  --yes             No-op (update never prompts)
  -h, --help

Never flattens exchange positions.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name|-n) apply_name "${2:-}"; shift 2 ;;
    --port|-p|--desk-port) DESK_PORT="${2:-}"; PORT_EXPLICIT=1; shift 2 ;;
    --pulse-port) PULSE_PORT="${2:-}"; PULSE_PORT_EXPLICIT=1; shift 2 ;;
    --redis-db) REDIS_DB="${2:-}"; REDIS_DB_EXPLICIT=1; shift 2 ;;
    --state-dir) STATE_DIR="${2:-}"; STATE_EXPLICIT=1; shift 2 ;;
    --host) PUBLIC_HOST="${2:-}"; REMOTE_HOST="${2:-}"; shift 2 ;;
    --from-dir) FROM_DIR="${2:-}"; shift 2 ;;
    --repo) REPO_URL="${2:-}"; shift 2 ;;
    --branch) BRANCH="${2:-}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --no-restart) NO_RESTART=1; shift ;;
    --start-live) START_LIVE=1; shift ;;
    --yes|-y) shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

require_linux
require_root

load_existing_env_config
validate_instance_config
lock_and_check_instance

[[ -d "$CTS_G_ROOT" ]] || die "$CTS_G_ROOT missing — run deploy/install-linux.sh first"

log "update  name=$CTS_G_NAME  root=$CTS_G_ROOT  desk=:${DESK_PORT}"

ensure_base_packages
ensure_node
ensure_dirs
ensure_redis
quiesce_instance
create_verified_backup
migrate_legacy_redis_state

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
  if [[ "$FORCE" -eq 1 ]]; then
    git -C "$CTS_G_ROOT" checkout "$BRANCH"
    git -C "$CTS_G_ROOT" reset --hard "$GIT_REMOTE/$BRANCH"
    ok "hard reset $GIT_REMOTE/$BRANCH"
  else
    [[ -z "$(git -C "$CTS_G_ROOT" status --porcelain --untracked-files=no)" ]] || die "local code changes preserved; resolve before updating"
    git -C "$CTS_G_ROOT" checkout "$BRANCH"
    git -C "$CTS_G_ROOT" merge --ff-only "$GIT_REMOTE/$BRANCH" \
      || die "non-fast-forward update; backup preserved"
    ok "fast-forward $BRANCH"
  fi
else
  die "no git clone at $CTS_G_ROOT and no --from-dir given"
fi

find "$CTS_G_ROOT/deploy" -maxdepth 1 -type f -name '*.sh' ! -name 'linux-common.sh' -exec chmod 755 {} +
configure_git "$CTS_G_ROOT"
seed_env
sync_pulse_tree
npm_install_desk
npm_build_desk
install_units
configure_host_log_retention

if [[ "$NO_RESTART" -eq 1 ]]; then
  skip "restart"
  print_results
else
  start_live="$START_LIVE"
  start_stack "$start_live"
  health_report
fi

log "update complete — open positions were not flattened"
if [[ ${#STEPS_FAIL[@]} -gt 0 ]]; then
  exit 1
fi
exit 0
