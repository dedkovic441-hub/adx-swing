# Zigzag Swing signal bot

Alert-only Python bot that ports the [Zigzag Swing](https://dedkovic441-hub.github.io/zigzag-swing/) v7 ATR-adaptive zigzag **exactly**. It marks confirmed swing bottoms as BUY and swing tops as SELL, then prints them to the console and optionally sends Telegram messages.

It does **not** place, cancel, or manage trades.

The bot must run on a machine that stays online. A laptop that sleeps will miss signals.

## What it does

- Watches Binance USD-M Futures symbols and timeframes you configure (the same market as the live chart)
- Seeds the last 1,000 candles from `https://fapi.binance.com/fapi/v1/klines`, with the chart's `fapi1`–`fapi4` failover. An intermittent CloudFront HTTP 403 is retried on that host with a short backoff before the next host is tried
- Streams combined kline WebSockets from `wss://fstream.binance.com`, with auto-reconnect and REST polling fallback
- On each **closed** candle, recomputes the v7 zigzag (ATR 14, Sensitivity Low/Med/High, percent floor, 3×ATR shock-wick clip, alternation, quality stars)
- Alerts only **new** confirmed signals after startup. Historic signals are logged, never re-alerted. A small `state.json` file remembers what was already sent across restarts

Default watch list: `PUMPUSDT`, `BTCUSDT`, `ETHUSDT` on `5m` and `15m`, sensitivity Medium.

## Requirements

- Python 3.10 or newer
- Outbound HTTPS to Binance USD-M Futures REST (`https://fapi.binance.com/fapi/v1`, plus `fapi1`–`fapi4`) and WebSocket (`wss://fstream.binance.com`)

## Install

From the repository root:

```bash
cd bot
python3 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

`config.yaml` is gitignored. Edit symbols, timeframes, and sensitivity there. Set `top_n_by_volume: 20` (for example) to also watch the top 20 USD-M perpetual USDT pairs by 24h quote volume, in addition to the explicit symbol list.

## Configure Telegram (optional)

Console alerts always work. Telegram is used only when both environment variables are set. **Never commit the token or chat id.**

1. In Telegram, open [@BotFather](https://t.me/BotFather), send `/newbot`, and follow the prompts. BotFather replies with a token that looks like `123456:ABC-DEF...`.
2. Start a chat with your new bot (open it and tap Start / send any message).
3. Get your chat id:
   - Open [@userinfobot](https://t.me/userinfobot) and send it any message, or
   - Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser after messaging your bot, and read `message.chat.id`.
4. Export the values in the same shell (or a gitignored `bot/.env` loaded by systemd):

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
export TELEGRAM_CHAT_ID="123456789"
```

If either variable is missing, the bot stays on console-only mode. A group chat id also works if you add the bot to the group.

## Run

```bash
# from the repo root, with the venv active
python3 bot/signal_bot.py --config bot/config.yaml

# seed history and print last signals, then exit (no live stream)
python3 bot/signal_bot.py --config bot/config.yaml --once

# force console-only even if Telegram env vars are set
python3 bot/signal_bot.py --config bot/config.yaml --no-telegram
```

A confirmed alert looks like:

```
[2026-11-14 16:05:00 Asia/Jakarta]
PUMPUSDT 5m BUY @ 0.00332 (bottom)
pivot 2026-11-14 15:42:00 Asia/Jakarta
confirmed 2026-11-14 15:47:00 Asia/Jakarta  lag 11 bars  ★★☆
https://dedkovic441-hub.github.io/zigzag-swing/?symbol=PUMPUSDT&tf=5m
```

The chart link opens that symbol and timeframe on the live Pages app (v7.1+).

Forming `BUY?` / `SELL?` alerts are off by default (`provisional_alerts: false`).

## Run 24/7

Pick one. The process has to keep running.

**systemd** (Linux VPS). Copy the repo to `/opt/coin-chart`, install dependencies, copy `bot/config.example.yaml` to `bot/config.yaml`, and put the Telegram variables in `bot/.env`:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

```bash
sudo cp bot/coin-chart-bot.service /etc/systemd/system/
# edit WorkingDirectory / ExecStart if the repo is not at /opt/coin-chart
sudo systemctl daemon-reload
sudo systemctl enable --now coin-chart-bot
sudo journalctl -u coin-chart-bot -f
```

**nohup** (quick and dirty):

```bash
cd /opt/coin-chart
source bot/.venv/bin/activate
nohup python3 bot/signal_bot.py --config bot/config.yaml >> bot/bot.log 2>&1 &
```

**Docker** (from this `bot/` directory):

```bash
docker build -t coin-chart-bot .
docker run --name coin-chart-bot --restart unless-stopped \
  -v "$PWD/config.yaml:/app/bot/config.yaml" \
  -v "$PWD/state.json:/app/bot/state.json" \
  -e TELEGRAM_BOT_TOKEN -e TELEGRAM_CHAT_ID \
  coin-chart-bot
```

Free-tier always-on hosts (a small VPS, Oracle Cloud free tier, a home Raspberry Pi, etc.) work the same way: install Python, clone the repo, and run systemd or Docker. A laptop lid-close or a sleeping phone will not.

## Parity test

The Python engine is checked against the JavaScript in `index.html` on the same 1,000-bar USD-M Futures kline windows (PUMPUSDT 5m, ANKRUSDT 1D, BTCUSDT 1h). Pivot time, side, and price must match. Candles come from `https://fapi.binance.com/fapi/v1/klines`. From a region where that host returns HTTP 451, the check uses the public USD-M kline archive on data.binance.vision, which is the published copy of those candles.

```bash
# needs node + Python 3.10+ (network used to fetch klines)
python3 bot/test_parity.py
```

## Supervisor scripts

From the repo root, `run/start.sh`, `run/stop.sh`, and `run/supervisor.sh` start the bot with `--no-telegram` unless those environment variables are already set. Logs and pid files stay under `run/` (gitignored).

## Paper trading and alert logs

Two isolated *paper* (simulated) accounts, no Binance keys or real orders:

- **Confirmed** — `paper_state.json` / `paper_trades.jsonl`. Enters at the confirmation-candle close when a new non-repainting BUY/SELL appears on the active timeframe.
- **Unconfirmed (repaints)** — `paper_state_unconfirmed.json` / `paper_trades_unconfirmed.jsonl`. Enters at the close of the candle when a new forming BUY?/SELL? first appears on the active timeframe. If that signal fades (the forming pivot disappears), the position stays open and a `FADED` / `repainted` event is logged. A later same-direction unconfirmed signal on the active timeframe can add another leg sized to the first leg's notional (configurable `unconfirmed.max_add_positions`, default 4 additional legs, so 5 total). On timeframes in `unconfirmed.dca_every_signal_timeframes` (default `15m`), every new same-direction unconfirmed signal — including a pivot walk that writes a new alert — adds a leg with no fade required. Other timeframes (default `5m`) still need a fade before a same-direction add. Further same-direction signals past the cap are ignored. Adds are skipped if virtual cash is insufficient.

Both accounts hold at most one open position per coin and trade only `paper.active_timeframe` (default `15m`). Signals on other timeframes are still alerted and logged, but do not trade. A LONG is closed only by a SHORT signal, which flips into a new short; a SHORT is closed only by a LONG signal. Fade, timeout, stop, and take-profit do not close a position.

Defaults: $1,000 virtual USDT, $50 notional per first leg, 1x, 0.05% taker fee per side. Historic signals are not back-filled on startup.

Confirmed alerts are appended to `alerts.jsonl` (what the forwarder routine reads). Unconfirmed BUY?/SELL? appearances go only to `alerts_unconfirmed.jsonl` — they are never mixed into `alerts.jsonl`.

## Alert-only

This bot never sends real orders. There is no API key, no wallet, and no live execution path. Paper trading above is simulated only.
