#!/usr/bin/env bash
# CTS-G Linux install — unattended, skip software already present, run to the end.
#
#   sudo ./deploy/install-linux.sh
#   sudo ./deploy/install-linux.sh --port 3102 --name cts-g
#   sudo ./deploy/install-linux.sh --from-dir /path/to/CTS-G --start-live
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=linux-common.sh
source "$HERE/linux-common.sh"

FROM_DIR=""
DO_CLONE=0
START_LIVE=0
NO_START=0
PORT_EXPLICIT=0
PULSE_PORT_EXPLICIT=0
REDIS_DB_EXPLICIT=0
STATE_EXPLICIT=0
NAME_EXPLICIT=0

usage() {
  cat <<'EOF'
CTS-G Linux install (unattended)

Usage: sudo ./deploy/install-linux.sh [options]

  --name NAME       Install name (default: cts-g) → /opt/NAME, /etc/NAME
  --port N          Desk listen port (default: 3102)
  --desk-port N     Same as --port
  --pulse-port N    Pulse HTTP port (default: 3015; new custom installs: desk+1)
  --redis-db N      Independent Redis logical DB 0..15 (default: 1)
  --state-dir PATH  Durable state root (default: /var/lib/cts/instances/NAME)
  --host HOST       Public hostname/IP for result URLs (default: 152.53.114.112)
  --from-dir PATH   Copy this checkout into /opt/NAME (default: this repo)
  --clone           git clone REPO_URL into /opt/NAME
  --repo URL        Git remote (default: https://github.com/mxssnx-creator/CTS-G.git)
  --branch NAME     Branch (default: main)
  --start-live      Start NAME-pulse@bingx-x01 (never implicit)
  --no-start        Install units but do not start services
  --yes             No-op (install never prompts)
  -h, --help

Packages and Node are installed only if missing. Overlay/open positions are
never flattened. Prints a results block with URLs at the end.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name|-n) apply_name "${2:-}"; NAME_EXPLICIT=1; shift 2 ;;
    --port|-p|--desk-port) DESK_PORT="${2:-}"; PORT_EXPLICIT=1; shift 2 ;;
    --pulse-port) PULSE_PORT="${2:-}"; PULSE_PORT_EXPLICIT=1; shift 2 ;;
    --redis-db) REDIS_DB="${2:-}"; REDIS_DB_EXPLICIT=1; shift 2 ;;
    --state-dir) STATE_DIR="${2:-}"; STATE_EXPLICIT=1; shift 2 ;;
    --host) PUBLIC_HOST="${2:-}"; REMOTE_HOST="${2:-}"; shift 2 ;;
    --from-dir) FROM_DIR="${2:-}"; shift 2 ;;
    --clone) DO_CLONE=1; shift ;;
    --repo) REPO_URL="${2:-}"; shift 2 ;;
    --branch) BRANCH="${2:-}"; shift 2 ;;
    --start-live) START_LIVE=1; shift ;;
    --no-start) NO_START=1; shift ;;
    --yes|-y) shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

require_linux
require_root

load_existing_env_config
if [[ "$PORT_EXPLICIT" == "1" && "$PULSE_PORT_EXPLICIT" != "1" && ! -f "$ENV_FILE" && "$DESK_PORT" != "3102" ]]; then
  PULSE_PORT=$((DESK_PORT + 1))
fi
validate_instance_config
lock_and_check_instance

log "install  name=$CTS_G_NAME  root=$CTS_G_ROOT  desk=:${DESK_PORT}  branch=$BRANCH"
log "unattended — no prompts; skip software already present"

ensure_base_packages
ensure_node
ensure_dirs
ensure_redis
quiesce_instance
create_verified_backup
migrate_legacy_redis_state

if [[ "$DO_CLONE" -eq 1 ]]; then
  if [[ -d "$CTS_G_ROOT/.git" ]]; then
    log "existing clone at $CTS_G_ROOT — fetch $BRANCH"
    git -C "$CTS_G_ROOT" remote set-url origin "$REPO_URL" 2>/dev/null \
      || git -C "$CTS_G_ROOT" remote add origin "$REPO_URL"
    git -C "$CTS_G_ROOT" fetch --prune origin
    [[ -z "$(git -C "$CTS_G_ROOT" status --porcelain --untracked-files=no)" ]] || die "local code changes preserved; resolve them before updating"
    git -C "$CTS_G_ROOT" checkout "$BRANCH"
    git -C "$CTS_G_ROOT" merge --ff-only "origin/$BRANCH" || die "non-fast-forward install; backup preserved"
    ok "git updated $CTS_G_ROOT"
  elif [[ -f "$CTS_G_ROOT/server/pulse/pulse_trader.py" ]]; then
    skip "tree already at $CTS_G_ROOT (not a clone)"
  else
    mkdir -p "$(dirname "$CTS_G_ROOT")"
    git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$CTS_G_ROOT"
    ok "git clone $CTS_G_ROOT"
  fi
else
  SRC="${FROM_DIR:-$(script_repo_root)}"
  [[ -f "$SRC/server/pulse/pulse_trader.py" ]] || die "not a CTS-G tree: $SRC"
  if [[ "$(cd "$SRC" && pwd)" != "$(cd "$CTS_G_ROOT" 2>/dev/null && pwd || true)" ]]; then
    sync_app_tree "$SRC"
  else
    skip "app tree (already $CTS_G_ROOT)"
  fi
fi

[[ -f "$CTS_G_ROOT/deploy/linux-common.sh" ]] || die "install did not land deploy/ in $CTS_G_ROOT"
find "$CTS_G_ROOT/deploy" -maxdepth 1 -type f -name '*.sh' ! -name 'linux-common.sh' -exec chmod 755 {} +

configure_git "$CTS_G_ROOT"
seed_env
sync_pulse_tree
npm_install_desk
npm_build_desk
install_units
configure_host_log_retention
enable_stack

if [[ "$NO_START" -eq 1 ]]; then
  skip "start ( --no-start )"
  print_results
else
  start_stack "$START_LIVE"
  health_report
fi

if [[ ${#STEPS_FAIL[@]} -gt 0 ]]; then
  warn "finished with failures: ${STEPS_FAIL[*]}"
  exit 1
fi
log "install complete"
exit 0
