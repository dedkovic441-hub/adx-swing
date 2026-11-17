#!/usr/bin/env bash
# Auto-restart wrapper for the ADX Swing alert bot (signal_bot.py).
# Does not start Telegram unless those env vars are already set; we pass --no-telegram.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
mkdir -p "$ROOT/run"
LOG=$ROOT/run/bot.log
PIDF=$ROOT/run/bot.pid
echo $$ > "$ROOT/run/supervisor.pid"
PYTHON=$ROOT/bot/.venv/bin/python
[ -x "$PYTHON" ] || PYTHON=python3
export PYTHONUNBUFFERED=1
export TZ=Asia/Jakarta
trim() {
  if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 5000000 ]; then
    tail -n 8000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
  fi
}
trap 'echo "$(date "+%F %T") WIB [supervisor] stopping (pid $$)"; [ -f "$PIDF" ] && kill "$(cat "$PIDF")" 2>/dev/null; rm -f "$PIDF" "$ROOT/run/supervisor.pid"; exit 0' TERM INT
while true; do
  trim
  echo "$(date "+%F %T") WIB [supervisor] starting signal_bot.py" >> "$LOG"
  "$PYTHON" -u bot/signal_bot.py --config bot/config.yaml --no-telegram >> "$LOG" 2>&1 &
  echo $! > "$PIDF"
  wait $! || true
  code=$?
  echo "$(date "+%F %T") WIB [supervisor] bot exited code=$code, restarting in 5s" >> "$LOG"
  rm -f "$PIDF"
  sleep 5
done
