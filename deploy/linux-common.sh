#!/usr/bin/env bash
# Shared helpers for CTS-G Linux install / update. Sourced, not executed.
set -euo pipefail

CTS_G_ROOT="${CTS_G_ROOT:-/opt/cts-g}"
PULSE_DIR="${PULSE_DIR:-/opt/grok-x01-pulse}"
ETC_DIR="${ETC_DIR:-/etc/cts-g}"
LOG_DIR="${LOG_DIR:-/var/log/cts-g}"
ENV_FILE="${ENV_FILE:-$ETC_DIR/cts-g.env}"
REPO_URL="${REPO_URL:-https://github.com/mxssnx-creator/CTS-G.git}"
BRANCH="${BRANCH:-main}"
GIT_REMOTE="${GIT_REMOTE:-origin}"
GIT_USER_NAME="${GIT_USER_NAME:-xssnet}"
GIT_USER_EMAIL="${GIT_USER_EMAIL:-mxssnx@gmail.com}"
DESK_HOST="${DESK_HOST:-0.0.0.0}"
DESK_PORT="${DESK_PORT:-3102}"
REMOTE_HOST="${REMOTE_HOST:-152.53.114.112}"
LIVE_SLOT="${LIVE_SLOT:-bingx-x01}"
VST_SLOT="${VST_SLOT:-bingx-x02}"

LIVE_UNITS=(
  grok-pulse-http.service
  grok-desk.service
  "grok-pulse@${VST_SLOT}.service"
  "grok-pulse@${LIVE_SLOT}.service"
)

log()  { printf '[cts-g] %s\n' "$*"; }
warn() { printf '[cts-g] WARN %s\n' "$*" >&2; }
die()  { printf '[cts-g] ERROR %s\n' "$*" >&2; exit 1; }

require_root() {
  [[ "$(id -u)" -eq 0 ]] || die "run as root (sudo $0)"
}

require_linux() {
  [[ "$(uname -s)" == Linux ]] || die "Linux only"
}

script_repo_root() {
  local caller="${BASH_SOURCE[1]:-${BASH_SOURCE[0]}}"
  local here
  here="$(cd "$(dirname "$caller")" && pwd)"
  cd "$here/.." && pwd
}

have() { command -v "$1" >/dev/null 2>&1; }

detect_pkg() {
  if have apt-get; then echo apt
  elif have dnf; then echo dnf
  elif have yum; then echo yum
  else die "need apt-get or dnf"
  fi
}

pkg_install() {
  local kind
  kind="$(detect_pkg)"
  case "$kind" in
    apt)
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -y
      apt-get install -y --no-install-recommends "$@"
      ;;
    dnf) dnf install -y "$@" ;;
    yum) yum install -y "$@" ;;
  esac
}

ensure_base_packages() {
  local kind py redis curl_pkg
  kind="$(detect_pkg)"
  py="python3"
  curl_pkg="curl"
  redis="redis-server"
  [[ "$kind" != apt ]] && redis="redis"
  log "installing packages ($kind)"
  pkg_install ca-certificates curl git rsync gnupg "$py" "$redis" systemd
  if [[ "$kind" == apt ]]; then
    pkg_install python3-minimal redis-tools || true
  fi
}

node_major() {
  have node || { echo 0; return; }
  node -p "parseInt(process.versions.node,10)" 2>/dev/null || echo 0
}

ensure_node() {
  local major
  major="$(node_major)"
  if [[ "$major" -ge 20 ]]; then
    log "node $(node -v) already ok"
    have npm || die "npm missing next to node"
    return
  fi
  log "installing Node.js 22"
  if [[ "$(detect_pkg)" == apt ]]; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    pkg_install nodejs
  else
    pkg_install nodejs npm || {
      curl -fsSL https://rpm.nodesource.com/setup_22.x | bash -
      pkg_install nodejs
    }
  fi
  have node && have npm || die "node/npm install failed"
  log "node $(node -v) npm $(npm -v)"
}

ensure_redis() {
  systemctl enable --now redis-server.service 2>/dev/null \
    || systemctl enable --now redis.service 2>/dev/null \
    || warn "could not enable redis systemd unit — start it yourself"
  if have redis-cli; then
    redis-cli ping >/dev/null 2>&1 || warn "redis-cli ping failed (keys can still be set later)"
  fi
}

ensure_dirs() {
  mkdir -p "$CTS_G_ROOT" "$PULSE_DIR" "$ETC_DIR" "$LOG_DIR"
  chmod 755 "$CTS_G_ROOT" "$PULSE_DIR" "$ETC_DIR" "$LOG_DIR"
}

