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
STATE_DIR="${CTS_STATE_DIR:-/var/lib/cts/instances/${CTS_G_NAME}}"
CTS_DATA_DIR="${CTS_DATA_DIR:-$STATE_DIR/data}"
LEGACY_PULSE_DIR="${CTS_LEGACY_PULSE_DIR:-}"
PULSE_DIR="${PULSE_DIR:-/opt/${CTS_G_NAME}-pulse}"
ETC_DIR="${ETC_DIR:-/etc/${CTS_G_NAME}}"
LOG_DIR="${LOG_DIR:-/var/log/${CTS_G_NAME}}"
ENV_FILE="${ENV_FILE:-$ETC_DIR/cts-g.env}"
CREDENTIALS_ENV_FILE="${CREDENTIALS_ENV_FILE:-$ETC_DIR/credentials.env}"
REPO_URL="${REPO_URL:-https://github.com/mxssnx-creator/CTS-G.git}"
BRANCH="${BRANCH:-main}"
GIT_REMOTE="${GIT_REMOTE:-origin}"
GIT_USER_NAME="${GIT_USER_NAME:-xssnet}"
GIT_USER_EMAIL="${GIT_USER_EMAIL:-mxssnx@gmail.com}"
DESK_HOST="${DESK_HOST:-0.0.0.0}"
DESK_PORT="${DESK_PORT:-3102}"
PULSE_PORT="${PULSE_PORT:-3015}"
REDIS_DB="${CTS_REDIS_DB:-1}"
LOG_MAX_LINES="${CTS_LOG_MAX_LINES:-1000}"
LOG_MAX_BYTES="${CTS_LOG_MAX_BYTES:-8388608}"
JOURNAL_MAX_USE="${CTS_JOURNAL_MAX_USE:-256M}"
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
  # Every named installation gets independent code, state, logs and units.
  CTS_G_NAME="${1:-$CTS_G_NAME}"
  [[ "$CTS_G_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || die "invalid instance name"
  CTS_G_ROOT="/opt/${CTS_G_NAME}"
  STATE_DIR="/var/lib/cts/instances/${CTS_G_NAME}"
  CTS_DATA_DIR="$STATE_DIR/data"
  PULSE_DIR="/opt/${CTS_G_NAME}-pulse"
  ETC_DIR="/etc/${CTS_G_NAME}"
  LOG_DIR="/var/log/${CTS_G_NAME}"
  ENV_FILE="$ETC_DIR/cts-g.env"
  CREDENTIALS_ENV_FILE="$ETC_DIR/credentials.env"
}

target_unit() { printf '%s-pulse.target' "$CTS_G_NAME"; }
desk_unit() { printf '%s-desk.service' "$CTS_G_NAME"; }
pulse_http_unit() { printf '%s-pulse-http.service' "$CTS_G_NAME"; }
pulse_template_unit() { printf '%s-pulse@.service' "$CTS_G_NAME"; }
pulse_unit() { printf '%s-pulse@%s.service' "$CTS_G_NAME" "$1"; }

valid_port() { [[ "$1" =~ ^[0-9]+$ ]] && (( $1 >= 1024 && $1 <= 65535 )); }

env_value() {
  local key="$1"
  [[ -f "$ENV_FILE" ]] || return 0
  sed -n "s/^${key}=//p" "$ENV_FILE" | tail -n 1
}

upsert_env() {
  local key="$1" value="$2"
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >>"$ENV_FILE"
  fi
}

load_existing_env_config() {
  local value
  CTS_DATA_DIR="$STATE_DIR/data"
  [[ -f "$ENV_FILE" ]] || return 0
  if [[ "${PORT_EXPLICIT:-0}" != "1" ]]; then
    value="$(env_value PORT)"; if valid_port "$value"; then DESK_PORT="$value"; fi
  fi
  if [[ "${PULSE_PORT_EXPLICIT:-0}" != "1" ]]; then
    value="$(env_value PULSE_PORT)"; if valid_port "$value"; then PULSE_PORT="$value"; fi
  fi
  if [[ "${REDIS_DB_EXPLICIT:-0}" != "1" ]]; then
    value="$(env_value CTS_REDIS_DB)"
    if [[ "$value" =~ ^([0-9]|1[0-5])$ ]]; then REDIS_DB="$value"; fi
  fi
  if [[ "${STATE_EXPLICIT:-0}" != "1" ]]; then
    value="$(env_value CTS_STATE_DIR)"
    if [[ "$value" == /* && "$value" != "/" && "$value" != *".."* ]]; then STATE_DIR="$value"; fi
    CTS_DATA_DIR="$(env_value CTS_DATA_DIR)"
    CTS_DATA_DIR="${CTS_DATA_DIR:-$STATE_DIR/data}"
  else
    LEGACY_PULSE_DIR="$(env_value CTS_DATA_DIR)"
  fi
  value="$(env_value CTS_LOG_DIR)"
  if [[ -n "$value" && "$value" == /* && "$value" != "/" && "$value" != *".."* ]]; then LOG_DIR="$value"; fi
  value="$(env_value PULSE_DIR)"
  if [[ -n "$value" && "$value" == /* && "$value" != "/" && "$value" != *".."* ]]; then PULSE_DIR="$value"; fi
  return 0
}

validate_instance_config() {
  [[ "$CTS_G_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || die "invalid instance name"
  valid_port "$DESK_PORT" || die "desk port must be 1024..65535"
  valid_port "$PULSE_PORT" || die "pulse port must be 1024..65535"
  [[ "$DESK_PORT" != "$PULSE_PORT" ]] || die "desk and pulse ports must differ"
  [[ "$REDIS_DB" =~ ^([1-9]|1[0-5])$ ]] || die "Redis DB must be 1..15; DB 0 is reserved for CTS-K-N"
  local path
  for path in "$STATE_DIR" "$CTS_DATA_DIR" "$PULSE_DIR" "$CTS_G_ROOT" "$LOG_DIR" "$ETC_DIR"; do
    [[ "$path" =~ ^/[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+$ && "$path" != *".."* && "$path" != *"//"* && ! -L "$path" ]] || die "unsafe or symlinked install path"
    case "$path" in /var/lib|/var/log|/var/backups|/usr/local|/etc/systemd) die "broad install path prohibited";; esac
  done
  [[ "$LOG_MAX_LINES" =~ ^[0-9]+$ ]] && (( LOG_MAX_LINES >= 100 && LOG_MAX_LINES <= 1000 )) \
    || die "CTS_LOG_MAX_LINES must be 100..1000"
  [[ "$LOG_MAX_BYTES" =~ ^[0-9]+$ ]] && (( LOG_MAX_BYTES >= 1048576 && LOG_MAX_BYTES <= 67108864 )) \
    || die "CTS_LOG_MAX_BYTES must be 1..64 MiB"
}

lock_and_check_instance() {
  mkdir -p /run/lock
  exec {CTS_DEPLOY_LOCK}>"/run/lock/${CTS_G_NAME}-deploy.lock"
  flock -n "$CTS_DEPLOY_LOCK" || die "another install/update owns this instance"
  local other key value
  for other in /etc/*/cts-g.env; do
    [[ -f "$other" && "$other" != "$ENV_FILE" ]] || continue
    while IFS='=' read -r key value; do
      case "$key" in
        PORT|PULSE_PORT) [[ "$value" != "$DESK_PORT" && "$value" != "$PULSE_PORT" ]] || die "port already reserved by $other";;
        CTS_REDIS_DB) [[ "$value" != "$REDIS_DB" ]] || die "Redis DB already reserved by $other";;
        CTS_STATE_DIR|CTS_DATA_DIR) [[ "$value" != "$STATE_DIR" && "$value" != "$CTS_DATA_DIR" ]] || die "state already reserved by $other";;
      esac
    done <"$other"
  done
  if [[ "$CTS_G_NAME" != "cts-g" && ! -f "$ENV_FILE" && "${REDIS_DB_EXPLICIT:-0}" != "1" ]]; then
    die "new parallel installs require an explicit unused --redis-db"
  fi
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
  cap_redis_memory
  if have redis-cli; then
    redis_cli ping >/dev/null 2>&1 && ok "redis db=${REDIS_DB} ping" || fail "redis ping"
  fi
}

