#!/usr/bin/env bash
# Shared helpers for CTS-G Linux install / update. Sourced, not executed.
# Always non-interactive. Packages/software are installed only when missing.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
export APT_LISTCHANGES_FRONTEND=none
export GIT_TERMINAL_PROMPT=0
export GIT_ASKPASS=/bin/true
export npm_config_fund=false
export npm_config_audit=false
export npm_config_update_notifier=false
export npm_config_yes=true

CTS_G_NAME="${CTS_G_NAME:-cts-g}"
CTS_G_ROOT="${CTS_G_ROOT:-/opt/${CTS_G_NAME}}"
PULSE_DIR="${PULSE_DIR:-/opt/grok-x01-pulse}"
ETC_DIR="${ETC_DIR:-/etc/${CTS_G_NAME}}"
LOG_DIR="${LOG_DIR:-/var/log/${CTS_G_NAME}}"
ENV_FILE="${ENV_FILE:-$ETC_DIR/cts-g.env}"
REPO_URL="${REPO_URL:-https://github.com/mxssnx-creator/CTS-G.git}"
BRANCH="${BRANCH:-main}"
GIT_REMOTE="${GIT_REMOTE:-origin}"
GIT_USER_NAME="${GIT_USER_NAME:-xssnet}"
GIT_USER_EMAIL="${GIT_USER_EMAIL:-mxssnx@gmail.com}"
DESK_HOST="${DESK_HOST:-0.0.0.0}"
DESK_PORT="${DESK_PORT:-3102}"
PULSE_PORT="${PULSE_PORT:-3015}"
REMOTE_HOST="${REMOTE_HOST:-152.53.114.112}"
PUBLIC_HOST="${PUBLIC_HOST:-}"
LIVE_SLOT="${LIVE_SLOT:-bingx-x01}"
VST_SLOT="${VST_SLOT:-bingx-x02}"

STEPS_OK=()
STEPS_SKIP=()
STEPS_FAIL=()
PKG_INSTALLED=()
PKG_SKIPPED=()

log()  { printf '[%s] %s\n' "$CTS_G_NAME" "$*"; }
warn() { printf '[%s] WARN %s\n' "$CTS_G_NAME" "$*" >&2; }
die()  { printf '[%s] ERROR %s\n' "$CTS_G_NAME" "$*" >&2; exit 1; }

ok()   { STEPS_OK+=("$1"); log "ok    $1"; }
skip() { STEPS_SKIP+=("$1"); log "skip  $1"; }
fail() { STEPS_FAIL+=("$1"); warn "fail  $1"; }

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

apply_name() {
  # --name changes install root / etc / logs. Pulse dir stays hardcoded in the engine.
  CTS_G_NAME="${1:-$CTS_G_NAME}"
  CTS_G_NAME="${CTS_G_NAME//[^A-Za-z0-9._-]/}"
  [[ -n "$CTS_G_NAME" ]] || CTS_G_NAME=cts-g
  CTS_G_ROOT="/opt/${CTS_G_NAME}"
  ETC_DIR="/etc/${CTS_G_NAME}"
  LOG_DIR="/var/log/${CTS_G_NAME}"
  ENV_FILE="$ETC_DIR/cts-g.env"
}

detect_pkg() {
  if have apt-get; then echo apt
  elif have dnf; then echo dnf
  elif have yum; then echo yum
  else echo none
  fi
}

pkg_is_installed() {
  local p="$1" kind
  kind="$(detect_pkg)"
  case "$kind" in
    apt) dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'install ok installed' ;;
    dnf|yum) rpm -q "$p" >/dev/null 2>&1 ;;
    *) have "$p" ;;
  esac
}

pkg_install_missing() {
  local kind missing=() p
  kind="$(detect_pkg)"
  [[ "$kind" != none ]] || { fail "no package manager"; return 0; }
  for p in "$@"; do
    if pkg_is_installed "$p" || have "$p"; then
      PKG_SKIPPED+=("$p")
    else
      missing+=("$p")
    fi
  done
  if [[ ${#missing[@]} -eq 0 ]]; then
    skip "packages (already installed)"
    return 0
  fi
  log "installing packages: ${missing[*]}"
  case "$kind" in
    apt)
      apt-get update -y -qq
      apt-get install -y -qq --no-install-recommends \
        -o Dpkg::Options::=--force-confdef \
        -o Dpkg::Options::=--force-confold \
        "${missing[@]}"
      ;;
    dnf) dnf install -y "${missing[@]}" ;;
    yum) yum install -y "${missing[@]}" ;;
  esac
  PKG_INSTALLED+=("${missing[@]}")
  ok "packages ${missing[*]}"
}