seed_env() {
  mkdir -p "$ETC_DIR"
  if [[ ! -f "$ENV_FILE" ]]; then
    if [[ -f "$CTS_G_ROOT/deploy/cts-g.env.example" ]]; then
      sed \
        -e "s|^PULSE_URL=.*|PULSE_URL=http://127.0.0.1:3015|" \
        -e "s|^CTS_URL=.*|CTS_URL=http://127.0.0.1|" \
        -e "s|^HOST=.*|HOST=${DESK_HOST}|" \
        -e "s|^PORT=.*|PORT=${DESK_PORT}|" \
        "$CTS_G_ROOT/deploy/cts-g.env.example" >"$ENV_FILE"
    else
      cat >"$ENV_FILE" <<EOF
PULSE_URL=http://127.0.0.1:3015
CTS_URL=http://127.0.0.1
HOST=${DESK_HOST}
PORT=${DESK_PORT}
PYTHONUNBUFFERED=1
PYTHONDONTWRITEBYTECODE=1
EOF
    fi
    chmod 0644 "$ENV_FILE"
    log "wrote $ENV_FILE (desk :$DESK_PORT)"
  else
    log "keeping existing $ENV_FILE"
  fi
}

configure_git() {
  local root="${1:-$CTS_G_ROOT}"
  [[ -d "$root" ]] || die "git tree missing: $root"
  if [[ ! -d "$root/.git" ]]; then
    git -C "$root" init -b "$BRANCH" >/dev/null
    log "git init $root ($BRANCH)"
  fi
  git -C "$root" config user.name "$GIT_USER_NAME"
  git -C "$root" config user.email "$GIT_USER_EMAIL"
  git -C "$root" config pull.ff only
  git -C "$root" config init.defaultBranch "$BRANCH"
  if git -C "$root" remote get-url "$GIT_REMOTE" >/dev/null 2>&1; then
    git -C "$root" remote set-url "$GIT_REMOTE" "$REPO_URL"
  else
    git -C "$root" remote add "$GIT_REMOTE" "$REPO_URL"
  fi
  git -C "$root" branch -M "$BRANCH" 2>/dev/null || true
  log "git $GIT_REMOTE=$REPO_URL  user=$GIT_USER_NAME <$GIT_USER_EMAIL>"
}

# Runtime files the engine writes — never clobber on update.
pulse_rsync_excludes() {
  cat <<'EOF'
--exclude=__pycache__/
--exclude=*.pyc
--exclude=open-*.json
--exclude=block-state-*.json
--exclude=trades-*.jsonl
--exclude=stats-*.json
--exclude=STOP
--exclude=STOP-*
--exclude=PAUSE-*
--exclude=errors-*.jsonl
--exclude=lev-set-*.json
--exclude=cts-settings-*.json
--exclude=pulse-*.log
EOF
}

sync_pulse_tree() {
  local src="$CTS_G_ROOT/server/pulse"
  [[ -d "$src" ]] || die "pulse tree missing at $src"
  mkdir -p "$PULSE_DIR"
  local keep_overlay=0
  [[ -f "$PULSE_DIR/overlay-${LIVE_SLOT}.json" ]] && keep_overlay=1

  local args=(-a)
  while IFS= read -r line; do
    [[ -n "$line" ]] && args+=("$line")
  done < <(pulse_rsync_excludes)
  if [[ "$keep_overlay" -eq 1 ]]; then
    args+=(--exclude='overlay-*.json')
  fi
  rsync "${args[@]}" "$src/" "$PULSE_DIR/"

  local f
  for f in "overlay-${LIVE_SLOT}.json" "overlay-${VST_SLOT}.json" universe.json; do
    if [[ ! -f "$PULSE_DIR/$f" && -f "$src/$f" ]]; then
      cp -a "$src/$f" "$PULSE_DIR/$f"
    fi
  done
  # universe cache can refresh; overlays stay put once seeded
  [[ -f "$src/universe.json" ]] && cp -a "$src/universe.json" "$PULSE_DIR/universe.json"
  python3 -m py_compile "$PULSE_DIR"/pulse_trader.py "$PULSE_DIR"/pulse_http.py \
    "$PULSE_DIR"/block_engine.py "$PULSE_DIR"/dca_engine.py "$PULSE_DIR"/set_engine.py
  log "pulse tree synced → $PULSE_DIR"
}