redis_cli() {
  command redis-cli -n "$REDIS_DB" "$@"
}

migrate_legacy_redis_state() {
  [[ "$CTS_G_NAME" == "cts-g" && "$REDIS_DB" != "0" ]] || return 0
  local copied
  copied="$(redis-cli -n 0 --raw EVAL '
    local source = tonumber(ARGV[1])
    local target = tonumber(ARGV[2])
    local keys = {"connection:bingx-x01", "connection:bingx-x02", "settings:connection_settings:bingx-x01", "settings:connection_settings:bingx-x02"}
    local rows = {}
    redis.call("SELECT", source)
    for _, key in ipairs(keys) do
      if redis.call("EXISTS", key) == 1 then
        table.insert(rows, {key, redis.call("DUMP", key), redis.call("PTTL", key)})
      end
    end
    local copied = 0
    redis.call("SELECT", target)
    for _, row in ipairs(rows) do
      if redis.call("EXISTS", row[1]) == 0 then
        local ttl = tonumber(row[3]) or 0
        if ttl < 1 then ttl = 0 end
        redis.call("RESTORE", row[1], ttl, row[2])
        copied = copied + 1
      end
    end
    return copied
  ' 0 0 "$REDIS_DB")" || die "Redis migration failed"
  [[ "$copied" =~ ^[0-9]+$ ]] || die "Redis migration returned an error"
  if (( copied > 0 )); then
    ok "migrated $copied legacy CTS-G Redis records from DB 0 to DB $REDIS_DB"
  else
    skip "legacy Redis migration (target records already present or source empty)"
  fi
}

