#!/usr/bin/env bash
# Shared helpers for CTS-G Linux install / update. Sourced, not executed.
# Always non-interactive. Packages/software are installed only when missing.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l
export APT_LISTCHANGES_FRONTEND=none
export GIT_TERMINAL_PROMPT=0
export GIT_ASKPASS=/bin/true
export npm_config_fund=false
export npm_config_audit=false
export npm_config_update_notifier=false
export npm_config_yes=true

CTS_G_NAME="${CTS_G_NAME:-cts-g}"
CTS_INSTALL_PREFIX="${CTS_INSTALL_PREFIX:-}"
CTS_G_ROOT="${CTS_INSTALL_PREFIX}/opt/${CTS_G_NAME}"
PULSE_DIR="$CTS_G_ROOT/server/pulse"
CTS_DATA_DIR="${CTS_INSTALL_PREFIX}/var/lib/${CTS_G_NAME}"
LEGACY_PULSE_DIR="${CTS_LEGACY_PULSE_DIR:-}"
ETC_DIR="${CTS_INSTALL_PREFIX}/etc/${CTS_G_NAME}"
LOG_DIR="${CTS_INSTALL_PREFIX}/var/log/${CTS_G_NAME}"
UNIT_DIR="${CTS_INSTALL_PREFIX}/etc/systemd/system"
ENV_FILE="${ENV_FILE:-$ETC_DIR/cts-g.env}"
CREDENTIALS_ENV_FILE="${CREDENTIALS_ENV_FILE:-$ETC_DIR/credentials.env}"
REPO_URL="${REPO_URL:-https://github.com/mxssnx-creator/CTS-G.git}"
BRANCH="${BRANCH:-main}"
GIT_REMOTE="${GIT_REMOTE:-origin}"
GIT_USER_NAME="${GIT_USER_NAME:-xssnet}"
GIT_USER_EMAIL="${GIT_USER_EMAIL:-mxssnx@gmail.com}"
DESK_HOST="${DESK_HOST:-0.0.0.0}"
DESK_PORT="${DESK_PORT:-3102}"
PULSE_PORT="${PULSE_PORT:-}"
PULSE_PORT_EXPLICIT=0
CTS_REDIS_PREFIX="${CTS_G_NAME}:"
PYTHON_BIN="$CTS_G_ROOT/.venv/bin/python"
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
fail() { STEPS_FAIL+=("$1"); warn "fail $1"; }

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
  # --name changes every CTS-owned path. A separate pulse/data root prevents
  # another checkout using generic grok-* runtime files on the same host.
  [[ "${1:-}" =~ ^[a-z][a-z0-9-]{1,39}$ ]] || die "invalid install name (2–40 lowercase letters/digits/hyphens)"
  CTS_G_NAME="$1"
  CTS_G_ROOT="${CTS_INSTALL_PREFIX}/opt/${CTS_G_NAME}"
  PULSE_DIR="$CTS_G_ROOT/server/pulse"
  PYTHON_BIN="$CTS_G_ROOT/.venv/bin/python"
  CTS_DATA_DIR="${CTS_INSTALL_PREFIX}/var/lib/${CTS_G_NAME}"
  ETC_DIR="${CTS_INSTALL_PREFIX}/etc/${CTS_G_NAME}"
  LOG_DIR="${CTS_INSTALL_PREFIX}/var/log/${CTS_G_NAME}"
  ENV_FILE="$ETC_DIR/cts-g.env"
  CREDENTIALS_ENV_FILE="$ETC_DIR/credentials.env"
  CTS_REDIS_PREFIX="${CTS_G_NAME}:"
}

env_value() { sed -n "s/^${1}=//p" "$ENV_FILE" 2>/dev/null | tail -1; }

can_bind_port() {
  # Match the HTTP servers' restart semantics. A stopped listener may leave
  # accepted connections in TIME_WAIT; that is not another installation.
  # SO_REUSEADDR does not allow binding over an active listening socket.
  python3 -c 'import socket,sys; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(("0.0.0.0",int(sys.argv[1]))); s.close()' "$1" 2>/dev/null
}