ensure_base_packages() {
  local kind redis
  kind="$(detect_pkg)"
  redis="redis-server"
  [[ "$kind" != apt ]] && redis="redis"
  pkg_install_missing ca-certificates curl git rsync python3 "$redis"
  if [[ "$kind" == apt ]]; then
    pkg_install_missing gnupg redis-tools python3-venv || true
  fi
  have python3 || die "python3 is required"
  have git || die "git is required"
  have rsync || die "rsync is required"
}

node_major() {
  have node || { echo 0; return; }
  node -p "parseInt(process.versions.node,10)" 2>/dev/null || echo 0
}

ensure_node() {
  local major kind
  major="$(node_major)"
  if [[ "$major" -ge 20 ]] && have npm; then
    skip "node $(node -v) / npm $(npm -v)"
    return 0
  fi
  kind="$(detect_pkg)"
  log "installing Node.js 22 (found major=$major)"
  if [[ "$kind" == apt ]]; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null
    pkg_install_missing nodejs
  else
    pkg_install_missing nodejs npm || {
      curl -fsSL https://rpm.nodesource.com/setup_22.x | bash - >/dev/null
      pkg_install_missing nodejs
    }
  fi
  have node && have npm || die "node/npm install failed"
  ok "node $(node -v) npm $(npm -v)"
}

ensure_redis() {
  local unit=""
  if systemctl list-unit-files redis-server.service >/dev/null 2>&1; then
    unit=redis-server.service
  elif systemctl list-unit-files redis.service >/dev/null 2>&1; then
    unit=redis.service
  fi
  if [[ -n "$unit" ]]; then
    if systemctl is-enabled --quiet "$unit" 2>/dev/null && systemctl is-active --quiet "$unit"; then
      skip "redis ($unit running)"
    else
      systemctl enable --now "$unit" >/dev/null 2>&1 || warn "could not enable $unit"
      ok "redis $unit"
    fi
  else
    warn "no redis systemd unit — start redis yourself if needed"
  fi
  if have redis-cli; then
    redis-cli ping >/dev/null 2>&1 && ok "redis ping" || fail "redis ping"
  fi
}

ensure_dirs() {
  mkdir -p "$CTS_G_ROOT" "$PULSE_DIR" "$ETC_DIR" "$LOG_DIR"
  chmod 755 "$CTS_G_ROOT" "$PULSE_DIR" "$ETC_DIR" "$LOG_DIR"
  ok "dirs $CTS_G_ROOT $PULSE_DIR $ETC_DIR $LOG_DIR"
}

write_env_file() {
  mkdir -p "$ETC_DIR"
  cat >"$ENV_FILE" <<EOF
PULSE_URL=http://127.0.0.1:${PULSE_PORT}
CTS_URL=http://127.0.0.1
HOST=${DESK_HOST}
PORT=${DESK_PORT}
PYTHONUNBUFFERED=1
PYTHONDONTWRITEBYTECODE=1
CTS_G_NAME=${CTS_G_NAME}
CTS_G_ROOT=${CTS_G_ROOT}
EOF
  chmod 0644 "$ENV_FILE"
}

seed_env() {
  mkdir -p "$ETC_DIR"
  if [[ ! -f "$ENV_FILE" ]]; then
    write_env_file
    ok "env $ENV_FILE (desk :$DESK_PORT)"
  elif [[ "${FORCE_ENV:-0}" == "1" ]]; then
    write_env_file
    ok "env $ENV_FILE rewritten (desk :$DESK_PORT)"
  else
    # Keep keys/custom lines; still refresh PORT if --port was passed.
    if [[ "${PORT_EXPLICIT:-0}" == "1" ]]; then
      if grep -q '^PORT=' "$ENV_FILE"; then
        sed -i "s|^PORT=.*|PORT=${DESK_PORT}|" "$ENV_FILE"
      else
        printf 'PORT=%s\n' "$DESK_PORT" >>"$ENV_FILE"
      fi
      ok "env PORT=${DESK_PORT}"
    else
      skip "env $ENV_FILE"
    fi
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
  git -C "$root" config advice.detachedHead false
  if git -C "$root" remote get-url "$GIT_REMOTE" >/dev/null 2>&1; then
    git -C "$root" remote set-url "$GIT_REMOTE" "$REPO_URL"
  else
    git -C "$root" remote add "$GIT_REMOTE" "$REPO_URL"
  fi
  git -C "$root" branch -M "$BRANCH" 2>/dev/null || true
  ok "git $GIT_REMOTE=$REPO_URL  $GIT_USER_NAME <$GIT_USER_EMAIL>"
}

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