cap_redis_memory() {
  # Shared Redis is bounded in proportion to host memory. noeviction protects
  # credentials and trading/statistics state; callers receive an explicit
  # write error instead of silent key loss under pressure.
  local f total_kb available_kb used_bytes target_bytes available_pool_bytes max_bytes
  # A CTS-K-N governor already coordinates shared-host memory; never replace
  # its dynamic budget with a second competing policy during CTS-G updates.
  if [[ -f /opt/cts-kn/scripts/redis-memory-governor.mjs ]]; then
    local policy
    policy="$(redis_cli --raw CONFIG GET maxmemory-policy | tail -n 1)"
    [[ "$policy" == "noeviction" ]] || die "shared Redis must use noeviction"
    skip "shared CTS Redis memory governor already installed"
    return 0
  fi
  total_kb="$(awk '/^MemTotal:/ {print $2; exit}' /proc/meminfo 2>/dev/null || echo 0)"
  available_kb="$(awk '/^MemAvailable:/ {print $2; exit}' /proc/meminfo 2>/dev/null || echo 0)"
  used_bytes=0
  if have redis-cli && redis_cli ping >/dev/null 2>&1; then
    used_bytes="$(redis_cli INFO memory 2>/dev/null | sed -n 's/^used_memory:\([0-9]*\).*/\1/p' | head -n 1)"
  fi
  [[ "$total_kb" =~ ^[0-9]+$ ]] || total_kb=0
  [[ "$available_kb" =~ ^[0-9]+$ ]] || available_kb=0
  [[ "$used_bytes" =~ ^[0-9]+$ ]] || used_bytes=0
  target_bytes=$(( total_kb * 1024 / 4 ))
  available_pool_bytes=$(( available_kb * 1024 + used_bytes ))
  max_bytes=$(( available_pool_bytes * 3 / 5 ))
  (( max_bytes > target_bytes )) && max_bytes="$target_bytes"
  (( max_bytes > 4294967296 )) && max_bytes=4294967296
  (( max_bytes < 536870912 )) && max_bytes=536870912
  if (( used_bytes > 0 && max_bytes < used_bytes + used_bytes / 4 )); then
    max_bytes=$(( used_bytes + used_bytes / 4 ))
    (( max_bytes > 4294967296 )) && max_bytes=4294967296
  fi

  for f in /etc/redis/redis.conf /etc/redis.conf; do
    [[ -f "$f" ]] || continue
    grep -q '^maxmemory ' "$f" || printf 'maxmemory %s\n' "$max_bytes" >> "$f"
    sed -i "s/^maxmemory .*/maxmemory ${max_bytes}/" "$f"
    if grep -q '^maxmemory-policy ' "$f"; then
      sed -i 's/^maxmemory-policy .*/maxmemory-policy noeviction/' "$f"
    else
      echo 'maxmemory-policy noeviction' >> "$f"
    fi
  done
  if have redis-cli && redis_cli ping >/dev/null 2>&1; then
    [[ "$(redis_cli --raw CONFIG SET maxmemory "$max_bytes")" == "OK" ]] || die "Redis memory cap rejected"
    [[ "$(redis_cli --raw CONFIG SET maxmemory-policy noeviction)" == "OK" ]] || die "Redis noeviction rejected"
    redis_cli MEMORY PURGE >/dev/null 2>&1 || true
    redis_cli CONFIG REWRITE >/dev/null 2>&1 || true
    ok "redis maxmemory=$max_bytes noeviction (dynamic host headroom)"
  fi
}

ensure_dirs() {
  mkdir -p "$CTS_G_ROOT" "$PULSE_DIR" "$ETC_DIR" "$LOG_DIR" "$CTS_DATA_DIR" "$STATE_DIR/db"
  chmod 755 "$CTS_G_ROOT" "$PULSE_DIR" "$ETC_DIR"
  chmod 750 "$LOG_DIR" "$STATE_DIR" "$CTS_DATA_DIR" "$STATE_DIR/db"
  ok "dirs code=$CTS_G_ROOT pulse=$PULSE_DIR state=$STATE_DIR logs=$LOG_DIR"
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
CTS_SYSTEMD_PREFIX=${CTS_G_NAME}
CTS_STATE_DIR=${STATE_DIR}
CTS_DATA_DIR=${CTS_DATA_DIR}
CTS_DB_DIR=${STATE_DIR}/db
CTS_LOG_DIR=${LOG_DIR}
PULSE_DIR=${PULSE_DIR}
PULSE_PORT=${PULSE_PORT}
CTS_REDIS_DB=${REDIS_DB}
CTS_LOG_MAX_LINES=${LOG_MAX_LINES}
CTS_LOG_MAX_BYTES=${LOG_MAX_BYTES}
CTS_JOURNAL_MAX_USE=${JOURNAL_MAX_USE}
EOF
  chmod 0600 "$ENV_FILE"
}

