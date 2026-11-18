#!/usr/bin/env bash
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
for f in "$ROOT/run/supervisor.pid" "$ROOT/run/bot.pid" "$ROOT/run/settings_poller.pid"; do
  [ -f "$f" ] || continue
  P=$(cat "$f" 2>/dev/null)
  if [ -n "$P" ] && kill -0 "$P" 2>/dev/null; then
    kill "$P" 2>/dev/null || true
    echo "stopped pid $P ($f)"
  fi
  rm -f "$f"
done
# Reap leftovers from this checkout only (not other bots).
for P in $(pgrep -f "$ROOT/bot/signal_bot.py" || true); do
  kill "$P" 2>/dev/null || true
  echo "stopped leftover bot pid $P"
done
for P in $(pgrep -f "$ROOT/bot/settings_poller.py" || true); do
  kill "$P" 2>/dev/null || true
  echo "stopped leftover settings poller pid $P"
done
sleep 1
for P in $(pgrep -f "$ROOT/bot/signal_bot.py|$ROOT/bot/settings_poller.py" || true); do
  kill -9 "$P" 2>/dev/null || true
  echo "killed leftover pid $P"
done
echo "Zigzag Swing bot stopped"