sync_app_tree() {
  local from="$1"
  [[ -d "$from" ]] || die "source tree missing: $from"
  mkdir -p "$CTS_G_ROOT"
  rsync -a \
    --exclude 'node_modules/' \
    --exclude 'dist/' \
    --exclude '.tanstack/' \
    --exclude '.nitro/' \
    --exclude '.vercel/' \
    --exclude 'artifacts/' \
    --exclude 'attachments/' \
    --exclude 'screenshots/' \
    --exclude '.preview.pid' \
    --exclude 'preview.log' \
    --exclude '__pycache__/' \
    "$from/" "$CTS_G_ROOT/"
  chmod 755 "$CTS_G_ROOT/deploy/"*.sh 2>/dev/null || true
  log "app tree synced → $CTS_G_ROOT"
}

install_units() {
  local unit
  for unit in grok-pulse@.service grok-pulse-http.service grok-desk.service grok-pulse.target; do
    install -m 0644 "$CTS_G_ROOT/deploy/$unit" "/etc/systemd/system/$unit"
  done
  systemctl daemon-reload
  log "systemd units installed"
}

npm_install_desk() {
  [[ -f "$CTS_G_ROOT/package.json" ]] || die "package.json missing in $CTS_G_ROOT"
  (
    cd "$CTS_G_ROOT"
    if [[ -f package-lock.json ]]; then
      npm ci
    else
      npm install
    fi
  )
  log "npm install complete"
}

redis_has_keys() {
  local slot="$1"
  have redis-cli || return 1
  local key sec
  key="$(redis-cli HGET "connection:$slot" api_key 2>/dev/null || true)"
  sec="$(redis-cli HGET "connection:$slot" api_secret 2>/dev/null || true)"
  [[ -n "$key" && -n "$sec" && "$key" != "(nil)" && "$sec" != "(nil)" ]]
}

enable_stack() {
  systemctl enable grok-pulse.target grok-pulse-http.service grok-desk.service
  systemctl enable "grok-pulse@${VST_SLOT}.service"
  systemctl enable "grok-pulse@${LIVE_SLOT}.service"
}

start_stack() {
  local start_live="${1:-0}"
  systemctl restart grok-pulse-http.service
  systemctl restart grok-desk.service
  systemctl restart "grok-pulse@${VST_SLOT}.service"
  if [[ "$start_live" == "1" ]]; then
    systemctl restart "grok-pulse@${LIVE_SLOT}.service"
  elif redis_has_keys "$LIVE_SLOT"; then
    log "Live keys present — starting $LIVE_SLOT"
    systemctl restart "grok-pulse@${LIVE_SLOT}.service"
  else
    warn "Live $LIVE_SLOT not started (no Redis api_key/api_secret). VST is up."
  fi
  systemctl start grok-pulse.target || true
}

wait_http() {
  local url="$1" tries="${2:-30}" delay="${3:-1}"
  local i
  for i in $(seq 1 "$tries"); do
    if curl -sf -o /dev/null --max-time 2 "$url"; then
      return 0
    fi
    sleep "$delay"
  done
  return 1
}

health_report() {
  local u
  echo
  log "unit status"
  for u in grok-pulse-http.service grok-desk.service "grok-pulse@${VST_SLOT}.service" "grok-pulse@${LIVE_SLOT}.service"; do
    printf '  %-36s %s\n' "$u" "$(systemctl is-active "$u" 2>/dev/null || echo unknown)"
  done
  if wait_http "http://127.0.0.1:3015/stats.json?conn=overall" 8 1; then
    log "pulse HTTP :3015 ok"
  else
    warn "pulse HTTP :3015 not answering yet"
  fi
  if wait_http "http://127.0.0.1:${DESK_PORT}/" 20 1; then
    log "desk :${DESK_PORT} ok"
  else
    warn "desk :${DESK_PORT} not answering yet (first npm/vite boot can take a minute)"
  fi
}

print_keys_help() {
  cat <<EOF

Add BingX keys (never stored in the repo):

  redis-cli HSET connection:${LIVE_SLOT} api_key 'YOUR_LIVE_KEY' api_secret 'YOUR_LIVE_SECRET'
  redis-cli HSET connection:${VST_SLOT} api_key 'YOUR_VST_KEY' api_secret 'YOUR_VST_SECRET'
  systemctl restart grok-pulse@${LIVE_SLOT}

Control:
  systemctl status grok-pulse.target
  journalctl -u grok-pulse@${LIVE_SLOT} -f
  curl -s http://127.0.0.1:${DESK_PORT}/ | head
  journalctl -u grok-desk -n 50

EOF
}