seed_env() {
  mkdir -p "$ETC_DIR"
  if [[ ! -f "$ENV_FILE" ]]; then
    write_env_file
    ok "env $ENV_FILE (desk :$DESK_PORT)"
  else
    # Preserve every custom/secret line while reconciling canonical routing.
    upsert_env PULSE_URL "http://127.0.0.1:${PULSE_PORT}"
    upsert_env HOST "$DESK_HOST"
    upsert_env PORT "$DESK_PORT"
    upsert_env CTS_G_NAME "$CTS_G_NAME"
    upsert_env CTS_G_ROOT "$CTS_G_ROOT"
    upsert_env CTS_SYSTEMD_PREFIX "$CTS_G_NAME"
    upsert_env CTS_STATE_DIR "$STATE_DIR"
    upsert_env CTS_DATA_DIR "$CTS_DATA_DIR"
    upsert_env CTS_DB_DIR "$STATE_DIR/db"
    upsert_env CTS_LOG_DIR "$LOG_DIR"
    upsert_env PULSE_DIR "$PULSE_DIR"
    upsert_env PULSE_PORT "$PULSE_PORT"
    upsert_env CTS_REDIS_DB "$REDIS_DB"
    upsert_env CTS_LOG_MAX_LINES "$LOG_MAX_LINES"
    upsert_env CTS_LOG_MAX_BYTES "$LOG_MAX_BYTES"
    upsert_env CTS_JOURNAL_MAX_USE "$JOURNAL_MAX_USE"
    chmod 0600 "$ENV_FILE"
    ok "env reconciled without replacing custom values"
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
  # Prefer the in-repo engine when it is the real file (not the git PLACEHOLDER).
  # Fallback rebuilds from the pinned base blob + restore patches.
  local pt="$PULSE_DIR/pulse_trader.py"
  local src="$CTS_G_ROOT/server/pulse/pulse_trader.py"
  local pt_want="5319e02ae28b6cfb2f2661aed07da1bbec1c0c8d"
  local pt_base="b3a9ff3c60c72864ac5558f488d7e6991bb31d76"
  local pt_patch_sha="aa6cf593268181c0b938bc632f5eb12957091709"
  local pt_patch_url="https://raw.githubusercontent.com/mxssnx-creator/CTS-G/f76f042374efd17a7f2eb61247c56c0d0de021ec/restore/pulse_trader.py.patch"
  local pt_pf_patch="$CTS_G_ROOT/restore/pulse_trader_pf.patch"
  local pt_pf_sha="37ae494b324929d7e139c94d87beeacbfd8a8e6e"
  if [[ -f "$src" ]] && ! grep -q '^PLACEHOLDER' "$src" && [[ "$(wc -l < "$src" | tr -d ' ')" -gt 200 ]]; then
    cp -a "$src" "$pt"
    ok "pulse_trader.py from in-repo engine $(git hash-object "$pt" 2>/dev/null || echo local)"
    return 0
  fi
  if [[ "$(git hash-object "$pt" 2>/dev/null || true)" == "$pt_want" ]]; then
    skip "pulse_trader.py already current"
    return 0
  fi
  [[ -f "$pt_pf_patch" ]] || die "restore PF patch missing: $pt_pf_patch"
  [[ "$(git hash-object "$pt_pf_patch")" == "$pt_pf_sha" ]] || die "restore PF patch hash mismatch"
  local scratch
  scratch="$(mktemp -d)"
  mkdir -p "$scratch/server/pulse"
  if curl -fsSL -m 90 -o "$scratch/server/pulse/pulse_trader.py" \
      "https://raw.githubusercontent.com/mxssnx-creator/CTS-G/2b3432d7b3/server/pulse/pulse_trader.py" \
    && [[ "$(git hash-object "$scratch/server/pulse/pulse_trader.py")" == "$pt_base" ]] \
    && curl -fsSL -m 90 -o "$scratch/main.patch" "$pt_patch_url" \
    && [[ "$(git hash-object "$scratch/main.patch")" == "$pt_patch_sha" ]] \
    && git -C "$scratch" apply "$scratch/main.patch" \
    && git -C "$scratch" apply "$pt_pf_patch" \
    && [[ "$(git hash-object "$scratch/server/pulse/pulse_trader.py")" == "$pt_want" ]]; then
    cp -a "$scratch/server/pulse/pulse_trader.py" "$pt"
    rm -rf "$scratch"
    ok "pulse_trader.py restored from verified patch"
    return 0
  fi
  rm -rf "$scratch"
  die "pulse_trader.py restore failed (base/patch/result hash mismatch)"
}

migrate_overlay_rungs() {
  # Overlay rsync is excluded so live 0-rung (unbounded pyramid) files survive
  # updates. Clamp 0 → Block 3 / DCA distance-list so volume cannot balloon.
  # Also reset a leftover volume_ratio that was stored as max_stack (n=1 then
  # adds 3× parent instead of 1×).
  python3 - "$CTS_DATA_DIR" <<'PY'
import json, os, sys
root = sys.argv[1]
for name in ("overlay-bingx-x01.json", "overlay-bingx-x02.json"):
    path = os.path.join(root, name)
    if not os.path.isfile(path):
        continue
    with open(path) as f:
        ov = json.load(f)
    dirty = False
    try:
        dca_n = int(ov.get("dcaMaxSteps") or 0)
    except Exception:
        dca_n = 0
    if dca_n <= 0:
        dist = ov.get("dcaStepDistancesPct") or [0.5, 1, 1.5, 2]
        ov["dcaMaxSteps"] = max(len(dist), 4)
        dirty = True
    try:
        stack = int(ov.get("blockMaxStack") or 0)
    except Exception:
        stack = 0
    if stack <= 0:
        ov["blockMaxStack"] = 3
        stack = 3
        dirty = True
    try:
        vr = float(ov.get("blockVolumeRatio") if ov.get("blockVolumeRatio") is not None else 1)
    except Exception:
        vr = 1.0
    # Stack leaked into ratio (vr==stack>=2) → n=1 adds stack× parent.
    if vr >= 2.0 and int(round(vr)) == int(stack or 0):
        ov["blockVolumeRatio"] = 1
        dirty = True
    if not dirty:
        continue
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ov, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    print(
        "migrated", name,
        "dcaMaxSteps", ov.get("dcaMaxSteps"),
        "blockMaxStack", ov.get("blockMaxStack"),
        "blockVolumeRatio", ov.get("blockVolumeRatio"),
    )
PY
}

