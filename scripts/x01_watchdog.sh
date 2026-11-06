#!/usr/bin/env bash
# Keeps the live x01 engine running between Claude sessions (owner 2026-11-06: "Always keep engine continuously running"). A loop, one instance (pidfile), that
# runs scripts/ensure_x01.sh every WATCHDOG_EVERY seconds; ensure_x01.sh starts the engine only when none runs and touches no setting. A restart storm (three
# starts inside ten minutes: a crash loop) backs off for ten minutes so the venue is not hammered. It dies with the container; scripts/ensure_x01.sh (SessionStart
# hook) and the scheduled wake-up bring it back.
set -u
REPO="${REPO:-/home/user/CTS-G}"
DIR="${PULSE_DIR:-/opt/grok-x01-pulse}"
EVERY="${WATCHDOG_EVERY:-20}"
PIDFILE="$DIR/x01-watchdog.pid"
old="$(cat "$PIDFILE" 2>/dev/null || true)"
if [ -n "$old" ] && [ "$old" != "$$" ] && tr '\0' ' ' < "/proc/$old/cmdline" 2>/dev/null | grep -q "x01_watchdog.sh"; then
  echo "watchdog already running (pid $old)"
  exit 0
fi
echo $$ > "$PIDFILE"
starts=()
while :; do
  out="$(ENSURE_X01_NO_WATCHDOG=1 bash "$REPO/scripts/ensure_x01.sh" 2>&1)"
  case "$out" in
    *"engine started"*)
      now="$(date +%s)"
      echo "$(date -u +%FT%T) watchdog: $out"
      kept=()
      for t in "${starts[@]:-}"; do [ -n "$t" ] && [ $((now - t)) -lt 600 ] && kept+=("$t"); done
      starts=("${kept[@]:-}" "$now")
      if [ "${#starts[@]}" -ge 3 ]; then
        echo "$(date -u +%FT%T) watchdog: three starts in ten minutes, backing off 600 s"
        sleep 600
        starts=()
      fi
      ;;
  esac
  sleep "$EVERY"
done
