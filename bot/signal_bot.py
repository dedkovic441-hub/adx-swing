#!/usr/bin/env python3
"""Alert-only Binance USD-M Futures bot for coin-chart v7 ATR-adaptive swing BUY/SELL signals."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests
import yaml

BOT_DIR = Path(__file__).resolve().parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from swing import INTERVAL_MS, detect_swing_signals  # noqa: E402
import paper_engine  # noqa: E402

JAKARTA = ZoneInfo("Asia/Jakarta")
CHART_DEFAULT = "https://dedkovic441-hub.github.io/adx-swing/"
ALERTS_JSONL = Path("/workspace/adx-swing-bot/alerts.jsonl")
ALERTS_UNCONFIRMED_JSONL = Path("/workspace/adx-swing-bot/alerts_unconfirmed.jsonl")

# Same USD-M Futures hosts as index.html. Public market data only.
REST_BASES = [
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
    "https://fapi3.binance.com",
    "https://fapi4.binance.com",
]
REST_PREFIX = "/fapi/v1"
REST_TIMEOUT = 4
REST_TIMEOUT_BULKY = 8
# fapi.binance.com sits behind CloudFront and sometimes answers HTTP 403.
# A reused keep-alive session can stay stuck on 403; retry quickly with a
# fresh connection, then fail over. Do not serial-sleep 5 times per symbol.
REST_403_ATTEMPTS = 2
REST_403_BACKOFF_SEC = 0.0
WS_HOSTS = [
    "wss://fstream.binance.com",
]
WS_CONNECT_TIMEOUT = 5
# Client pings through this box's egress proxy often time out and drop an
# otherwise connected socket. Rely on Binance's server pings (every ~20s,
# 60s dead-timeout) and an application-level kline watchdog instead.
WS_PING_INTERVAL = None
WS_PING_TIMEOUT = None
WS_RECV_WATCHDOG_SEC = 45
WS_STALE_RECONNECT_SEC = 45
CATCHUP_WORKERS = 8
CATCHUP_TRIES = 4
CATCHUP_AFTER_CLOSE_SEC = 0.8

log = logging.getLogger("signal_bot")


def now_jakarta() -> str:
    from datetime import datetime
    return datetime.now(JAKARTA).strftime("%Y-%m-%d %H:%M:%S Asia/Jakarta")


def fmt_jakarta(sec: int | float) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(int(sec), tz=JAKARTA).strftime("%Y-%m-%d %H:%M:%S Asia/Jakarta")


def fmt_price(n: float) -> str:
    if not isinstance(n, (int, float)):
        return "—"
    a = abs(n)
    if a >= 1000:
        s = f"{n:.2f}"
    elif a >= 1:
        s = f"{n:.4f}"
    elif a >= 0.01:
        s = f"{n:.6f}"
    else:
        s = f"{n:.8f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def star_text(n: int) -> str:
    n = max(0, min(3, int(n or 0)))
    return "".join("★" if i < n else "☆" for i in range(3))


def signal_key(symbol: str, tf: str, sig: dict, provisional: bool = False) -> str:
    if provisional or sig.get("provisional"):
        return f"{symbol}:{tf}:prov:{sig.get('time')}:{sig.get('price')}:{sig.get('id')}"
    return f"{symbol}:{tf}:{sig.get('time')}:{sig.get('id')}"


def combined_stream_url(host: str, streams: list[str]) -> str:
    return host.rstrip("/") + "/stream?streams=" + "/".join(streams)


def cloudfront_403_backoff(attempt: int) -> float:
    """Short delay before repeating a CloudFront HTTP 403. attempt is 0-based."""
    return REST_403_BACKOFF_SEC * (attempt + 1)


def _response_text(response, limit: int = 240) -> str:
    try:
        return (response.text or "")[:limit]
    except Exception:
        return ""


def _is_restricted(response) -> bool:
    if getattr(response, "status_code", None) == 451:
        return True
    return "restricted location" in _response_text(response).lower()


def _is_cloudfront_403(response) -> bool:
    if getattr(response, "status_code", None) != 403:
        return False
    server = ""
    headers = getattr(response, "headers", None) or {}
    try:
        server = str(headers.get("Server") or headers.get("server") or "")
    except Exception:
        server = ""
    blob = (server + " " + _response_text(response, 500)).lower()
    return "cloudfront" in blob or "request could not be satisfied" in blob


def chart_link(base: str, symbol: str, tf: str) -> str:
    base = (base or CHART_DEFAULT).rstrip("/") + "/"
    return f"{base}?symbol={symbol}&tf={tf}"


def format_alert(symbol: str, tf: str, sig: dict, chart: str) -> str:
    kind = sig.get("swingKind") or ("bottom" if sig.get("dir") == "bull" else "top")
    name = sig.get("name") or ("BUY" if sig.get("dir") == "bull" else "SELL")
    lines = [
        f"{symbol} {tf} {name} @ {fmt_price(sig['price'])} ({kind})",
        f"pivot {fmt_jakarta(sig['time'])}",
    ]
    if not sig.get("provisional"):
        lines.append(
            f"confirmed {fmt_jakarta(sig['confirmTime'])}  lag {sig.get('lag')} bar"
            f"{'' if sig.get('lag') == 1 else 's'}  {star_text(sig.get('stars') or 0)}"
        )
        reasons = [r for r in (sig.get("reasons") or []) if not str(r).startswith("Lag ")]
        if reasons:
            lines.append(" · ".join(reasons))
    else:
        lines.append("developing — not confirmed")
    lines.append(chart_link(chart, symbol, tf))
    return "\n".join(lines)


# --- config / state ---------------------------------------------------------

DEFAULTS = {
    "symbols": ["PUMPUSDT", "BTCUSDT", "ETHUSDT"],
    "timeframes": ["5m", "15m"],
    "sensitivity": "medium",
    "provisional_alerts": False,
    "top_n_by_volume": 0,
    "state_file": "state.json",
    "chart_url": CHART_DEFAULT,
    "poll_interval_sec": 1,
}


def load_config(path: Optional[Path]) -> dict:
    cfg = dict(DEFAULTS)
    if path and path.exists():
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) if path.suffix.lower() in {".yaml", ".yml"} else json.loads(text)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if v is not None})
    symbols = [str(s).replace("/", "").upper() for s in (cfg.get("symbols") or [])]
    tfs = []
    for tf in cfg.get("timeframes") or []:
        t = str(tf).lower()
        if t in INTERVAL_MS:
            tfs.append(t)
    if not symbols:
        symbols = list(DEFAULTS["symbols"])
    if not tfs:
        tfs = list(DEFAULTS["timeframes"])
    sens = str(cfg.get("sensitivity") or "medium").lower()
    if sens not in {"low", "medium", "high"}:
        sens = "medium"
    state_file = Path(str(cfg.get("state_file") or "state.json"))
    if not state_file.is_absolute():
        state_file = (path.parent if path else Path.cwd()) / state_file
    cfg.update({
        "symbols": symbols,
        "timeframes": tfs,
        "sensitivity": sens,
        "provisional_alerts": bool(cfg.get("provisional_alerts")),
        "top_n_by_volume": int(cfg.get("top_n_by_volume") or 0),
        "state_file": state_file,
        "chart_url": str(cfg.get("chart_url") or CHART_DEFAULT),
        "poll_interval_sec": float(cfg.get("poll_interval_sec") or 1),
        "telegram_token": os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN") or "",
        "telegram_chat": os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT") or "",
        "unconfirmed_dca_every_signal_timeframes": paper_engine.load_unconfirmed_dca_timeframes(path),
        "unconfirmed_max_add_positions": paper_engine.load_unconfirmed_max_add(path),
    })
    return cfg


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.seen: dict[str, dict] = {}
        self.rest_base: Optional[str] = None
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.seen = dict(data.get("seen") or {})
                self.rest_base = data.get("rest_base")
        except Exception as e:
            log.warning("Could not read state file %s: %s", self.path, e)
            self.seen = {}

    def save(self) -> None:
        payload = {
            "seen": self.seen,
            "rest_base": self.rest_base,
            "updated": now_jakarta(),
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def mark(self, key: str, meta: Optional[dict] = None) -> None:
        self.seen[key] = meta or {"at": now_jakarta()}

    def has(self, key: str) -> bool:
        return key in self.seen


# --- Binance REST -----------------------------------------------------------

class RestClient:
    def __init__(self, remembered: Optional[str] = None):
        self.bases = list(REST_BASES)
        if remembered and remembered in self.bases:
            self.bases.remove(remembered)
            self.bases.insert(0, remembered)
        self.current: Optional[str] = remembered if remembered in REST_BASES else None
        self.saw_restricted = False
        self._sleep = time.sleep
        self._new_session()

    def _new_session(self) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "coin-chart-signal-bot/7.1"
        self.session.headers["Accept"] = "application/json"
        # CloudFront 403s on this box often stick to a reused keep-alive socket.
        self.session.headers["Connection"] = "close"

    def _drop_connections(self) -> None:
        """Close pooled sockets without replacing self.session (tests patch session.get)."""
        try:
            for adapter in list(getattr(self.session, "adapters", {}).values()):
                closer = getattr(adapter, "close", None)
                if closer:
                    closer()
        except Exception:
            pass

    def _urllib_get(self, url: str, timeout: float):
        import urllib.request
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "coin-chart-signal-bot/7.1", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def get(self, path: str, timeout: Optional[float] = None, bulky: bool = False) -> Any:
        timeout = timeout or (REST_TIMEOUT_BULKY if bulky else REST_TIMEOUT)
        order = list(self.bases)
        if self.current and self.current in order:
            order.remove(self.current)
            order.insert(0, self.current)
        # Prefer the primary host; fapi1-4 often return 202/302 from this box.
        if "https://fapi.binance.com" in order:
            order.remove("https://fapi.binance.com")
            order.insert(0, "https://fapi.binance.com")
        last_err: Optional[Exception] = None
        for base in order:
            url = base + REST_PREFIX + path
            for attempt in range(REST_403_ATTEMPTS):
                try:
                    r = self.session.get(url, timeout=timeout, allow_redirects=False)
                except Exception as e:
                    last_err = e
                    log.debug("REST %s error: %s", url, e)
                    self._new_session()
                    self._sleep(0.5)
                    continue
                if r.status_code in (301, 302, 303, 202) and base != "https://fapi.binance.com":
                    last_err = RuntimeError(f"{base} HTTP {r.status_code}")
                    break
                if r.status_code == 403 and attempt + 1 < REST_403_ATTEMPTS:
                    delay = cloudfront_403_backoff(attempt)
                    why = "CloudFront 403" if _is_cloudfront_403(r) else "HTTP 403"
                    last_err = RuntimeError(f"{base} {why}")
                    log.warning("REST %s %s; retrying%s (%s/%s)", url, why, f" in {delay:.2f}s" if delay else " with a new connection", attempt + 1, REST_403_ATTEMPTS)
                    self._drop_connections()
                    if delay:
                        self._sleep(delay)
                    continue
                if r.status_code != 200:
                    if _is_restricted(r):
                        self.saw_restricted = True
                    detail = _response_text(r).replace("\n", " ").strip()
                    last_err = RuntimeError(f"{base} HTTP {r.status_code} {detail}".strip())
                    log.debug("REST %s failed: %s", url, last_err)
                    break
                try:
                    payload = r.json()
                except Exception as e:
                    last_err = e
                    break
                self.current = base
                return payload
        # Fresh-client fallback on the primary host (a sticky Session can stay 403).
        url = "https://fapi.binance.com" + REST_PREFIX + path
        try:
            payload = self._urllib_get(url, timeout)
            self.current = "https://fapi.binance.com"
            return payload
        except Exception as e:
            last_err = e
            log.warning("REST urllib fallback failed for %s: %s", path, e)
        raise RuntimeError(f"All REST bases failed for {path}: {last_err}")

    def klines(self, symbol: str, tf: str, limit: int = 1000) -> list[dict]:
        raw = self.get(f"/klines?symbol={symbol}&interval={tf}&limit={limit}", bulky=True)
        return _parse_kline_rows(raw)

    def top_usdt(self, n: int) -> list[str]:
        # Same universe as the chart coin list: TRADING USDT perpetuals.
        info = self.get("/exchangeInfo", bulky=True)
        allowed = {
            s["symbol"]
            for s in (info.get("symbols") or [])
            if s.get("status") == "TRADING"
            and s.get("quoteAsset") == "USDT"
            and s.get("contractType") == "PERPETUAL"
        }
        ticks = self.get("/ticker/24hr", bulky=True)
        rows = []
        for t in ticks or []:
            sym = t.get("symbol")
            if sym not in allowed:
                continue
            try:
                qv = float(t.get("quoteVolume") or 0)
            except (TypeError, ValueError):
                qv = 0.0
            rows.append((qv, sym))
        rows.sort(reverse=True)
        return [sym for _, sym in rows[:n]]


def _parse_kline_rows(raw) -> list[dict]:
    out = []
    for k in raw or []:
        out.append({
            "time": int(k[0]) // 1000,
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    return out


def last_closed_open_time(tf: str, now: Optional[float] = None) -> int:
    """Unix seconds open time of the most recently closed bar for `tf`."""
    interval = INTERVAL_MS.get(tf, 60_000) / 1000.0
    now = time.time() if now is None else now
    current_open = int(now // interval) * int(interval)
    return current_open - int(interval)


def bar_close_lag_sec(open_s: int, tf: str, now: Optional[float] = None) -> float:
    interval = INTERVAL_MS.get(tf, 60_000) / 1000.0
    now = time.time() if now is None else now
    return now - (int(open_s) + interval)


def urllib_klines(symbol: str, tf: str, limit: int = 3, tries: int = CATCHUP_TRIES) -> list[dict]:
    """Thread-safe kline fetch that does not share the bot's requests.Session.

    Uses Connection: close and immediate retries so an intermittent CloudFront
    403 does not stall other markets. www.binance.com is a last-resort host
    and is not in REST_BASES (those stay official fapi* URLs).
    """
    path = f"/klines?symbol={symbol}&interval={tf}&limit={limit}"
    bases = list(REST_BASES[:1]) + ["https://www.binance.com"]
    last_err: Optional[Exception] = None
    for attempt in range(max(1, int(tries))):
        for base in bases:
            url = base + REST_PREFIX + path
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "coin-chart-signal-bot/7.1",
                    "Accept": "application/json",
                    "Connection": "close",
                },
            )
            try:
                timeout = REST_TIMEOUT_BULKY if limit > 10 else REST_TIMEOUT
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = json.loads(r.read().decode())
                return _parse_kline_rows(raw)
            except Exception as e:
                last_err = e
                msg = str(e)
                if "403" in msg or "Forbidden" in msg:
                    log.warning("REST %s HTTP 403; retrying with a new connection (%s/%s)", url, attempt + 1, tries)
                    continue
                if any(code in msg for code in ("202", "302", "301")):
                    break
        # Immediate retry — do not sleep a backlog into other symbols.
    raise RuntimeError(f"urllib klines failed for {symbol} {tf}: {last_err}")


def _jsonl_key(rec: dict) -> str:
    return f"{rec.get('symbol')}|{rec.get('timeframe')}|{rec.get('side')}|{rec.get('candle_close_utc')}"


def _load_jsonl_keys() -> set[str]:
    keys: set[str] = set()
    try:
        if ALERTS_JSONL.exists():
            for line in ALERTS_JSONL.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    keys.add(_jsonl_key(json.loads(line)))
                except Exception:
                    pass
    except OSError:
        pass
    return keys


_JSONL_KEYS: set[str] | None = None


def append_alert_jsonl(symbol: str, tf: str, sig: dict) -> None:
    """Append one deduped confirmed BUY/SELL line for the forwarder routine."""
    global _JSONL_KEYS
    if sig.get("provisional"):
        return
    side = sig.get("name") or ("BUY" if sig.get("dir") == "bull" else "SELL")
    if side not in {"BUY", "SELL"}:
        return
    open_s = int(sig.get("confirmTime") or sig.get("time") or 0)
    close_dt = datetime.fromtimestamp(open_s, tz=timezone.utc) + timedelta(milliseconds=INTERVAL_MS.get(tf, 60_000))
    rec = {
        "symbol": symbol,
        "timeframe": tf,
        "side": side,
        "price": sig.get("price"),
        "candle_close_utc": close_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "detected_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if _JSONL_KEYS is None:
        _JSONL_KEYS = _load_jsonl_keys()
    key = _jsonl_key(rec)
    if key in _JSONL_KEYS:
        return
    ALERTS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERTS_JSONL, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    _JSONL_KEYS.add(key)
    log.info("alerts.jsonl %s %s %s", symbol, tf, side)


def last_closed_bar(mkt: "Market"):
    candles = mkt.candles or []
    return candles[-1] if candles else None


def last_closed_close(mkt: "Market"):
    bar = last_closed_bar(mkt)
    try:
        return float(bar["close"]) if bar is not None else None
    except (TypeError, ValueError, KeyError):
        return None


def _prov_side(sig: dict | None) -> str | None:
    if not sig:
        return None
    name = str(sig.get("name") or "")
    if name.startswith("BUY"):
        return "BUY"
    if name.startswith("SELL"):
        return "SELL"
    return None


def _prov_snapshot(sig: dict | None) -> dict | None:
    if not sig:
        return None
    side = _prov_side(sig)
    if not side:
        return None
    return {
        "name": sig.get("name") or (side + "?"),
        "side": side,
        "price": sig.get("price"),
        "time": sig.get("time"),
        "id": sig.get("id"),
        "swingKind": sig.get("swingKind"),
    }


def _is_new_unconfirmed_pivot(prev: dict | None, curr: dict | None) -> bool:
    return paper_engine.is_new_unconfirmed_pivot(prev, curr)


def _confirmed_same_pivot(pack: dict, prev: dict | None) -> bool:
    """True if the previous unconfirmed pivot is now a confirmed signal (not a repaint)."""
    if not prev:
        return False
    want_time = prev.get("time")
    want_side = prev.get("side")
    for s in pack.get("signals") or []:
        if want_time is not None and s.get("time") != want_time:
            continue
        if _prov_side(s) == want_side:
            return True
    return False


_UNCONF_JSONL_KEYS: set[str] | None = None


def _load_unconf_jsonl_keys() -> set[str]:
    keys: set[str] = set()
    try:
        if ALERTS_UNCONFIRMED_JSONL.exists():
            for line in ALERTS_UNCONFIRMED_JSONL.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    keys.add(_jsonl_key(json.loads(line)))
                except Exception:
                    pass
    except OSError:
        pass
    return keys


def append_unconfirmed_alert_jsonl(symbol: str, tf: str, sig: dict, bar: dict | None) -> None:
    """Append one deduped unconfirmed BUY?/SELL? line. Never writes alerts.jsonl."""
    global _UNCONF_JSONL_KEYS
    side = sig.get("name") or ((_prov_side(sig) or "") + "?")
    if side not in {"BUY?", "SELL?"}:
        base = _prov_side(sig)
        if base in {"BUY", "SELL"}:
            side = base + "?"
        else:
            return
    if bar is not None:
        open_s = int(bar.get("time") or 0)
        close_dt = datetime.fromtimestamp(open_s, tz=timezone.utc) + timedelta(milliseconds=INTERVAL_MS.get(tf, 60_000))
        price = bar.get("close", sig.get("price"))
    else:
        open_s = int(sig.get("time") or 0)
        close_dt = datetime.fromtimestamp(open_s, tz=timezone.utc) + timedelta(milliseconds=INTERVAL_MS.get(tf, 60_000))
        price = sig.get("price")
    rec = {
        "symbol": symbol,
        "timeframe": tf,
        "side": side,
        "price": price,
        "pivot_price": sig.get("price"),
        "pivot_time": sig.get("time"),
        "candle_close_utc": close_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "detected_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if _UNCONF_JSONL_KEYS is None:
        _UNCONF_JSONL_KEYS = _load_unconf_jsonl_keys()
    key = _jsonl_key(rec)
    if key in _UNCONF_JSONL_KEYS:
        return
    ALERTS_UNCONFIRMED_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERTS_UNCONFIRMED_JSONL, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    _UNCONF_JSONL_KEYS.add(key)
    log.info("unconfirmed %s %s %s", symbol, tf, side)
    stamp = now_jakarta()
    kind = sig.get("swingKind") or ("bottom" if side.startswith("BUY") else "top")
    print(f"[{stamp}] unconfirmed {symbol} {tf} {side} @ {fmt_price(sig.get('price'))} ({kind})", flush=True)


# --- alerts -----------------------------------------------------------------

class Alerter:
    def __init__(self, token: str, chat: str, chart_url: str):
        self.token = (token or "").strip()
        self.chat = (chat or "").strip()
        self.chart_url = chart_url
        self.enabled = bool(self.token and self.chat)

    def emit(self, symbol: str, tf: str, sig: dict, prefix: str = "") -> None:
        body = format_alert(symbol, tf, sig, self.chart_url)
        stamp = now_jakarta()
        text = f"[{stamp}] {prefix}{body}" if prefix else f"[{stamp}]\n{body}"
        print(text, flush=True)
        log.info("alert %s %s %s", symbol, tf, sig.get("name"))
        try:
            append_alert_jsonl(symbol, tf, sig)
        except Exception as e:
            log.error("alerts.jsonl write failed: %s", e)
        if self.enabled:
            self._telegram(text)

    def _telegram(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            r = requests.post(
                url,
                json={"chat_id": self.chat, "text": text, "disable_web_page_preview": False},
                timeout=10,
            )
            if r.status_code != 200:
                log.error("Telegram send failed: HTTP %s %s", r.status_code, r.text[:200])
        except Exception as e:
            log.error("Telegram send failed: %s", e)


# --- market engine ----------------------------------------------------------

class Market:
    def __init__(self, symbol: str, tf: str, sensitivity: str, swing_early: bool):
        self.symbol = symbol
        self.tf = tf
        self.sensitivity = sensitivity
        self.swing_early = swing_early
        self.candles: list[dict] = []
        self.pack: dict = {"signals": [], "provisional": None}
        self._last_closed_key: Optional[tuple] = None

    def seed(self, candles: list[dict]) -> dict:
        self.candles = candles
        self.pack = detect_swing_signals(self.candles, self.tf, self.sensitivity, self.swing_early)
        interval_ms = INTERVAL_MS.get(self.tf, 60_000)
        now_ms = time.time() * 1000
        last_closed = None
        for c in reversed(self.candles):
            if int(c["time"]) * 1000 + interval_ms <= now_ms + 500:
                last_closed = c
                break
        if last_closed:
            self._last_closed_key = (int(last_closed["time"]), float(last_closed["close"]))
        else:
            self._last_closed_key = None
        return self.pack

    def upsert(self, bar: dict, closed: bool) -> Optional[dict]:
        rows = self.candles
        if not rows or bar["time"] > rows[-1]["time"]:
            rows.append(bar)
        elif bar["time"] == rows[-1]["time"]:
            rows[-1] = bar
        else:
            return None
        if len(rows) > 1500:
            del rows[: len(rows) - 1200]
        if not closed:
            return None
        key = (int(bar["time"]), float(bar["close"]))
        if key == self._last_closed_key:
            return None
        self._last_closed_key = key
        self.pack = detect_swing_signals(self.candles, self.tf, self.sensitivity, self.swing_early)
        return self.pack


# --- bot --------------------------------------------------------------------

def confirmation_close(mkt: "Market", sig: dict):
    """Close of the confirmation candle; fall back to the last closed bar."""
    want = int(sig.get("confirmTime") or 0)
    close = None
    for c in mkt.candles or []:
        if int(c.get("time") or 0) == want:
            close = float(c["close"])
            break
    if close is None and mkt.candles:
        close = float(mkt.candles[-1]["close"])
    return close


class SignalBot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.state = StateStore(cfg["state_file"])
        self.rest = RestClient(self.state.rest_base)
        self.alerter = Alerter(cfg["telegram_token"], cfg["telegram_chat"], cfg["chart_url"])
        self.markets: dict[tuple[str, str], Market] = {}
        self.ws_task: Optional[asyncio.Task] = None
        self.poll_task: Optional[asyncio.Task] = None
        self.stop = asyncio.Event()
        self.polling = False
        self.ws_ok = False
        self._started = time.time()
        self._streams: list[str] = []
        self._last_prov: dict[tuple[str, str], dict | None] = {}
        self._ws_kline_at = 0.0
        self._ws_msgs = 0
        self._ws_disconnects = 0
        self._catchup_pool = ThreadPoolExecutor(max_workers=CATCHUP_WORKERS, thread_name_prefix="adx-rest")

    def watch_pairs(self) -> list[tuple[str, str]]:
        return sorted(self.markets.keys())

    def ensure_market(self, symbol: str, tf: str) -> Market:
        key = (symbol, tf)
        if key not in self.markets:
            self.markets[key] = Market(symbol, tf, self.cfg["sensitivity"], True)
        return self.markets[key]

    def refresh_universe(self) -> None:
        symbols = list(self.cfg["symbols"])
        n = self.cfg["top_n_by_volume"]
        if n > 0:
            try:
                top = self.rest.top_usdt(n)
                for s in top:
                    if s not in symbols:
                        symbols.append(s)
                log.info("Top %s USD-M perpetuals by volume added; watching %s symbols", n, len(symbols))
            except Exception as e:
                log.error("top-N volume fetch failed: %s", e)
        for sym in symbols:
            for tf in self.cfg["timeframes"]:
                self.ensure_market(sym, tf)
        self.state.rest_base = self.rest.current
        self.state.save()

    def seed_all(self) -> None:
        items = list(self.markets.items())

        def fetch_one(sym_tf):
            (sym, tf), _mkt = sym_tf
            last_err = None
            for attempt in range(4):
                try:
                    return sym, tf, urllib_klines(sym, tf, 1000, tries=4), None
                except Exception as e:
                    last_err = e
                    log.warning("seed retry %s %s (%s/4): %s", sym, tf, attempt + 1, e)
            return sym, tf, None, last_err

        fetched: dict[tuple[str, str], list] = {}
        with ThreadPoolExecutor(max_workers=CATCHUP_WORKERS, thread_name_prefix="adx-seed") as pool:
            for sym, tf, candles, err in pool.map(fetch_one, items):
                if err is not None or candles is None:
                    log.error("seed failed %s %s: %s", sym, tf, err)
                    continue
                fetched[(sym, tf)] = candles

        for (sym, tf), mkt in items:
            if (sym, tf) not in fetched:
                continue
            try:
                candles = fetched[(sym, tf)]
                pack = mkt.seed(candles)
                sigs = pack["signals"]
                last = sigs[-1] if sigs else None
                if last:
                    log.info(
                        "seeded %s %s: %s bars, %s signals, last %s @ %s (lag %s)",
                        sym, tf, len(candles), len(sigs), last["name"],
                        fmt_price(last["price"]), last.get("lag"),
                    )
                else:
                    log.info("seeded %s %s: %s bars, 0 signals", sym, tf, len(candles))
                for s in sigs:
                    self.state.mark(signal_key(sym, tf, s), {"name": s["name"], "price": s["price"], "time": s["time"]})
                if pack.get("provisional"):
                    self.state.mark(signal_key(sym, tf, pack["provisional"], True))
                self._last_prov[(sym, tf)] = _prov_snapshot(pack.get("provisional"))
            except Exception as e:
                log.error("seed failed %s %s: %s", sym, tf, e)
        self.state.rest_base = self.rest.current
        self.state.save()
        # Log the overall last confirmed (most recent confirmTime) without alerting.
        newest = None
        for (sym, tf), mkt in self.markets.items():
            for s in mkt.pack.get("signals") or []:
                if newest is None or s.get("confirmTime", 0) > newest[2].get("confirmTime", 0):
                    newest = (sym, tf, s)
        if newest:
            sym, tf, s = newest
            log.info(
                "startup last signal (not alerted): %s %s %s @ %s  %s",
                sym, tf, s["name"], fmt_price(s["price"]), fmt_jakarta(s["time"]),
            )

    def on_pack(self, mkt: Market, pack: dict, from_close: bool) -> None:
        if not pack or not from_close:
            return
        dirty = False
        for s in pack.get("signals") or []:
            key = signal_key(mkt.symbol, mkt.tf, s)
            if self.state.has(key):
                continue
            self.alerter.emit(mkt.symbol, mkt.tf, s)
            self.state.mark(key, {"name": s["name"], "price": s["price"], "time": s["time"]})
            dirty = True
            try:
                fill = confirmation_close(mkt, s)
                if fill is not None:
                    paper_engine.on_signal(mkt.symbol, mkt.tf, s.get("name") or "", fill)
            except Exception as e:
                log.error("paper trade failed %s %s: %s", mkt.symbol, mkt.tf, e)
        self._handle_unconfirmed(mkt, pack)
        if self.cfg["provisional_alerts"] and pack.get("provisional"):
            p = pack["provisional"]
            key = signal_key(mkt.symbol, mkt.tf, p, True)
            if not self.state.has(key):
                self.alerter.emit(mkt.symbol, mkt.tf, p, prefix="(forming) ")
                self.state.mark(key)
                dirty = True
        if dirty:
            self.state.save()

    def _handle_unconfirmed(self, mkt: Market, pack: dict) -> None:
        """Paper + alerts_unconfirmed.jsonl for forming BUY?/SELL? on closed candles.

        Historic provisionals are recorded at seed and not traded. A later
        confirmation of the same pivot is not treated as a repaint. A fade
        (forming pivot disappears) keeps the unconfirmed paper position open.
        On timeframes in unconfirmed.dca_every_signal_timeframes (default 15m),
        every new same-direction unconfirmed pivot — including a pivot walk —
        writes a new alert and may add a leg with no fade required. Other
        timeframes still need a fade before a same-direction add.
        """
        key = (mkt.symbol, mkt.tf)
        prev = self._last_prov.get(key)
        curr = _prov_snapshot(pack.get("provisional"))
        fill = last_closed_close(mkt)
        bar = last_closed_bar(mkt)
        prev_side = prev.get("side") if prev else None
        curr_side = curr.get("side") if curr else None
        try:
            if curr and prev_side != curr_side:
                if fill is not None:
                    paper_engine.on_unconfirmed_signal(mkt.symbol, mkt.tf, curr["name"], fill)
                append_unconfirmed_alert_jsonl(mkt.symbol, mkt.tf, pack.get("provisional") or curr, bar)
            elif prev and not curr:
                if _confirmed_same_pivot(pack, prev):
                    log.info(
                        "unconfirmed %s %s %s confirmed — keeping paper position",
                        mkt.symbol, mkt.tf, prev.get("name") or prev_side,
                    )
                elif fill is not None:
                    paper_engine.close_unconfirmed_repainted(mkt.symbol, mkt.tf, fill)
            elif curr and prev and prev_side == curr_side and _is_new_unconfirmed_pivot(prev, curr):
                dca_tfs = {
                    str(t).lower()
                    for t in (self.cfg.get("unconfirmed_dca_every_signal_timeframes") or [])
                }
                if "unconfirmed_dca_every_signal_timeframes" not in self.cfg:
                    dca_tfs = {"15m"}
                if mkt.tf in dca_tfs:
                    # 15m (or configured) DCA: new same-direction pivot writes a
                    # new alert and can add a leg without a fade.
                    if fill is not None:
                        paper_engine.on_unconfirmed_signal(mkt.symbol, mkt.tf, curr["name"], fill)
                    append_unconfirmed_alert_jsonl(mkt.symbol, mkt.tf, pack.get("provisional") or curr, bar)
            # 5m / non-DCA same-side walk with no fade: keep the open position, do not re-alert
        except Exception as e:
            log.error("unconfirmed paper/alert failed %s %s: %s", mkt.symbol, mkt.tf, e)
        self._last_prov[key] = curr

    def handle_kline(self, symbol: str, tf: str, k: dict) -> None:
        mkt = self.markets.get((symbol, tf))
        if not mkt:
            return
        bar = {
            "time": int(k["t"]) // 1000,
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"]),
        }
        closed = bool(k.get("x"))
        if closed:
            lag = bar_close_lag_sec(bar["time"], tf)
            log.info(
                "closed candle %s %s %s close=%s lag=%.1fs source=ws",
                symbol, tf, fmt_jakarta(bar["time"]), fmt_price(bar["close"]), lag,
            )
        pack = mkt.upsert(bar, closed)
        if pack:
            self.on_pack(mkt, pack, True)

    def _apply_closed_bars(self, mkt: Market, candles: list[dict], source: str) -> None:
        """Apply REST catch-up bars in order, filling any gap since the last seed."""
        if not candles:
            return
        interval_ms = INTERVAL_MS.get(mkt.tf, 60_000)
        now_ms = time.time() * 1000
        have = int(mkt._last_closed_key[0]) if mkt._last_closed_key else 0
        for bar in candles:
            if bar["time"] < have:
                continue
            closed = bar["time"] * 1000 + interval_ms <= now_ms + 500
            is_new_closed = closed and bar["time"] > have
            if is_new_closed:
                lag = bar_close_lag_sec(bar["time"], mkt.tf)
                log.info(
                    "closed candle %s %s %s close=%s lag=%.1fs source=%s",
                    mkt.symbol, mkt.tf, fmt_jakarta(bar["time"]), fmt_price(bar["close"]), lag, source,
                )
            pack = mkt.upsert(bar, closed)
            if pack:
                self.on_pack(mkt, pack, True)

    async def connect_ws_loop(self) -> None:
        # Wait until markets exist, then run one self-healing socket per
        # timeframe so a stall on 5m does not drop 15m (and vice versa).
        while not self.stop.is_set() and not self.watch_pairs():
            await asyncio.sleep(1)
        if self.stop.is_set():
            return
        host = WS_HOSTS[0]
        by_tf: dict[str, list[str]] = {}
        for (sym, tf) in self.watch_pairs():
            by_tf.setdefault(tf, []).append(f"{sym.lower()}@kline_{tf}")
        groups = [g for g in by_tf.values() if g] or [
            [f"{sym.lower()}@kline_{tf}" for (sym, tf) in self.watch_pairs()]
        ]
        log.info("WebSocket connecting %s (%s streams in %s sockets)", host, sum(len(g) for g in groups), len(groups))
        tasks = [asyncio.create_task(self._ws_group_loop(host, group, gi)) for gi, group in enumerate(groups)]
        await self.stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _ws_group_loop(self, host: str, streams: list[str], group_i: int) -> None:
        delay = 1.0
        while not self.stop.is_set():
            try:
                await self._ws_one(host, streams, group_i)
                delay = 1.0
            except Exception as e:
                self._ws_disconnects += 1
                if time.time() - self._ws_kline_at > 5:
                    self.ws_ok = False
                log.warning("WebSocket error on %s group=%s: %s", host, group_i, e)
            if self.stop.is_set():
                return
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=min(delay, 5))
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 1.5, 5)

    async def _ws_one(self, host: str, streams: list[str], group_i: int) -> None:
        import websockets
        url = combined_stream_url(host, streams)
        async with websockets.connect(
            url,
            open_timeout=WS_CONNECT_TIMEOUT,
            ping_interval=WS_PING_INTERVAL,
            ping_timeout=WS_PING_TIMEOUT,
            close_timeout=3,
            max_queue=2048,
        ) as ws:
            delay_ok = 1.0
            log.info("WebSocket live on %s group=%s streams=%s", host, group_i, len(streams))
            saw_kline = False
            connected_at = time.time()
            while not self.stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=15)
                except asyncio.TimeoutError:
                    # A quiet interval can happen. Reconnect only once the
                    # socket has gone stale (no kline for WS_STALE_RECONNECT_SEC).
                    last = self._ws_kline_at if saw_kline else connected_at
                    if time.time() - last > WS_STALE_RECONNECT_SEC:
                        raise RuntimeError(
                            f"no kline frames for {int(time.time() - last)}s on group {group_i}"
                        )
                    continue
                data = json.loads(raw)
                payload = data.get("data") if isinstance(data, dict) else None
                if not payload or payload.get("e") != "kline":
                    if isinstance(data, dict) and "k" in data:
                        payload = data
                    else:
                        continue
                k = payload.get("k") or {}
                sym = payload.get("s") or k.get("s")
                tf = k.get("i")
                if not (sym and tf):
                    continue
                self._ws_msgs += 1
                self._ws_kline_at = time.time()
                if not saw_kline:
                    saw_kline = True
                    self.ws_ok = True
                    log.info("WebSocket first kline on group %s (%s %s)", group_i, sym, tf)
                self.handle_kline(sym, tf, k)

    async def poll_loop(self) -> None:
        """REST catch-up for closed bars the WebSocket missed or never sent.

        On this box the futures socket often completes the handshake but
        delivers no kline frames, so REST must still catch closes — but it
        stays off the critical path of markets that are already up to date,
        and fetches only the lagging ones in parallel.
        """
        while not self.stop.is_set():
            now = time.time()
            due: list[tuple[str, str]] = []
            for (sym, tf), mkt in list(self.markets.items()):
                if self.stop.is_set():
                    break
                interval = INTERVAL_MS.get(tf, 60_000) / 1000.0
                last_open = last_closed_open_time(tf, now)
                if now < last_open + interval + CATCHUP_AFTER_CLOSE_SEC:
                    continue
                have_closed = int(mkt._last_closed_key[0]) if mkt._last_closed_key else 0
                if have_closed >= last_open:
                    continue
                due.append((sym, tf))
            if due:
                jobs: list[tuple[str, str, int]] = []
                for sym, tf in due:
                    mkt = self.markets.get((sym, tf))
                    have = int(mkt.candles[-1]["time"]) if mkt and mkt.candles else 0
                    interval = INTERVAL_MS.get(tf, 60_000) / 1000.0
                    last_open = last_closed_open_time(tf, now)
                    missing = max(1, int((last_open - have) / interval) if have else 1)
                    jobs.append((sym, tf, min(200, missing + 2)))
                await self._catchup_due(jobs)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

    async def _catchup_due(self, due: list[tuple[str, str, int]]) -> None:
        loop = asyncio.get_running_loop()

        def one(sym: str, tf: str, limit: int):
            try:
                return sym, tf, urllib_klines(sym, tf, limit, CATCHUP_TRIES), None
            except Exception as e:
                return sym, tf, None, e

        futs = [loop.run_in_executor(self._catchup_pool, one, sym, tf, limit) for sym, tf, limit in due]
        rows = await asyncio.gather(*futs, return_exceptions=True)
        for item in rows:
            if isinstance(item, Exception):
                log.debug("catch-up worker failed: %s", item)
                continue
            sym, tf, candles, err = item
            if err is not None:
                log.debug("poll %s %s failed: %s", sym, tf, err)
                continue
            mkt = self.markets.get((sym, tf))
            if not mkt or not candles:
                continue
            self._apply_closed_bars(mkt, candles, "rest")

    async def topn_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=30 * 60)
            except asyncio.TimeoutError:
                pass
            if self.stop.is_set():
                break
            if self.cfg["top_n_by_volume"] > 0:
                before = set(self.markets)
                try:
                    await asyncio.to_thread(self.refresh_universe)
                    added = set(self.markets) - before
                    if added:
                        log.info("New top-N markets %s — seeding", added)
                        for key in added:
                            mkt = self.markets[key]
                            try:
                                candles = await asyncio.to_thread(self.rest.klines, mkt.symbol, mkt.tf, 1000)
                                pack = mkt.seed(candles)
                                for s in pack["signals"]:
                                    self.state.mark(signal_key(mkt.symbol, mkt.tf, s))
                                self.state.save()
                            except Exception as e:
                                log.error("seed new market %s failed: %s", key, e)
                except Exception as e:
                    log.error("top-N refresh failed: %s", e)

    async def run(self) -> None:
        log.info(
            "coin-chart signal bot starting  market=USD-M  rest=%s  ws=%s  symbols=%s  tfs=%s  sensitivity=%s  telegram=%s",
            REST_BASES[0] + REST_PREFIX,
            WS_HOSTS[0],
            ",".join(self.cfg["symbols"]),
            ",".join(self.cfg["timeframes"]),
            self.cfg["sensitivity"],
            "on" if self.alerter.enabled else "off",
        )
        self.refresh_universe()
        self.seed_all()
        # REST catch-up always runs, but only for markets whose last closed
        # bar is missing. Confirmed signals come from WS x=true when frames
        # arrive, otherwise from this catch-up within a few seconds of close.
        self.polling = True
        log.info("History seeded. Watching for new confirmed signals (alert-only, no trading).")
        tasks = [
            asyncio.create_task(self.connect_ws_loop(), name="ws"),
            asyncio.create_task(self.poll_loop(), name="poll"),
            asyncio.create_task(self.topn_loop(), name="topn"),
        ]
        await self.stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            self._catchup_pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        self.state.save()
        log.info(
            "Stopped. ws_msgs=%s ws_disconnects=%s",
            self._ws_msgs, self._ws_disconnects,
        )


def setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOGLEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.Formatter.converter = time.localtime  # box is already Asia/Jakarta


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="coin-chart v7 ATR swing signal bot for USD-M Futures (alerts only)")
    p.add_argument("--config", type=Path, default=None, help="YAML or JSON config (see config.example.yaml)")
    p.add_argument("--once", action="store_true", help="Seed history, print last signals, exit (no live stream)")
    p.add_argument("--no-telegram", action="store_true", help="Console alerts only")
    return p.parse_args(argv)


async def amain(args: argparse.Namespace) -> int:
    cfg_path = args.config
    if cfg_path is None:
        candidate = BOT_DIR / "config.yaml"
        cfg_path = candidate if candidate.exists() else None
    cfg = load_config(cfg_path)
    if args.no_telegram:
        cfg["telegram_token"] = ""
        cfg["telegram_chat"] = ""
    bot = SignalBot(cfg)
    if args.once:
        bot.refresh_universe()
        bot.seed_all()
        return 0

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, bot.stop.set)
        except NotImplementedError:
            pass
    await bot.run()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    setup_logging()
    args = parse_args(argv)
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