sync_pulse_tree() {
  local src="$CTS_G_ROOT/server/pulse"
  [[ -d "$src" ]] || die "pulse tree missing at $src"
  mkdir -p "$PULSE_DIR"
  migrate_legacy_state
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

  # The source sync intentionally excludes generated Python caches.  Remove
  # stale caches already present in the install tree so a restart cannot load
  # bytecode from an older source revision.
  find "$PULSE_DIR" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
  find "$PULSE_DIR" -type d -name __pycache__ -empty -delete

  local f
  for f in "overlay-${LIVE_SLOT}.json" "overlay-${VST_SLOT}.json" universe.json; do
    if [[ ! -f "$PULSE_DIR/$f" && -f "$src/$f" ]]; then
      cp -a "$src/$f" "$PULSE_DIR/$f"
    fi
    if [[ ! -f "$CTS_DATA_DIR/$f" && -f "$src/$f" ]]; then
      cp -a "$src/$f" "$CTS_DATA_DIR/$f"
    fi
  done
  [[ -f "$src/universe.json" ]] && cp -a "$src/universe.json" "$PULSE_DIR/universe.json"
  restore_pulse_trader
  migrate_overlay_rungs
  python3 -m py_compile "$PULSE_DIR"/pulse_trader.py "$PULSE_DIR"/pulse_http.py \
    "$PULSE_DIR"/block_engine.py "$PULSE_DIR"/dca_engine.py "$PULSE_DIR"/set_engine.py
  ok "pulse tree $PULSE_DIR"
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
    overlay-bingx-x02.json STOP STOP-bingx-x01 STOP-bingx-x02
    PAUSE-bingx-x01 PAUSE-bingx-x02 RUN-bingx-x01 RUN-bingx-x02
  )
  local pattern
  for pattern in stats-*.json trades-*.jsonl block-state-*.json open-*.json pending-*.json \
    cts-settings-*.json lev-set-*.json start-eq-*.json \
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
      [[ -f "$src" && ! -L "$src" ]] || continue
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
  rsync -a \
    --exclude 'node_modules/' \
    --exclude 'dist/' \
    --exclude '.tanstack/' \
    --exclude '.nitro/' \
    --exclude '.vercel/' \
    --exclude '.output/' \
    --exclude 'artifacts/' \
    --exclude 'attachments/' \
    --exclude 'screenshots/' \
    --exclude '.preview.pid' \
    --exclude 'preview.log' \
    --exclude '__pycache__/' \
    "$from/" "$CTS_G_ROOT/"
  # A source sync can carry a newer HEAD/ref but leave an older index in an
  # existing install.  Refresh only the index so staged state cannot mask the
  # deployed worktree; mixed reset never discards file contents.
  if [[ -d "$CTS_G_ROOT/.git" ]]; then
    git -C "$CTS_G_ROOT" reset --mixed HEAD >/dev/null 2>&1 || true
  fi
  find "$CTS_G_ROOT/deploy" -maxdepth 1 -type f -name '*.sh' ! -name 'linux-common.sh' -exec chmod 755 {} + 2>/dev/null || true
  ok "app tree $CTS_G_ROOT"
}

