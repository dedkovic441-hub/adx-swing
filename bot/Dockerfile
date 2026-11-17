# USD-M Futures alert bot.
# Needs outbound HTTPS to https://fapi.binance.com/fapi/v1 (fapi1–fapi4 failover)
# and WebSocket to wss://fstream.binance.com.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt /app/bot/requirements.txt
RUN pip install --no-cache-dir -r /app/bot/requirements.txt

COPY . /app/bot/
# Mount your config and state at runtime:
#   docker run --rm -v $PWD/config.yaml:/app/bot/config.yaml \
#     -v $PWD/state.json:/app/bot/state.json \
#     -e TELEGRAM_BOT_TOKEN -e TELEGRAM_CHAT_ID \
#     coin-chart-bot
WORKDIR /app
CMD ["python3", "/app/bot/signal_bot.py", "--config", "/app/bot/config.yaml"]
