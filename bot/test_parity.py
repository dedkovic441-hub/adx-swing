#!/usr/bin/env python3
"""Compare Python swing signals to the JavaScript engine on the same USD-M klines.

Klines are loaded from Binance USD-M Futures REST (`/fapi/v1/klines`). That host
answers HTTP 451 from some regions; in that case the same candles are read from
the public USD-M archive on data.binance.vision, which is the published copy of
`/fapi/v1/klines`.
"""
from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse

BOT_DIR = Path(__file__).resolve().parent
ROOT = BOT_DIR.parent
sys.path.insert(0, str(BOT_DIR))
from swing import detect_swing_signals  # noqa: E402

CASES = [
    ("PUMPUSDT", "5m"),
    ("ANKRUSDT", "1d"),
    ("BTCUSDT", "1h"),
]
VISION_MONTHLY = "https://data.binance.vision/data/futures/um/monthly/klines"


class _FakeResp:
    def __init__(self, code, payload=None, text="", headers=None):
        self.status_code = code
        self._payload = [] if payload is None else payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload


def rows_from_kline_csv(text: str) -> list:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("open_time"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            opened = int(float(parts[0]))
        except ValueError:
            continue
        if opened > 10**14:
            opened //= 1000
        rows.append([opened, *parts[1:]])
    return rows


def _prev_month(year: int, month: int) -> tuple[int, int]:
    month -= 1
    if month == 0:
        return year - 1, 12
    return year, month


def fetch_vision_klines(symbol: str, tf: str, limit: int = 1000) -> list:
    """Last `limit` USD-M klines from the public futures archive."""
    now = datetime.now(timezone.utc)
    year, month = now.year, now.month
    rows: list = []
    seen: set = set()
    started = False
    leading_misses = 0
    trailing_misses = 0
    for _ in range(48):
        url = f"{VISION_MONTHLY}/{symbol}/{tf}/{symbol}-{tf}-{year:04d}-{month:02d}.zip"
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                blob = r.read()
        except HTTPError as e:
            if e.code != 404:
                raise
            if not started:
                leading_misses += 1
                if leading_misses >= 4:
                    break
            else:
                trailing_misses += 1
                if trailing_misses >= 2:
                    break
            year, month = _prev_month(year, month)
            continue
        started = True
        trailing_misses = 0
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for name in zf.namelist():
                if not name.endswith(".csv"):
                    continue
                text = zf.read(name).decode("utf-8", "replace")
                for row in rows_from_kline_csv(text):
                    if row[0] in seen:
                        continue
                    seen.add(row[0])
                    rows.append(row)
        if len(rows) >= limit:
            break
        year, month = _prev_month(year, month)
    if not rows:
        raise RuntimeError(f"no USD-M archive klines for {symbol} {tf}")
    rows.sort(key=lambda row: row[0])
    return rows[-limit:]


def fetch_klines(symbol: str, tf: str, dest: Path) -> None:
    from signal_bot import RestClient

    client = RestClient()
    path = f"/klines?symbol={symbol}&interval={tf}&limit=1000"
    try:
        raw = client.get(path, bulky=True)
    except Exception as e:
        if not client.saw_restricted:
            raise RuntimeError(f"could not fetch {symbol} {tf}: {e}") from e
        print(f"  fapi.binance.com is restricted here; using the USD-M kline archive for {symbol} {tf}")
        raw = fetch_vision_klines(symbol, tf, 1000)
    dest.write_text(json.dumps(raw), encoding="utf-8")


def check_rest_client() -> None:
    """Offline checks: futures URLs, CloudFront 403 retry, perpetual universe."""
    logging.getLogger("signal_bot").setLevel(logging.ERROR)
    from signal_bot import (
        REST_403_ATTEMPTS,
        REST_BASES,
        REST_PREFIX,
        WS_HOSTS,
        RestClient,
        cloudfront_403_backoff,
        combined_stream_url,
    )

    assert REST_BASES[0] == "https://fapi.binance.com"
    assert REST_PREFIX == "/fapi/v1"
    assert WS_HOSTS == ["wss://fstream.binance.com"]
    assert all("/api/v3" not in base and base.startswith("https://fapi") for base in REST_BASES)
    assert combined_stream_url(WS_HOSTS[0], ["btcusdt@kline_1m", "ethusdt@kline_5m"]) == (
        "wss://fstream.binance.com/stream?streams=btcusdt@kline_1m/ethusdt@kline_5m"
    )

    stale = RestClient("https://api.binance.com")
    assert stale.bases[0] == "https://fapi.binance.com"
    assert stale.current is None
    pinned = RestClient("https://fapi2.binance.com")
    assert pinned.bases[0] == "https://fapi2.binance.com"

    sleeps: list[float] = []
    calls: list[str] = []
    client = RestClient()
    client._sleep = sleeps.append

    def deny_then_ok(url, timeout=None, **_kwargs):
        calls.append(url)
        if len(calls) < REST_403_ATTEMPTS:
            return _FakeResp(
                403,
                text="403 ERROR The request could not be satisfied. Generated by cloudfront (CloudFront)",
                headers={"Server": "CloudFront"},
            )
        return _FakeResp(200, payload=[["ok"]])

    client.session.get = deny_then_ok
    assert client.get("/klines?symbol=BTCUSDT&interval=1h&limit=1") == [["ok"]]
    assert calls[0] == "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h&limit=1"
    assert len(calls) == REST_403_ATTEMPTS
    assert sleeps == [cloudfront_403_backoff(i) for i in range(REST_403_ATTEMPTS - 1)]
    assert all(delay <= 1.0 for delay in sleeps)
    assert all("/api/v3" not in url and urlparse(url).hostname == "fapi.binance.com" for url in calls)

    calls.clear()
    sleeps.clear()
    failover = RestClient()
    failover._sleep = sleeps.append

    def cloudfront_then_next(url, timeout=None, **_kwargs):
        calls.append(url)
        host = urlparse(url).hostname
        if host == "fapi.binance.com":
            return _FakeResp(403, text="request could not be satisfied cloudfront", headers={"Server": "CloudFront"})
        if host == "fapi1.binance.com":
            return _FakeResp(200, payload={"ok": True})
        return _FakeResp(500, text="unused")

    failover.session.get = cloudfront_then_next
    assert failover.get("/ping") == {"ok": True}
    assert sum(urlparse(url).hostname == "fapi.binance.com" for url in calls) == REST_403_ATTEMPTS
    assert urlparse(calls[-1]).hostname == "fapi1.binance.com"
    assert sleeps == [cloudfront_403_backoff(i) for i in range(REST_403_ATTEMPTS - 1)]

    calls.clear()
    sleeps.clear()
    restricted = RestClient()
    restricted._sleep = sleeps.append

    def restricted_then_ok(url, timeout=None, **_kwargs):
        calls.append(url)
        host = urlparse(url).hostname
        if host == "fapi.binance.com":
            return _FakeResp(451, text='{"msg":"Service unavailable from a restricted location"}')
        if host == "fapi1.binance.com":
            return _FakeResp(200, payload=[1])
        return _FakeResp(500, text="unused")

    restricted.session.get = restricted_then_ok
    assert restricted.get("/klines?symbol=BTCUSDT&interval=1h&limit=1") == [1]
    assert sleeps == []
    assert restricted.saw_restricted is True
    assert [urlparse(url).hostname for url in calls] == ["fapi.binance.com", "fapi1.binance.com"]

    parsed = RestClient()

    def one_bar(url, timeout=None, **_kwargs):
        return _FakeResp(200, payload=[[1_700_000_000_000, "1.5", "2", "1", "1.8", "9"]])

    parsed.session.get = one_bar
    bars = parsed.klines("BTCUSDT", "1h", 1)
    assert bars == [{"time": 1_700_000_000, "open": 1.5, "high": 2.0, "low": 1.0, "close": 1.8, "volume": 9.0}]

    universe = RestClient()

    def exchange(url, timeout=None, **_kwargs):
        if "/exchangeInfo" in url:
            assert "/api/v3" not in url and "permissions=SPOT" not in url
            return _FakeResp(200, payload={"symbols": [
                {"symbol": "BTCUSDT", "status": "TRADING", "quoteAsset": "USDT", "contractType": "PERPETUAL"},
                {"symbol": "ETHUSDT", "status": "TRADING", "quoteAsset": "USDT", "contractType": "PERPETUAL"},
                {"symbol": "BTCUSDT_241227", "status": "TRADING", "quoteAsset": "USDT", "contractType": "CURRENT_QUARTER"},
                {"symbol": "BNBUSDT", "status": "BREAK", "quoteAsset": "USDT", "contractType": "PERPETUAL"},
                {"symbol": "BTCBUSD", "status": "TRADING", "quoteAsset": "BUSD", "contractType": "PERPETUAL"},
            ]})
        if "/ticker/24hr" in url:
            return _FakeResp(200, payload=[
                {"symbol": "ETHUSDT", "quoteVolume": "50"},
                {"symbol": "BTCUSDT", "quoteVolume": "10"},
                {"symbol": "BTCUSDT_241227", "quoteVolume": "999999"},
                {"symbol": "BNBUSDT", "quoteVolume": "999999"},
                {"symbol": "BTCBUSD", "quoteVolume": "999999"},
            ])
        raise AssertionError(url)

    universe.session.get = exchange
    assert universe.top_usdt(5) == ["ETHUSDT", "BTCUSDT"]

    sample = rows_from_kline_csv(
        "open_time,open,high,low,close,volume\n"
        "1700000000000,1,2,0.5,1.5,9\n"
        "1700000000000000,3,4,2,3.5,8\n"
    )
    assert sample[0][0] == 1_700_000_000_000
    assert sample[1][0] == 1_700_000_000_000
    print("REST client checks OK")


def load_candles(path: Path) -> list[dict]:
    raw = json.loads(path.read_text())
    out = []
    for k in raw[:1000]:
        out.append({
            "time": int(k[0]) // 1000,
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    return out


def js_detect(html: Path, klines: Path, tf: str, sens: str, now_ms: float) -> dict:
    r = subprocess.run(
        ["node", str(BOT_DIR / "js_gold.js"), str(html), str(klines), tf, sens, str(now_ms)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr or r.stdout)
    return json.loads(r.stdout)


def main() -> int:
    try:
        check_rest_client()
    except Exception as e:
        print(f"REST client checks FAILED: {e}", file=sys.stderr)
        return 1
    html = ROOT / "index.html"
    if not html.exists():
        print("index.html not found next to bot/", file=sys.stderr)
        return 2
    cache = Path("/tmp")
    now_ms = time.time() * 1000
    fetch = "--cached" not in sys.argv
    fail = 0
    for sym, tf in CASES:
        dest = cache / f"klines_{sym}_{tf}.json"
        if fetch or not dest.exists():
            print(f"fetch {sym} {tf} …")
            fetch_klines(sym, tf, dest)
        candles = load_candles(dest)
        py = detect_swing_signals(candles, tf, "medium", True, now_ms)
        js = js_detect(html, dest, tf, "medium", now_ms)
        ps, js_sigs = py["signals"], js["signals"]
        print(f"{sym} {tf}: Python {len(ps)}  JS {len(js_sigs)}  bars {len(candles)}")
        diffs = 0
        if len(ps) != len(js_sigs):
            diffs += 1
            print(f"  COUNT MISMATCH py={len(ps)} js={len(js_sigs)}")
        for i in range(min(len(ps), len(js_sigs))):
            a, b = ps[i], js_sigs[i]
            if not (a["time"] == b["time"] and a["dir"] == b["dir"] and a["price"] == b["price"]):
                diffs += 1
                print("  DIFF pivot", i, a["time"], a["dir"], a["price"], "vs", b["time"], b["dir"], b["price"])
            elif a.get("lag") != b.get("lag") or a.get("stars") != b.get("stars") or a.get("confirmTime") != b.get("confirmTime"):
                diffs += 1
                print("  DIFF meta", i, a, b)
        pp, jp = py.get("provisional"), js.get("provisional")
        if (pp is None) != (jp is None):
            diffs += 1
            print("  PROVISIONAL mismatch")
        elif pp and (pp["time"] != jp["time"] or pp["dir"] != jp["dir"] or pp["price"] != jp["price"]):
            diffs += 1
            print("  PROVISIONAL DIFF", pp, jp)
        if diffs:
            fail += 1
            print("  FAIL")
        else:
            last = ps[-1] if ps else None
            extra = f" last {last['name']} @ {last['price']} lag {last['lag']}" if last else ""
            print("  MATCH" + extra)
    print("OVERALL", "OK" if not fail else f"FAIL ({fail} cases)")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