create_verified_backup() {
  local backup_root backup stamp manifest dir keep=0
  backup_root="/var/backups/cts/${CTS_G_NAME}"
  [[ "$backup_root" == /var/backups/cts/* && "$backup_root" != /var/backups/cts/ ]] \
    || die "unsafe backup root"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup="$backup_root/$stamp"
  [[ ! -e "$backup" ]] || backup="$backup_root/${stamp}-$$"
  mkdir -p "$backup"
  chmod 0700 "$backup_root" "$backup"

  if [[ -d "$STATE_DIR" ]]; then
    mkdir -p "$backup/state"
    rsync -a --exclude 'logs/' --exclude 'data/logs/' --exclude '*.log' \
      --exclude '*.out' --exclude '*.err' --exclude 'errors-*.jsonl' \
      "$STATE_DIR/" "$backup/state/"
  fi
  if [[ -f "$ENV_FILE" ]]; then
    install -m 0600 "$ENV_FILE" "$backup/cts-g.env"
  fi
  if [[ -f "$CREDENTIALS_ENV_FILE" ]]; then
    install -m 0600 "$CREDENTIALS_ENV_FILE" "$backup/credentials.env"
  fi
  if [[ "$CTS_DATA_DIR" != "$STATE_DIR" && "$CTS_DATA_DIR" != "$STATE_DIR/"* && -d "$CTS_DATA_DIR" ]]; then
    rsync -a --exclude 'logs/' --exclude '*.log' "$CTS_DATA_DIR/" "$backup/data/"
  fi
  if [[ -d "$CTS_G_ROOT/.v0-data" && ! -L "$CTS_G_ROOT/.v0-data" ]]; then
    rsync -a "$CTS_G_ROOT/.v0-data/" "$backup/legacy-desk-data/"
  fi
  if [[ -n "$LEGACY_PULSE_DIR" && -d "$LEGACY_PULSE_DIR" ]]; then
    rsync -a --exclude '*.log' --exclude '*.py' --exclude '__pycache__/' "$LEGACY_PULSE_DIR/" "$backup/previous-data/"
  fi
  # Capture recognized legacy state before the first migration without copying
  # replaceable Python code or diagnostics.
  if [[ "$CTS_G_NAME" == "cts-g" && -d /opt/grok-x01-pulse ]]; then
    mkdir -p "$backup/legacy-state"
    rsync -a --include='*/' --include='*.json' --include='trades-*.jsonl' \
      --include='history-*.jsonl' --exclude='*' \
      /opt/grok-x01-pulse/ "$backup/legacy-state/"
  fi
  if [[ -d "$CTS_G_ROOT/.git" ]]; then
    git -C "$CTS_G_ROOT" bundle create "$backup/source.bundle" --all >/dev/null 2>&1 || die "source backup failed"
    git -C "$CTS_G_ROOT" bundle verify "$backup/source.bundle" >/dev/null 2>&1 || die "source backup verification failed"
    git -C "$CTS_G_ROOT" diff HEAD --binary >"$backup/worktree.patch"
  fi
  if have timeout && have redis-cli && redis_cli ping >/dev/null 2>&1; then
    timeout 90 redis-cli --rdb "$backup/redis-all-dbs.rdb" >/dev/null 2>&1 || die "Redis backup failed"
    [[ -s "$backup/redis-all-dbs.rdb" ]] || die "Redis backup empty"
    [[ ! -f "$backup/redis-all-dbs.rdb" ]] || chmod 0600 "$backup/redis-all-dbs.rdb"
  fi

  manifest="$backup/SHA256SUMS"
  (
    cd "$backup"
    find . -type f ! -name SHA256SUMS ! -name VERIFIED -print0 \
      | sort -z | xargs -0 -r sha256sum >SHA256SUMS
    [[ -s SHA256SUMS ]]
    sha256sum -c SHA256SUMS >/dev/null
  ) || die "backup verification failed: $backup"
  install -m 0600 /dev/null "$backup/VERIFIED"
  ok "verified pre-change backup $backup"

  # Retain the newest three checksum-verified generations. Unknown or partial
  # directories are deliberately left for operator inspection.
  while IFS= read -r dir; do
    [[ -n "$dir" && "$dir" == "$backup_root"/* && ! -L "$dir" && -f "$dir/VERIFIED" && -s "$dir/SHA256SUMS" ]] || continue
    (cd "$dir" && sha256sum -c SHA256SUMS >/dev/null 2>&1) || continue
    keep=$((keep + 1))
    (( keep <= 3 )) && continue
    rm -rf -- "$dir"
  done < <(find "$backup_root" -mindepth 1 -maxdepth 1 -type d -name '20*T*Z*' -printf '%p\n' | sort -r)
}

render_unit() {
  local src="$1" dest="$2"
  sed \
    -e "s|/opt/cts-g|${CTS_G_ROOT}|g" \
    -e "s|/opt/grok-x01-pulse|${PULSE_DIR}|g" \
    -e "s|/etc/cts-g|${ETC_DIR}|g" \
    -e "s|/var/log/cts-g|${LOG_DIR}|g" \
    -e "s|:3102|:${DESK_PORT}|g" \
    -e "s|PORT=3102|PORT=${DESK_PORT}|g" \
    -e "s|:3015|:${PULSE_PORT}|g" \
    -e "s|grok-pulse-%i|${CTS_G_NAME}-pulse-%i|g" \
    -e "s|grok-pulse@|${CTS_G_NAME}-pulse@|g" \
    -e "s|grok-pulse-http|${CTS_G_NAME}-pulse-http|g" \
    -e "s|grok-desk|${CTS_G_NAME}-desk|g" \
    -e "s|grok-pulse.target|${CTS_G_NAME}-pulse.target|g" \
    -e "s|CTS-G desk UI|${CTS_G_NAME} desk UI|g" \
    -e "s|CTS-G pulse|${CTS_G_NAME} pulse|g" \
    "$src" >"$dest"
}

install_units() {
  render_unit "$CTS_G_ROOT/deploy/grok-pulse@.service" "/etc/systemd/system/$(pulse_template_unit)"
  render_unit "$CTS_G_ROOT/deploy/grok-pulse-http.service" "/etc/systemd/system/$(pulse_http_unit)"
  render_unit "$CTS_G_ROOT/deploy/grok-desk.service" "/etc/systemd/system/$(desk_unit)"
  render_unit "$CTS_G_ROOT/deploy/grok-pulse.target" "/etc/systemd/system/$(target_unit)"
  systemctl daemon-reload
  ok "independent systemd units prefix=${CTS_G_NAME}"
}

quiesce_legacy_stack() {
  [[ "$CTS_G_NAME" == "cts-g" ]] || return 0
  local unit
  for unit in grok-desk.service grok-pulse-http.service \
    "grok-pulse@${LIVE_SLOT}.service" "grok-pulse@${VST_SLOT}.service" grok-pulse.target; do
    local definition
    definition="$(systemctl cat "$unit" 2>/dev/null || true)"
    [[ "$definition" == *"/etc/cts-g/cts-g.env"* || "$definition" == *"WorkingDirectory=/opt/grok-x01-pulse"* ]] || continue
    systemctl stop "$unit" >/dev/null 2>&1 || true
    if systemctl is-active --quiet "$unit" 2>/dev/null; then
      systemctl kill --kill-who=all --signal=SIGKILL "$unit" >/dev/null 2>&1 || true
      systemctl stop "$unit" >/dev/null 2>&1 || true
    fi
    systemctl disable "$unit" >/dev/null 2>&1 || true
  done
  ok "legacy grok units quiesced before replacement"
}

quiesce_instance() {
  local unit slot
  for slot in "$VST_SLOT" "$LIVE_SLOT"; do
    unit="$(pulse_unit "$slot")"
    if systemctl is-active --quiet "$unit"; then
      touch "$CTS_DATA_DIR/RUN-$slot"
    fi
  done
  for unit in "$(desk_unit)" "$(pulse_http_unit)" "$(pulse_unit "$VST_SLOT")" "$(pulse_unit "$LIVE_SLOT")" "$(target_unit)"; do
    systemctl stop "$unit" >/dev/null 2>&1 || true
    if systemctl is-active --quiet "$unit"; then
      die "unable to stop owned unit $unit; refusing to replace running code"
    fi
  done
  quiesce_legacy_stack
}

configure_host_log_retention() {
  local script="$CTS_G_ROOT/deploy/limit-runtime-logs.sh"
  [[ -f "$script" ]] || die "missing log retention script"
  install -m 0750 "$script" /usr/local/sbin/cts-log-retention
  install -d -m 0755 /etc/systemd/journald.conf.d
  cat >/etc/default/cts-log-retention <<EOF
CTS_LOG_MAX_LINES=${LOG_MAX_LINES}
CTS_LOG_MAX_BYTES=${LOG_MAX_BYTES}
CTS_JOURNAL_MAX_USE=${JOURNAL_MAX_USE}
EOF
  chmod 0644 /etc/default/cts-log-retention
  cat >/etc/systemd/journald.conf.d/cts-retention.conf <<EOF
[Journal]
SystemMaxUse=${JOURNAL_MAX_USE}
RuntimeMaxUse=64M
SystemMaxFileSize=32M
RuntimeMaxFileSize=16M
SystemKeepFree=1G
MaxRetentionSec=7day
MaxFileSec=1day
RateLimitIntervalSec=30s
RateLimitBurst=1000
EOF
  cat >/etc/systemd/system/cts-log-retention.service <<'EOF'
[Unit]
Description=Bound CTS and supporting host text logs

[Service]
Type=oneshot
EnvironmentFile=-/etc/default/cts-log-retention
ExecStart=/usr/local/sbin/cts-log-retention
Nice=10
IOSchedulingClass=idle
NoNewPrivileges=true
PrivateTmp=true
MemoryMax=128M
LogRateLimitIntervalSec=300s
LogRateLimitBurst=30
EOF
  cat >/etc/systemd/system/cts-log-retention.timer <<'EOF'
[Unit]
Description=Run CTS log retention every five minutes

[Timer]
OnActiveSec=2min
OnUnitActiveSec=5min
AccuracySec=30s
Persistent=true
Unit=cts-log-retention.service

[Install]
WantedBy=timers.target
EOF
  rm -f /etc/cron.d/cts-log-retention
  systemctl daemon-reload
  systemctl restart systemd-journald
  systemctl enable --now cts-log-retention.timer
  systemctl start cts-log-retention.service
  ok "logs capped at ${LOG_MAX_LINES} lines/${LOG_MAX_BYTES} bytes; journal=${JOURNAL_MAX_USE}"
}

npm_install_desk() {
  [[ -f "$CTS_G_ROOT/package.json" ]] || die "package.json missing in $CTS_G_ROOT"
  local oldpwd="$PWD"
  cd "$CTS_G_ROOT"
  local fingerprint
  fingerprint="$(sha256sum package.json package-lock.json 2>/dev/null | sha256sum | cut -d' ' -f1)"
  if [[ -d node_modules/vite && -f node_modules/.cts-install-sha && "$(<node_modules/.cts-install-sha)" == "$fingerprint" ]]; then
    skip "npm (node_modules current)"
    cd "$oldpwd"
    return 0
  fi
  if [[ -f package-lock.json ]]; then
    npm ci --no-fund --no-audit --no-progress
  else
    npm install --no-fund --no-audit --no-progress
  fi
  printf '%s\n' "$fingerprint" >node_modules/.cts-install-sha
  cd "$oldpwd"
  ok "npm install"
}

npm_build_desk() {
  local oldpwd="$PWD"
  cd "$CTS_G_ROOT"
  npm run build
  [[ -f "$CTS_G_ROOT/.output/server/index.mjs" ]] \
    || die "node-server production build is incomplete"
  # Remove obsolete provider-specific output left by pre-node-server builds.
  if [[ -d "$CTS_G_ROOT/.vercel" && ! -L "$CTS_G_ROOT/.vercel" ]]; then
    find "$CTS_G_ROOT/.vercel" -depth -delete
  fi
  cd "$oldpwd"
  ok "provider-independent node-server production build"
}

redis_has_keys() {
  local slot="$1"
  have redis-cli || return 1
  local key sec
  key="$(redis_cli HGET "connection:$slot" api_key 2>/dev/null || true)"
  sec="$(redis_cli HGET "connection:$slot" api_secret 2>/dev/null || true)"
  [[ -n "$key" && -n "$sec" && "$key" != "(nil)" && "$sec" != "(nil)" ]]
}

enable_stack() {
  systemctl enable "$(target_unit)" "$(pulse_http_unit)" "$(desk_unit)" >/dev/null 2>&1 || true
  # Engine restarts are coordinated by durable RUN/STOP intent, not boot links.
  systemctl disable "$(pulse_unit "$VST_SLOT")" "$(pulse_unit "$LIVE_SLOT")" >/dev/null 2>&1 || true
  ok "units enabled"
}

start_stack() {
  local start_live="${1:-0}"
  if [[ "$start_live" != "1" ]]; then
    touch "$CTS_DATA_DIR/STOP-$LIVE_SLOT"
  fi
  if [[ ! -e "$CTS_DATA_DIR/STOP" && ! -e "$CTS_DATA_DIR/STOP-$VST_SLOT" ]]; then
    touch "$CTS_DATA_DIR/RUN-$VST_SLOT"
  fi
  systemctl restart "$(pulse_http_unit)" || fail "start $(pulse_http_unit)"
  systemctl restart "$(desk_unit)" || fail "start $(desk_unit)"
  if [[ -e "$CTS_DATA_DIR/RUN-$VST_SLOT" && ! -e "$CTS_DATA_DIR/STOP" && ! -e "$CTS_DATA_DIR/STOP-$VST_SLOT" ]]; then
    systemctl restart "$(pulse_unit "$VST_SLOT")" || fail "start vst"
  else
    skip "VST engine (durable operator-stop preserved)"
  fi
  if [[ "$start_live" == "1" ]]; then
    systemctl restart "$(pulse_unit "$LIVE_SLOT")" || fail "start live"
  else
    systemctl stop "$(pulse_unit "$LIVE_SLOT")" >/dev/null 2>&1 || true
    skip "live engine (requires explicit --start-live)"
  fi
  systemctl start "$(target_unit)" >/dev/null 2>&1 || true
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
  pulse="http://${host}:${DESK_PORT}"
  repo="$REPO_URL"
  sha="$(git_head)"
  echo
  echo "======== ${CTS_G_NAME} install results ========"
  echo "name        $CTS_G_NAME"
  echo "root        $CTS_G_ROOT"
  echo "pulse dir   $PULSE_DIR"
  echo "state       $STATE_DIR"
  echo "logs        $LOG_DIR (last ${LOG_MAX_LINES} lines, max ${LOG_MAX_BYTES} bytes/file)"
  echo "env         $ENV_FILE"
  echo "redis db    $REDIS_DB"
  echo "git         $repo"
  echo "branch      $BRANCH"
  echo "sha         $sha"
  echo "user        $GIT_USER_NAME <$GIT_USER_EMAIL>"
  echo "desk bind   ${DESK_HOST}:${DESK_PORT}"
  echo
  echo "units"
  printf '  %-36s %s\n' "$(pulse_http_unit)" "$(unit_state "$(pulse_http_unit)")"
  printf '  %-36s %s\n' "$(desk_unit)" "$(unit_state "$(desk_unit)")"
  printf '  %-36s %s\n' "$(pulse_unit "$VST_SLOT")" "$(unit_state "$(pulse_unit "$VST_SLOT")")"
  printf '  %-36s %s\n' "$(pulse_unit "$LIVE_SLOT")" "$(unit_state "$(pulse_unit "$LIVE_SLOT")")"
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
    echo "Live keys           present in Redis DB ${REDIS_DB} connection:${LIVE_SLOT}"
  else
    echo "Live keys           missing — set then restart:"
    echo "  redis-cli -n ${REDIS_DB} HSET connection:${LIVE_SLOT} api_key '…' api_secret '…'"
    echo "  systemctl restart $(pulse_unit "$LIVE_SLOT")"
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