restore_pulse_trader() {
  # server/pulse/pulse_trader.py is a placeholder in git (233KB engine). Rebuild the
  # real engine from the pinned base blob + restore/pulse_trader.py.patch and hard
  # verify both input and output git blob hashes. Runs on every install/update.
  local pt="$PULSE_DIR/pulse_trader.py"
  local pt_want="7d3da43156cbc79a84a5df802a8206ba25223b0d"
  local pt_base="b3a9ff3c60c72864ac5558f488d7e6991bb31d76"
  local pt_patch="$CTS_G_ROOT/restore/pulse_trader.py.patch"
  if [[ "$(git hash-object "$pt" 2>/dev/null || true)" == "$pt_want" ]]; then
    skip "pulse_trader.py already current"
    return 0
  fi
  [[ -f "$pt_patch" ]] || die "restore patch missing: $pt_patch"
  local scratch
  scratch="$(mktemp -d)"
  mkdir -p "$scratch/server/pulse"
  if curl -fsSL -m 90 -o "$scratch/server/pulse/pulse_trader.py" \
      "https://raw.githubusercontent.com/mxssnx-creator/CTS-G/2b3432d7b3/server/pulse/pulse_trader.py" \
    && [[ "$(git hash-object "$scratch/server/pulse/pulse_trader.py")" == "$pt_base" ]] \
    && git -C "$scratch" apply "$pt_patch" \
    && [[ "$(git hash-object "$scratch/server/pulse/pulse_trader.py")" == "$pt_want" ]]; then
    cp -a "$scratch/server/pulse/pulse_trader.py" "$pt"
    rm -rf "$scratch"
    ok "pulse_trader.py restored from verified patch"
    return 0
  fi
  rm -rf "$scratch"
  die "pulse_trader.py restore failed (base/patch/result hash mismatch)"
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
  [[ -f "$src/universe.json" ]] && cp -a "$src/universe.json" "$PULSE_DIR/universe.json"
  restore_pulse_trader
  python3 -m py_compile "$PULSE_DIR"/pulse_trader.py "$PULSE_DIR"/pulse_http.py \
    "$PULSE_DIR"/block_engine.py "$PULSE_DIR"/dca_engine.py "$PULSE_DIR"/set_engine.py
  ok "pulse tree $PULSE_DIR"
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
  ok "app tree $CTS_G_ROOT"
}

render_unit() {
  local src="$1" dest="$2"
  sed \
    -e "s|/opt/cts-g|${CTS_G_ROOT}|g" \
    -e "s|/etc/cts-g|${ETC_DIR}|g" \
    -e "s|:3102|:${DESK_PORT}|g" \
    -e "s|PORT=3102|PORT=${DESK_PORT}|g" \
    -e "s|CTS-G desk UI|${CTS_G_NAME} desk UI|g" \
    "$src" >"$dest"
}

install_units() {
  local unit
  for unit in grok-pulse@.service grok-pulse-http.service grok-desk.service grok-pulse.target; do
    render_unit "$CTS_G_ROOT/deploy/$unit" "/etc/systemd/system/$unit"
  done
  systemctl daemon-reload
  ok "systemd units"
}

npm_install_desk() {
  [[ -f "$CTS_G_ROOT/package.json" ]] || die "package.json missing in $CTS_G_ROOT"
  local oldpwd="$PWD"
  cd "$CTS_G_ROOT"
  if [[ -d node_modules/vite && -f package-lock.json && ! package.json -nt node_modules ]]; then
    skip "npm (node_modules current)"
    cd "$oldpwd"
    return 0
  fi
  if [[ -f package-lock.json ]]; then
    npm ci --no-fund --no-audit --no-progress
  else
    npm install --no-fund --no-audit --no-progress
  fi
  cd "$oldpwd"
  ok "npm install"
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
  systemctl enable grok-pulse.target grok-pulse-http.service grok-desk.service >/dev/null 2>&1 || true
  systemctl enable "grok-pulse@${VST_SLOT}.service" >/dev/null 2>&1 || true
  systemctl enable "grok-pulse@${LIVE_SLOT}.service" >/dev/null 2>&1 || true
  ok "units enabled"
}

start_stack() {
  local start_live="${1:-0}"
  systemctl restart grok-pulse-http.service || fail "start grok-pulse-http"
  systemctl restart grok-desk.service || fail "start grok-desk"
  systemctl restart "grok-pulse@${VST_SLOT}.service" || fail "start vst"
  if [[ "$start_live" == "1" ]]; then
    systemctl restart "grok-pulse@${LIVE_SLOT}.service" || fail "start live"
  elif redis_has_keys "$LIVE_SLOT"; then
    log "Live keys present — starting $LIVE_SLOT"
    systemctl restart "grok-pulse@${LIVE_SLOT}.service" || fail "start live"
  else
    skip "live engine (no Redis api_key/api_secret)"
  fi
  systemctl start grok-pulse.target >/dev/null 2>&1 || true
}

