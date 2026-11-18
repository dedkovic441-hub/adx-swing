#!/usr/bin/env bash
# Auto-restart wrapper for the Zigzag Swing alert bot (signal_bot.py).
# Does not start Telegram unless those env vars are already set; we pass --no-telegram.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
mkdir -p "$ROOT/run"
LOG=$ROOT/run/bot.log
PIDF=$ROOT/run/bot.pid
POLLER_LOG=$ROOT/run/settings_poller.log
POLLER_PIDF=$ROOT/run/settings_poller.pid
echo $$ > "$ROOT/run/supervisor.pid"
PYTHON=$ROOT/bot/.venv/bin/python
[ -x "$PYTHON" ] || PYTHON=python3
export PYTHONUNBUFFERED=1
export TZ=Asia/Jakarta
trim() {
  if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 5000000 ]; then
    tail -n 8000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
  fi
  if [ -f "$POLLER_LOG" ] && [ "$(stat -c %s "$POLLER_LOG")" -gt 2000000 ]; then
    tail -n 4000 "$POLLER_LOG" > "$POLLER_LOG.tmp" && mv "$POLLER_LOG.tmp" "$POLLER_LOG"
  fi
}
poller_alive() {
  [ -f "$POLLER_PIDF" ] || return 1
  P=$(cat "$POLLER_PIDF" 2>/dev/null || true)
  [ -n "$P" ] && kill -0 "$P" 2>/dev/null || return 1
  tr '\0' ' ' < "/proc/$P/cmdline" 2>/dev/null | grep -q "settings_poller.py"
}
start_poller() {
  if poller_alive; then
    return 0
  fi
  echo "$(date "+%F %T") WIB [supervisor] starting settings_poller.py" >> "$POLLER_LOG"
  "$PYTHON" -u bot/settings_poller.py >> "$POLLER_LOG" 2>&1 &
  echo $! > "$POLLER_PIDF"
}
KEEPER_PID=""
stop_children() {
  echo "$(date "+%F %T") WIB [supervisor] stopping (pid $$)"
  if [ -n "${KEEPER_PID:-}" ]; then
    kill "$KEEPER_PID" 2>/dev/null || true
  fi
  if [ -f "$PIDF" ]; then
    kill "$(cat "$PIDF")" 2>/dev/null || true
  fi
  if [ -f "$POLLER_PIDF" ]; then
    kill "$(cat "$POLLER_PIDF")" 2>/dev/null || true
  fi
  rm -f "$PIDF" "$POLLER_PIDF" "$ROOT/run/supervisor.pid"
  exit 0
}
trap 'stop_children' TERM INT
# Keep the settings poller running even while we are blocked on the bot.
(
  while true; do
    start_poller
    if [ -f "$POLLER_PIDF" ]; then
      wait "$(cat "$POLLER_PIDF")" 2>/dev/null || true
    fi
    sleep 5
  done
) &
KEEPER_PID=$!
while true; do
  trim
  start_poller
  echo "$(date "+%F %T") WIB [supervisor] starting signal_bot.py" >> "$LOG"
  "$PYTHON" -u bot/signal_bot.py --config bot/config.yaml --no-telegram >> "$LOG" 2>&1 &
  echo $! > "$PIDF"
  wait $! || true
  code=$?
  echo "$(date "+%F %T") WIB [supervisor] bot exited code=$code, restarting in 5s" >> "$LOG"
  rm -f "$PIDF"
  sleep 5
done