validate_instance() {
  [[ "$CTS_G_NAME" =~ ^[a-z][a-z0-9-]{1,39}$ ]] || die "invalid install name"
  local saved_desk saved_pulse port unit
  saved_desk="$(env_value PORT || true)"
  saved_pulse="$(env_value PULSE_PORT || true)"
  [[ -n "$saved_pulse" ]] || saved_pulse="$(env_value PULSE_URL | sed -n 's|.*:\([0-9]*\)$|\1|p' || true)"
  if [[ "${PORT_EXPLICIT:-0}" != 1 && -n "$saved_desk" ]]; then DESK_PORT="$saved_desk"; fi
  if [[ "$PULSE_PORT_EXPLICIT" != 1 && -n "$saved_pulse" ]]; then PULSE_PORT="$saved_pulse"; fi
  [[ "$DESK_PORT" =~ ^[0-9]+$ ]] || die "invalid desk port"
  DESK_PORT=$((10#$DESK_PORT))
  PULSE_PORT="${PULSE_PORT:-$((DESK_PORT + 1))}"
  [[ "$PULSE_PORT" =~ ^[0-9]+$ ]] || die "invalid pulse port"
  PULSE_PORT=$((10#$PULSE_PORT))
  (( DESK_PORT >= 1024 && DESK_PORT <= 65535 && PULSE_PORT >= 1024 && PULSE_PORT <= 65535 && DESK_PORT != PULSE_PORT )) || die "ports must be distinct and between 1024 and 65535"
  for port in "$DESK_PORT" "$PULSE_PORT"; do
    if ! can_bind_port "$port"; then
      unit="$(desk_unit)"
      [[ "$port" == "$PULSE_PORT" ]] && unit="$(pulse_http_unit)"
      if [[ "$port" != "$saved_desk" && "$port" != "$saved_pulse" ]] || ! systemctl is-active --quiet "$unit"; then
        die "port $port is already in use; choose a free --port/--pulse-port"
      fi
    fi
  done
  export CTS_G_NAME CTS_G_ROOT PULSE_DIR CTS_DATA_DIR LOG_DIR CTS_REDIS_PREFIX PYTHON_BIN PULSE_PORT ENV_FILE
}

# Unit names are scoped to the installation name. Older releases used the
# shared grok-* names, which allowed another checkout on the same host to
# overwrite this project's desk or restart it with a different port.
desk_unit() { printf '%s-desk.service' "$CTS_G_NAME"; }
pulse_http_unit() { printf '%s-pulse-http.service' "$CTS_G_NAME"; }
pulse_template_unit() { printf '%s-pulse@.service' "$CTS_G_NAME"; }
pulse_instance_unit() { printf '%s-pulse@%s.service' "$CTS_G_NAME" "$1"; }
pulse_target_unit() { printf '%s-pulse.target' "$CTS_G_NAME"; }
retention_service_unit() { printf '%s-retention.service' "$CTS_G_NAME"; }
retention_timer_unit() { printf '%s-retention.timer' "$CTS_G_NAME"; }

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
    pkg_install_missing gnupg redis-tools python3-venv
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
  if have node && node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit((a===20&&b>=19)||(a===22&&b>=12)||a>=24?0:1)' && have npm; then
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
  # Redis may be shared with other projects. Never change global eviction,
  # persistence or memory settings as a side effect of installing this app.
  if have redis-cli; then
    redis-cli ping >/dev/null 2>&1 && ok "redis ping" || fail "redis ping"
  fi
}


ensure_dirs() {
  mkdir -p "$CTS_G_ROOT" "$CTS_DATA_DIR" "$ETC_DIR" "$LOG_DIR" "$UNIT_DIR"
  chmod 755 "$CTS_G_ROOT"
  chmod 700 "$ETC_DIR"
  chmod 750 "$LOG_DIR"
  chmod 750 "$CTS_DATA_DIR"
  ok "dirs $CTS_G_ROOT $PULSE_DIR $CTS_DATA_DIR $ETC_DIR $LOG_DIR"
}

write_env_file() {
  mkdir -p "$ETC_DIR"
  cat >"$ENV_FILE" <<EOF
PULSE_URL=http://127.0.0.1:${PULSE_PORT}
HOST=${DESK_HOST}
PORT=${DESK_PORT}
PYTHONUNBUFFERED=1
PYTHONDONTWRITEBYTECODE=1
CTS_G_NAME=${CTS_G_NAME}
CTS_G_ROOT=${CTS_G_ROOT}
PULSE_DIR=${PULSE_DIR}
CTS_DATA_DIR=${CTS_DATA_DIR}
CTS_STATE_DIR=${CTS_DATA_DIR}
LOG_DIR=${LOG_DIR}
CTS_MAX_RETAINED_LINES=1000
PULSE_PORT=${PULSE_PORT}
CTS_REDIS_PREFIX=${CTS_REDIS_PREFIX}
PYTHON_BIN=${PYTHON_BIN}
CTS_URL=
EOF
  chmod 0600 "$ENV_FILE"
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
    for pair in \
      "CTS_G_NAME=$CTS_G_NAME" \
      "CTS_G_ROOT=$CTS_G_ROOT" \
      "PULSE_DIR=$PULSE_DIR" \
      "CTS_DATA_DIR=$CTS_DATA_DIR" \
      "CTS_STATE_DIR=$CTS_DATA_DIR" \
      "LOG_DIR=$LOG_DIR" \
      "PULSE_PORT=$PULSE_PORT" \
      "PULSE_URL=http://127.0.0.1:$PULSE_PORT" \
      "CTS_REDIS_PREFIX=$CTS_REDIS_PREFIX" \
      "PYTHON_BIN=$PYTHON_BIN" \
      "CTS_MAX_RETAINED_LINES=1000"; do
      key="${pair%%=*}"
      value="${pair#*=}"
      if grep -q "^${key}=" "$ENV_FILE"; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
      else
        printf '%s\n' "$pair" >>"$ENV_FILE"
      fi
    done
    chmod 600 "$ENV_FILE"
  fi
}

ensure_python_deps() {
  python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"' || die "Python 3.11+ required"
  [[ -x "$PYTHON_BIN" ]] || python3 -m venv "$CTS_G_ROOT/.venv"
  "$PYTHON_BIN" -m pip install --disable-pip-version-check --no-input -r "$PULSE_DIR/requirements.txt"
  "$PYTHON_BIN" -m pip check
  "$PYTHON_BIN" -c 'import numpy, httpx, websocket, orjson'
  ok "isolated Python dependencies"
}

migrate_redis_scope() {
  # Only migrate the canonical existing install. A new named install must
  # never inherit another project's exchange credentials or settings.
  [[ "$CTS_G_NAME" == cts-g && -f "$ENV_FILE" ]] || return 0
  grep -q '^CTS_REDIS_PREFIX=' "$ENV_FILE" && return 0
  local slot key
  for slot in "$LIVE_SLOT" "$VST_SLOT"; do
    for key in "connection:$slot" "settings:connection_settings:$slot"; do
      redis-cli COPY "$key" "$CTS_REDIS_PREFIX$key" >/dev/null || die "Redis namespace migration failed"
    done
  done
  ok "copied existing connection keys into install namespace; originals retained"
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
  ok "git $GIT_REMOTE=$REPO_URL  $GIT_USER_NAME <$GIT_USER_EMAIL>"
}

sync_pulse_tree() {
  [[ -f "$PULSE_DIR/pulse_trader.py" ]] || die "in-place engine missing"
  migrate_legacy_state
  local file
  for file in "overlay-${LIVE_SLOT}.json" "overlay-${VST_SLOT}.json" universe.json; do
    if [[ ! -e "$CTS_DATA_DIR/$file" && -f "$PULSE_DIR/$file" ]]; then
      cp -a "$PULSE_DIR/$file" "$CTS_DATA_DIR/$file"
    fi
  done
  ok "engine source stays in $PULSE_DIR; state in $CTS_DATA_DIR"
}

migrate_legacy_state() {
  # The former CTS-G install kept durable state beside its source tree. Copy
  # only recognized state files, only when the new scoped data root is empty
  # for that name; never move/delete legacy files and never copy code/logs.
  local legacy="$LEGACY_PULSE_DIR" src name copied=0
  [[ -d "$legacy" && "$legacy" != "$CTS_DATA_DIR" ]] || return 0
  local names=(
    universe.json user-presets.json hist-calc.json hist-calc-req.json
    hist-calc-checkpoint.json overlay.json overlay-bingx-x01.json
    overlay-bingx-x02.json
  )
  local pattern
  for pattern in stats-*.json trades-*.jsonl block-state-*.json open-*.json pending-*.json \
    cts-settings-*.json errors-*.jsonl lev-set-*.json start-eq-*.json \
    results-export-*.json results-export-*.md events-*.json live-position-cost-*.json; do
    names+=("$pattern")
  done
  shopt -s nullglob
  for name in "${names[@]}"; do
    local matches=()
    if [[ "$name" == *'*'* ]]; then
      matches=("$legacy"/$name)
    else
      matches=("$legacy/$name")
    fi
    for src in "${matches[@]}"; do
      [[ -f "$src" ]] || continue
      local base="${src##*/}" dst="$CTS_DATA_DIR/${src##*/}"
      [[ -e "$dst" ]] && continue
      cp -a "$src" "$dst"
      copied=$((copied + 1))
    done
  done
  shopt -u nullglob
  if [[ "$copied" -gt 0 ]]; then
    ok "legacy state migrated $copied file(s) $legacy -> $CTS_DATA_DIR"
  else
    skip "legacy state (already scoped or empty)"
  fi
}

sync_app_tree() {
  local from="$1"
  [[ -d "$from" ]] || die "source tree missing: $from"
  mkdir -p "$CTS_G_ROOT"
  [[ ! -d "$CTS_G_ROOT/.git" ]] || die "existing Git install: use fast-forward update, not --from-dir overwrite"
  rsync -a \
    --exclude '.git/' \
    --exclude '.venv/' \
    --exclude 'deploy/units/' \
    --exclude 'deploy/instance.json' \
    --exclude '.env*' \
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
  find "$CTS_G_ROOT/deploy" -maxdepth 1 -type f -name '*.sh' ! -name 'linux-common.sh' -exec chmod 755 {} + 2>/dev/null || true
  ok "app tree $CTS_G_ROOT"
}

render_unit() {
  local src="$1" dest="$2"
  sed \
    -e "s|/opt/cts-g|${CTS_G_ROOT}|g" \
    -e "s|/opt/grok-x01-pulse|${PULSE_DIR}|g" \
    -e "s|/etc/cts-g|${ETC_DIR}|g" \
    -e "s|/var/log/cts-g|${LOG_DIR}|g" \
    -e "s|grok-pulse@|${CTS_G_NAME}-pulse@|g" \
    -e "s|grok-pulse-http|${CTS_G_NAME}-pulse-http|g" \
    -e "s|grok-desk|${CTS_G_NAME}-desk|g" \
    -e "s|grok-pulse.target|${CTS_G_NAME}-pulse.target|g" \
    -e "s|grok-retention|${CTS_G_NAME}-retention|g" \
    -e "s|:3015|:${PULSE_PORT}|g" \
    -e "s|/usr/bin/python3|${PYTHON_BIN}|g" \
    -e "s|:3102|:${DESK_PORT}|g" \
    -e "s|PORT=3102|PORT=${DESK_PORT}|g" \
    -e "s|CTS-G desk UI|${CTS_G_NAME} desk UI|g" \
    "$src" >"$dest"
}

install_units() {
  local source target name
  mkdir -p "$CTS_G_ROOT/deploy/units"
  for source in grok-pulse@.service grok-pulse-http.service grok-desk.service grok-pulse.target grok-retention.service grok-retention.timer; do
    name="${source/grok-/$CTS_G_NAME-}"
    target="$CTS_G_ROOT/deploy/units/$name"
    render_unit "$CTS_G_ROOT/deploy/$source" "$target"
    # Refuse replacement of another installation's unit definition.
    if [[ -e "$UNIT_DIR/$name" ]] && ! grep -Fq "$CTS_G_ROOT" "$UNIT_DIR/$name" && ! grep -Fq "$CTS_G_NAME-" "$UNIT_DIR/$name"; then
      die "unit ownership mismatch: $name"
    fi
    ln -sfn "$target" "$UNIT_DIR/$name"
  done
  systemctl daemon-reload
  "$PYTHON_BIN" "$CTS_G_ROOT/deploy/instance-manifest.py" write
  ok "scoped units (canonical files under project/deploy/units)"
}

npm_install_desk() {
  local digest
  [[ -f "$CTS_G_ROOT/package-lock.json" ]] || die "package-lock.json required"
  digest="$(sha256sum "$CTS_G_ROOT/package-lock.json" "$CTS_G_ROOT/package.json" | sha256sum | cut -d' ' -f1)"
  if [[ -f "$CTS_G_ROOT/.cts-deps.sha256" && "$(<"$CTS_G_ROOT/.cts-deps.sha256")" == "$digest" ]] && (cd "$CTS_G_ROOT" && npm ls --omit=optional --depth=0 >/dev/null 2>&1); then
    skip "npm lockfile dependencies verified"
  else
    (cd "$CTS_G_ROOT" && npm ci --no-fund --no-audit --no-progress)
    printf '%s\n' "$digest" > "$CTS_G_ROOT/.cts-deps.sha256"
    ok "npm ci"
  fi
}

fast_forward_app() {
  [[ -z "$(git -C "$CTS_G_ROOT" status --porcelain --untracked-files=no)" ]] || die "tracked edits present; preserve and commit/review them first"
  [[ "$(git -C "$CTS_G_ROOT" branch --show-current)" == "$BRANCH" ]] || die "branch differs; no forced checkout"
  git -C "$CTS_G_ROOT" fetch "$GIT_REMOTE" "$BRANCH"
  git -C "$CTS_G_ROOT" merge --ff-only "$GIT_REMOTE/$BRANCH" || die "non-fast-forward update blocked"
}

redis_has_keys() {
  local slot="$1"
  have redis-cli || return 1
  local key sec
  key="$(redis-cli HGET "${CTS_REDIS_PREFIX}connection:$slot" api_key 2>/dev/null || true)"
  sec="$(redis-cli HGET "${CTS_REDIS_PREFIX}connection:$slot" api_secret 2>/dev/null || true)"
  [[ -n "$key" && -n "$sec" && "$key" != "(nil)" && "$sec" != "(nil)" ]]
}

enable_stack() {
  [[ "${NO_START:-0}" != 1 ]] || { skip "enable (--no-start)"; return; }
  systemctl enable "$(pulse_http_unit)" "$(desk_unit)" "$(retention_timer_unit)" >/dev/null
  # An unconfigured instance must not start trading at boot.
  if redis_has_keys "$VST_SLOT"; then systemctl enable "$(pulse_instance_unit "$VST_SLOT")" >/dev/null; fi
  ok "configured units enabled"
}

start_stack() {
  local start_live="${1:-0}"
  systemctl restart "$(pulse_http_unit)"
  systemctl restart "$(desk_unit)"
  systemctl start "$(retention_timer_unit)"
  if redis_has_keys "$VST_SLOT"; then
    systemctl restart "$(pulse_instance_unit "$VST_SLOT")"
  else
    skip "VST engine: no credentials in this instance"
  fi
  if [[ "$start_live" == 1 ]]; then
    systemctl enable "$(pulse_instance_unit "$LIVE_SLOT")" >/dev/null
    systemctl restart "$(pulse_instance_unit "$LIVE_SLOT")"
  elif systemctl is-active --quiet "$(pulse_instance_unit "$LIVE_SLOT")"; then
    # Preserve its PAUSE flag and management of existing positions.
    systemctl restart "$(pulse_instance_unit "$LIVE_SLOT")"
    ok "existing live manager restarted; entry-control flags preserved"
  else
    skip "live engine: no new activation"
  fi
}

enforce_retention() {
  if [[ -x "$CTS_G_ROOT/deploy/retention.sh" ]]; then
    "$CTS_G_ROOT/deploy/retention.sh" --once >/dev/null 2>&1 || warn "retention pass failed"
    ok "runtime retention (last 1000 lines / 8 MiB per file)"
  else
    warn "retention helper missing at $CTS_G_ROOT/deploy/retention.sh"
  fi
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
  printf '  %-36s %s\n' "$(pulse_http_unit)" "$(unit_state "$(pulse_http_unit)")"
  printf '  %-36s %s\n' "$(desk_unit)" "$(unit_state "$(desk_unit)")"
  printf '  %-36s %s\n' "$(pulse_instance_unit "$VST_SLOT")" "$(unit_state "$(pulse_instance_unit "$VST_SLOT")")"
  printf '  %-36s %s\n' "$(pulse_instance_unit "$LIVE_SLOT")" "$(unit_state "$(pulse_instance_unit "$LIVE_SLOT")")"
  printf '  %-36s %s\n' "$(retention_timer_unit)" "$(unit_state "$(retention_timer_unit)")"
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
  echo "  Pulse connection  ${pulse}/connection.json?conn=live"
  echo "  GitHub            https://github.com/mxssnx-creator/CTS-G"
  echo "  Commit            https://github.com/mxssnx-creator/CTS-G/commit/${sha}"
  echo
  if redis_has_keys "$LIVE_SLOT"; then
    echo "Live keys           present in Redis connection:${LIVE_SLOT}"
  else
    echo "Live keys           missing — set then restart:"
    echo "  redis-cli HSET connection:${LIVE_SLOT} api_key '…' api_secret '…'"
    echo "  systemctl restart $(pulse_instance_unit "$LIVE_SLOT")"
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