wait_http() {
  local url="$1" tries="${2:-40}" delay="${3:-1}"
  local i
  i=0
  while [[ "$i" -lt "$tries" ]]; do
    if curl -sf -o /dev/null --max-time 2 "$url"; then
      return 0
    fi
    i=$((i + 1))
    sleep "$delay"
  done
  return 1
}

public_host() {
  if [[ -n "${PUBLIC_HOST}" ]]; then
    printf '%s\n' "$PUBLIC_HOST"
    return
  fi
  printf '%s\n' "$REMOTE_HOST"
}

local_ip() {
  hostname -I 2>/dev/null | awk '{print $1}'
}

unit_state() {
  systemctl is-active "$1" 2>/dev/null || echo unknown
}

git_head() {
  git -C "$CTS_G_ROOT" rev-parse --short HEAD 2>/dev/null || echo none
}

print_results() {
  local host ip desk pulse repo sha
  host="$(public_host)"
  ip="$(local_ip)"
  desk="http://${host}:${DESK_PORT}/"
  pulse="http://${host}:${PULSE_PORT}"
  repo="$REPO_URL"
  sha="$(git_head)"
  echo
  echo "======== ${CTS_G_NAME} install results ========"
  echo "name        $CTS_G_NAME"
  echo "root        $CTS_G_ROOT"
  echo "pulse dir   $PULSE_DIR"
  echo "env         $ENV_FILE"
  echo "git         $repo"
  echo "branch      $BRANCH"
  echo "sha         $sha"
  echo "user        $GIT_USER_NAME <$GIT_USER_EMAIL>"
  echo "desk bind   ${DESK_HOST}:${DESK_PORT}"
  echo
  echo "units"
  printf '  %-36s %s\n' grok-pulse-http.service "$(unit_state grok-pulse-http.service)"
  printf '  %-36s %s\n' grok-desk.service "$(unit_state grok-desk.service)"
  printf '  %-36s %s\n' "grok-pulse@${VST_SLOT}.service" "$(unit_state grok-pulse@${VST_SLOT}.service)"
  printf '  %-36s %s\n' "grok-pulse@${LIVE_SLOT}.service" "$(unit_state grok-pulse@${LIVE_SLOT}.service)"
  echo
  echo "packages installed  ${PKG_INSTALLED[*]:-(none)}"
  echo "packages skipped    ${PKG_SKIPPED[*]:-(none)}"
  echo "steps ok            ${STEPS_OK[*]:-(none)}"
  echo "steps skipped       ${STEPS_SKIP[*]:-(none)}"
  echo "steps failed        ${STEPS_FAIL[*]:-(none)}"
  echo
  echo "URLs"
  echo "  Desk UI           $desk"
  echo "  Desk (local)      http://127.0.0.1:${DESK_PORT}/"
  [[ -n "$ip" ]] && echo "  Desk (LAN)        http://${ip}:${DESK_PORT}/"
  echo "  Pulse stats       ${pulse}/stats.json?conn=overall"
  echo "  Pulse live        ${pulse}/stats.json?conn=live"
  echo "  Pulse VST         ${pulse}/stats.json?conn=vst"
  echo "  Pulse control     ${pulse}/control.json"
  echo "  Pulse config      ${pulse}/config.json?conn=live"
  echo "  GitHub            https://github.com/mxssnx-creator/CTS-G"
  echo "  Commit            https://github.com/mxssnx-creator/CTS-G/commit/${sha}"
  echo
  if redis_has_keys "$LIVE_SLOT"; then
    echo "Live keys           present in Redis connection:${LIVE_SLOT}"
  else
    echo "Live keys           missing — set then restart:"
    echo "  redis-cli HSET connection:${LIVE_SLOT} api_key '…' api_secret '…'"
    echo "  systemctl restart grok-pulse@${LIVE_SLOT}"
  fi
  echo "=============================================="
}

health_report() {
  if wait_http "http://127.0.0.1:${PULSE_PORT}/stats.json?conn=overall" 12 1; then
    ok "pulse :${PULSE_PORT}"
  else
    fail "pulse :${PULSE_PORT} not answering"
  fi
  if wait_http "http://127.0.0.1:${DESK_PORT}/" 40 1; then
    ok "desk :${DESK_PORT}"
  else
    fail "desk :${DESK_PORT} not answering yet"
  fi
  print_results
}
