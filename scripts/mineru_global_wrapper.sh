#!/bin/bash
# Global `mineru` front-end. MinerU 4 parse/read/search need its local doclib
# server; this starts it on demand and stops it after idle (default 10 min),
# so no server runs permanently. A server the user started by hand is never
# stopped. Installed as ~/.local/bin/mineru by scripts/install_mineru4.command;
# the real CLI stays in the uv tool environment.
REAL="$HOME/.local/share/uv/tools/mineru/bin/mineru"
STATE="${MINERU_HOME:-$HOME/.mineru}"
STAMP="$STATE/.wrapper-last-use"
OWNED="$STATE/.wrapper-owned"
IDLE="${MINERU_IDLE_STOP_SECONDS:-600}"
mkdir -p "$STATE"

if [ "${1:-}" = "__idle_stop" ]; then
  while sleep 30; do
    [ -f "$OWNED" ] || exit 0
    age=$(( $(date +%s) - $(stat -f %m "$STAMP" 2>/dev/null || echo 0) ))
    if [ "$age" -ge "$IDLE" ] && ! pgrep -qf "tools/mineru/bin/mineru (parse|read|search|scan)"; then
      "$REAL" server stop >/dev/null 2>&1; rm -f "$OWNED"; exit 0
    fi
  done
fi

case "${1:-}" in
  parse|read|search|scan|find|list|show|invalidate|forget) ;;
  *) exec "$REAL" "$@" ;;
esac
cold=0
if ! "$REAL" server status --json 2>/dev/null | grep -q '"running": true'; then
  "$REAL" server start >/dev/null 2>&1 && touch "$OWNED" && cold=1
fi
touch "$STAMP"
if [ -f "$OWNED" ] && ! pgrep -qf "mineru __idle_stop"; then
  nohup "$0" __idle_stop >/dev/null 2>&1 &
fi
if [ "$cold" = 0 ]; then "$REAL" "$@"; code=$?; touch "$STAMP"; exit $code; fi
# After a cold start the managed parse engine needs up to a few minutes.
for _ in $(seq 1 48); do
  out=$("$REAL" "$@" 2>&1); code=$?
  if [ $code = 0 ] || ! printf '%s' "$out" | grep -q "engine available"; then
    [ $code = 0 ] && printf '%s\n' "$out" || printf '%s\n' "$out" >&2
    touch "$STAMP"; exit $code
  fi
  sleep 5
done
printf '%s\n' "$out" >&2; touch "$STAMP"; exit $code
