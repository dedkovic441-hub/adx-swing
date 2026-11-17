#!/usr/bin/env python3
"""Virtual (paper) execution of ADX Swing BUY/SELL signals.

No Binance account, API keys, or real orders. Two isolated accounts:
Confirmed fills at the confirmation-candle close; Unconfirmed fills at
the close of the candle when a BUY?/SELL? first appears, and exits as
"repainted" if that forming pivot disappears.

One position per symbol+timeframe: BUY opens long, SELL opens short;
an opposite signal closes the open position and opens the new side.

Confirmed state/trades: paper_state.json / paper_trades.jsonl
Unconfirmed: paper_state_unconfirmed.json / paper_trades_unconfirmed.jsonl
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("signal_bot")

ROOT = Path("/workspace/adx-swing-bot")
STATE_PATH = ROOT / "paper_state.json"
TRADES_PATH = ROOT / "paper_trades.jsonl"
LOCK_PATH = ROOT / "run" / "paper.lock"

STARTING_BALANCE = 1000.0
NOTIONAL = 50.0
LEVERAGE = 1.0
TAKER_FEE = 0.0005  # 0.05% per side
WIB = timezone(timedelta(hours=7))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_wib(dt: datetime) -> str:
    return dt.astimezone(WIB).strftime("%Y-%m-%d %H:%M:%S Asia/Jakarta")


def _pos_key(symbol: str, tf: str) -> str:
    return f"{symbol}:{tf}"


def default_state() -> dict:
    return {
        "starting_balance": STARTING_BALANCE,
        "balance": STARTING_BALANCE,
        "realized_pnl": 0.0,
        "fees_paid": 0.0,
        "notional": NOTIONAL,
        "leverage": LEVERAGE,
        "taker_fee": TAKER_FEE,
        "positions": {},
        "closed_count": 0,
        "win_count": 0,
        "updated_utc": _fmt_utc(_now_utc()),
        "updated_wib": _fmt_wib(_now_utc()),
    }


class PaperEngine:
    def __init__(self, state_path: Path = STATE_PATH, trades_path: Path = TRADES_PATH):
        self.state_path = state_path
        self.trades_path = trades_path
        self.lock_path = LOCK_PATH
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

    def _lock_file(self):
        fh = open(self.lock_path, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        return fh

    def load(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "balance" in data:
                return data
        except Exception:
            pass
        return default_state()

    def save(self, st: dict) -> None:
        now = _now_utc()
        st["updated_utc"] = _fmt_utc(now)
        st["updated_wib"] = _fmt_wib(now)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def _append_trade(self, rec: dict) -> None:
        self.trades_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.trades_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _available(self, st: dict) -> float:
        locked = sum(float(p.get("margin") or 0) for p in (st.get("positions") or {}).values())
        return float(st["balance"]) - locked

    def _open(self, st: dict, symbol: str, tf: str, side: str, price: float, now: datetime, reason: str) -> Optional[dict]:
        notional = float(st.get("notional") or NOTIONAL)
        lev = float(st.get("leverage") or LEVERAGE) or 1.0
        fee_rate = float(st.get("taker_fee") or TAKER_FEE)
        margin = notional / lev
        fee = notional * fee_rate
        needed = margin + fee
        avail = self._available(st)
        if avail + 1e-12 < needed:
            log.warning(
                "paper skip open %s %s %s: need %.4f USDT, available %.4f",
                symbol, tf, side, needed, avail,
            )
            return None
        qty = notional / price
        key = _pos_key(symbol, tf)
        pos = {
            "symbol": symbol,
            "timeframe": tf,
            "side": side,
            "qty": qty,
            "entry": price,
            "notional": notional,
            "margin": margin,
            "entry_fee": fee,
            "opened_at_utc": _fmt_utc(now),
            "opened_at_wib": _fmt_wib(now),
        }
        st["positions"][key] = pos
        st["balance"] = float(st["balance"]) - fee
        st["fees_paid"] = float(st.get("fees_paid") or 0) + fee
        rec = {
            "action": "OPEN",
            "symbol": symbol,
            "timeframe": tf,
            "side": side,
            "price": price,
            "qty": qty,
            "fee": fee,
            "notional": notional,
            "margin": margin,
            "reason": reason,
            "at_utc": _fmt_utc(now),
            "at_wib": _fmt_wib(now),
            "balance": st["balance"],
        }
        self._append_trade(rec)
        log.info("paper OPEN %s %s %s @ %s qty=%s fee=%.5f", symbol, tf, side, price, qty, fee)
        return pos

    def _close(self, st: dict, pos: dict, price: float, now: datetime, reason: str) -> dict:
        key = _pos_key(pos["symbol"], pos["timeframe"])
        qty = float(pos["qty"])
        entry = float(pos["entry"])
        side = pos["side"]
        fee_rate = float(st.get("taker_fee") or TAKER_FEE)
        exit_fee = qty * price * fee_rate
        direction = 1.0 if side == "LONG" else -1.0
        gross = (price - entry) * qty * direction
        pnl = gross - float(pos.get("entry_fee") or 0) - exit_fee
        st["balance"] = float(st["balance"]) + pnl
        st["realized_pnl"] = float(st.get("realized_pnl") or 0) + pnl
        st["fees_paid"] = float(st.get("fees_paid") or 0) + exit_fee
        st["closed_count"] = int(st.get("closed_count") or 0) + 1
        if pnl > 0:
            st["win_count"] = int(st.get("win_count") or 0) + 1
        st["positions"].pop(key, None)
        rec = {
            "action": "CLOSE",
            "symbol": pos["symbol"],
            "timeframe": pos["timeframe"],
            "side": side,
            "entry": entry,
            "exit": price,
            "qty": qty,
            "gross_pnl": gross,
            "pnl": pnl,
            "fee": exit_fee,
            "entry_fee": float(pos.get("entry_fee") or 0),
            "win": pnl > 0,
            "reason": reason,
            "opened_at_utc": pos.get("opened_at_utc"),
            "opened_at_wib": pos.get("opened_at_wib"),
            "at_utc": _fmt_utc(now),
            "at_wib": _fmt_wib(now),
            "balance": st["balance"],
        }
        self._append_trade(rec)
        log.info(
            "paper CLOSE %s %s %s entry=%s exit=%s pnl=%.5f",
            pos["symbol"], pos["timeframe"], side, entry, price, pnl,
        )
        return rec

    def close_repainted(self, symbol: str, tf: str, fill_price: float) -> None:
        """Exit an open position because the unconfirmed signal disappeared."""
        try:
            price = float(fill_price)
        except (TypeError, ValueError):
            log.warning("paper skip repaint close %s %s: bad fill price %r", symbol, tf, fill_price)
            return
        if price <= 0:
            return
        lock = self._lock_file()
        try:
            st = self.load()
            st.setdefault("positions", {})
            pos = st["positions"].get(_pos_key(symbol, tf))
            if not pos:
                return
            self._close(st, pos, price, _now_utc(), reason="repainted")
            self.save(st)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def on_signal(self, symbol: str, tf: str, side_name: str, fill_price: float) -> None:
        """Apply a BUY/SELL (or BUY?/SELL?) at the given fill price."""
        n = (side_name or "").upper().rstrip("?")
        if n == "BUY":
            want = "LONG"
        elif n == "SELL":
            want = "SHORT"
        else:
            return
        try:
            price = float(fill_price)
        except (TypeError, ValueError):
            log.warning("paper skip %s %s: bad fill price %r", symbol, tf, fill_price)
            return
        if price <= 0:
            return
        lock = self._lock_file()
        try:
            st = self.load()
            st.setdefault("positions", {})
            now = _now_utc()
            key = _pos_key(symbol, tf)
            pos = st["positions"].get(key)
            if pos and pos.get("side") == want:
                log.info("paper already %s %s %s — ignoring same-side signal", symbol, tf, want)
                return
            if pos and pos.get("side") != want:
                self._close(st, pos, price, now, reason="opposite_signal")
                self._open(st, symbol, tf, want, price, now, reason="flip")
            elif not pos:
                self._open(st, symbol, tf, want, price, now, reason="signal")
            self.save(st)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()


UNCONF_STATE_PATH = ROOT / "paper_state_unconfirmed.json"
UNCONF_TRADES_PATH = ROOT / "paper_trades_unconfirmed.jsonl"
UNCONF_LOCK_PATH = ROOT / "run" / "paper_unconfirmed.lock"

_ENGINE: Optional[PaperEngine] = None
_UNCONF_ENGINE: Optional[PaperEngine] = None


def get_engine() -> PaperEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = PaperEngine()
    return _ENGINE


def get_unconfirmed_engine() -> PaperEngine:
    global _UNCONF_ENGINE
    if _UNCONF_ENGINE is None:
        _UNCONF_ENGINE = PaperEngine(
            state_path=UNCONF_STATE_PATH,
            trades_path=UNCONF_TRADES_PATH,
        )
        _UNCONF_ENGINE.lock_path = UNCONF_LOCK_PATH
    return _UNCONF_ENGINE


def on_signal(symbol: str, tf: str, side_name: str, fill_price: float) -> None:
    get_engine().on_signal(symbol, tf, side_name, fill_price)


def on_unconfirmed_signal(symbol: str, tf: str, side_name: str, fill_price: float) -> None:
    get_unconfirmed_engine().on_signal(symbol, tf, side_name, fill_price)


def close_unconfirmed_repainted(symbol: str, tf: str, fill_price: float) -> None:
    get_unconfirmed_engine().close_repainted(symbol, tf, fill_price)
