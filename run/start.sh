#!/usr/bin/env bash
# Idempotent start of the ADX Swing alert bot supervisor.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
mkdir -p "$ROOT/run"
PIDF=$ROOT/run/supervisor.pid
if [ -f "$PIDF" ]; then
  P=$(cat "$PIDF" 2>/dev/null)
  if [ -n "$P" ] && kill -0 "$P" 2>/dev/null && tr '\0' ' ' < /proc/$P/cmdline | grep -q "adx-swing-bot/run/supervisor.sh\|${ROOT}/run/supervisor.sh"; then
    echo "ADX Swing supervisor already running (pid $P)"
    exit 0
  fi
fi
nohup setsid bash "$ROOT/run/supervisor.sh" >> "$ROOT/run/supervisor.log" 2>&1 < /dev/null &
echo $! > "$PIDF"
sleep 1
echo "started ADX Swing supervisor (pid $(cat $PIDF)), bot log: $ROOT/run/bot.log"
