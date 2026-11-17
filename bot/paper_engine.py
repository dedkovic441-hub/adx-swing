#!/usr/bin/env python3
"""Virtual (paper) execution of ADX Swing BUY/SELL signals.

No Binance account, API keys, or real orders. Two isolated accounts:
Confirmed fills at the confirmation-candle close; Unconfirmed fills at
the close of the candle when a BUY?/SELL? first appears.

Each account holds at most one open position per coin, and only signals
on paper.active_timeframe (default 15m) are traded. Signals on other
timeframes are still alerted and logged, but do not open, add, or close.

A LONG is closed only by a SHORT signal, which realizes PnL and flips
into a new short. A SHORT is closed only by a LONG signal, which flips
into a new long. Fade, timeout, stop, and take-profit never close a
position. The one exception is a one-time migration that closes leftover
positions on a non-active timeframe (reason "timeframe_off").

Unconfirmed scale-in: if that forming pivot disappears (fades/repaints),
the open position is kept and marked faded. A later same-direction
unconfirmed signal on the active timeframe can add another leg sized to
the first leg's notional (not a fixed account default), up to
max_add_positions extra legs (default 4, so 5 legs total). On timeframes
in dca_every_signal_timeframes (default 15m), every new same-direction
unconfirmed signal adds a leg with no fade required. Other timeframes
(default 5m) still need a fade before a same-direction add.

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
DEFAULT_MAX_ADD_POSITIONS = 4
DEFAULT_DCA_EVERY_SIGNAL_TIMEFRAMES = ("15m",)
DEFAULT_ACTIVE_TIMEFRAME = "15m"
WIB = timezone(timedelta(hours=7))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_wib(dt: datetime) -> str:
    return dt.astimezone(WIB).strftime("%Y-%m-%d %H:%M:%S Asia/Jakarta")


def _pos_key(symbol: str, tf: str | None = None) -> str:
    """One position per coin. `tf` is kept for call-site compatibility."""
    return str(symbol or "").upper()


def _legacy_pos_key(symbol: str, tf: str) -> str:
    return f"{symbol}:{tf}"


def is_new_unconfirmed_pivot(prev: dict | None, curr: dict | None) -> bool:
    """True when curr is a different forming pivot than prev (a pivot walk)."""
    if not prev or not curr:
        return False
    if prev.get("time") != curr.get("time"):
        return True
    if prev.get("id") != curr.get("id"):
        return True
    return False


def _load_unconfirmed_yaml(config_path: Optional[Path] = None) -> tuple[Optional[dict], str, Path]:
    path = config_path or (ROOT / "bot" / "config.yaml")
    if not path.exists():
        return None, "", path
    text = path.read_text(encoding="utf-8")
    data = None
    try:
        import yaml
        loaded = yaml.safe_load(text) or {}
        if isinstance(loaded, dict):
            data = loaded
    except Exception:
        data = None
    return data, text, path


def load_unconfirmed_max_add(config_path: Optional[Path] = None) -> int:
    """Read unconfirmed.max_add_positions from config.yaml; default 4."""
    try:
        data, text, path = _load_unconfirmed_yaml(config_path)
        if not path.exists():
            return DEFAULT_MAX_ADD_POSITIONS
        if isinstance(data, dict):
            raw = data.get("unconfirmed")
            if isinstance(raw, dict) and raw.get("max_add_positions") is not None:
                return max(0, int(raw["max_add_positions"]))
            if data.get("unconfirmed_max_add_positions") is not None:
                return max(0, int(data["unconfirmed_max_add_positions"]))
        for line in text.splitlines():
            s = line.split("#", 1)[0].strip()
            if s.startswith("max_add_positions:") or s.startswith("unconfirmed_max_add_positions:"):
                return max(0, int(s.split(":", 1)[1].strip()))
    except Exception:
        pass
    return DEFAULT_MAX_ADD_POSITIONS


def load_unconfirmed_dca_timeframes(config_path: Optional[Path] = None) -> list[str]:
    """Read unconfirmed.dca_every_signal_timeframes; default ['15m'].

    An explicit empty list means no timeframe uses fade-free DCA adds.
    """
    q = "'\""
    try:
        data, text, path = _load_unconfirmed_yaml(config_path)
        if not path.exists():
            return list(DEFAULT_DCA_EVERY_SIGNAL_TIMEFRAMES)
        raw_list = None
        if isinstance(data, dict):
            raw = data.get("unconfirmed")
            if isinstance(raw, dict) and "dca_every_signal_timeframes" in raw:
                raw_list = raw.get("dca_every_signal_timeframes")
            elif "unconfirmed_dca_every_signal_timeframes" in data:
                raw_list = data.get("unconfirmed_dca_every_signal_timeframes")
        if raw_list is not None:
            return [str(t).lower() for t in (raw_list or [])]
        # Fallback if PyYAML is not on this interpreter
        collecting = False
        found = False
        acc: list[str] = []
        for line in text.splitlines():
            s = line.split("#", 1)[0].rstrip()
            stripped = s.strip()
            if stripped.startswith("dca_every_signal_timeframes:"):
                found = True
                rest = stripped.split(":", 1)[1].strip()
                if rest.startswith("[") and rest.endswith("]"):
                    inner = rest[1:-1].strip()
                    return [p.strip().strip(q).lower() for p in inner.split(",") if p.strip()]
                if rest:
                    acc.append(rest.strip("- ").strip(q).lower())
                    collecting = False
                else:
                    collecting = True
                continue
            if collecting:
                if stripped.startswith("-"):
                    token = stripped[1:].strip().strip(q).lower()
                    if token:
                        acc.append(token)
                elif stripped:
                    break
        if found:
            return acc
    except Exception:
        pass
    return list(DEFAULT_DCA_EVERY_SIGNAL_TIMEFRAMES)


def load_active_timeframe(config_path: Optional[Path] = None) -> str:
    """Read paper.active_timeframe from config.yaml; default '15m'."""
    try:
        data, text, path = _load_unconfirmed_yaml(config_path)
        if not path.exists():
            return DEFAULT_ACTIVE_TIMEFRAME
        if isinstance(data, dict):
            raw = data.get("paper")
            if isinstance(raw, dict) and raw.get("active_timeframe"):
                tf = str(raw["active_timeframe"]).strip().lower()
                if tf:
                    return tf
            if data.get("paper_active_timeframe"):
                tf = str(data["paper_active_timeframe"]).strip().lower()
                if tf:
                    return tf
        in_paper = False
        for line in text.splitlines():
            raw = line.split("#", 1)[0]
            stripped = raw.strip()
            indent = len(raw) - len(raw.lstrip())
            if stripped.startswith("paper_active_timeframe:"):
                tf = stripped.split(":", 1)[1].strip().strip("'\"")
                if tf:
                    return tf.lower()
            if stripped.rstrip(":") == "paper" or stripped.startswith("paper:"):
                in_paper = True
                continue
            if in_paper:
                if stripped and indent == 0:
                    in_paper = False
                elif stripped.startswith("active_timeframe:"):
                    tf = stripped.split(":", 1)[1].strip().strip("'\"")
                    if tf:
                        return tf.lower()
    except Exception:
        pass
    return DEFAULT_ACTIVE_TIMEFRAME


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
    def __init__(
        self,
        state_path: Path = STATE_PATH,
        trades_path: Path = TRADES_PATH,
        lock_path: Optional[Path] = None,
        scale_in: bool = False,
        max_add_positions: int = DEFAULT_MAX_ADD_POSITIONS,
        dca_every_signal_timeframes: Optional[list[str]] = None,
        active_timeframe: Optional[str] = DEFAULT_ACTIVE_TIMEFRAME,
    ):
        self.state_path = state_path
        self.trades_path = trades_path
        self.lock_path = Path(lock_path) if lock_path else LOCK_PATH
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.scale_in = bool(scale_in)
        self.max_add_positions = max(0, int(max_add_positions))
        tfs = DEFAULT_DCA_EVERY_SIGNAL_TIMEFRAMES if dca_every_signal_timeframes is None else dca_every_signal_timeframes
        self.dca_every_signal_timeframes = {str(t).lower() for t in tfs}
        self.active_timeframe = str(active_timeframe).strip().lower() if active_timeframe else ""

    def _lock_file(self):
        fh = open(self.lock_path, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        return fh

    def _rekey_unique_positions(self, data: dict) -> None:
        """Rename unambiguous legacy SYMBOL:tf keys to SYMBOL. Leaves collisions alone."""
        positions = data.get("positions") or {}
        by_sym: dict[str, list[tuple[str, dict]]] = {}
        for k, pos in positions.items():
            if not isinstance(pos, dict):
                continue
            sym = str(pos.get("symbol") or str(k).split(":")[0]).upper()
            by_sym.setdefault(sym, []).append((k, pos))
        new_positions: dict[str, dict] = {}
        changed = False
        leftovers: dict[str, dict] = {}
        for sym, items in by_sym.items():
            if len(items) == 1:
                k, pos = items[0]
                nk = _pos_key(sym)
                new_positions[nk] = pos
                if k != nk:
                    changed = True
            else:
                for k, pos in items:
                    leftovers[k] = pos
        if changed:
            leftovers.update(new_positions)
            data["positions"] = leftovers

    def load(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "balance" in data:
                if self.scale_in:
                    for pos in (data.get("positions") or {}).values():
                        if isinstance(pos, dict):
                            self._normalize_position(pos)
                self._rekey_unique_positions(data)
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

    def _is_tradable_tf(self, tf: str) -> bool:
        if not self.active_timeframe:
            return True
        return str(tf or "").lower() == self.active_timeframe

    def _find_position(
        self, st: dict, symbol: str, tf: Optional[str] = None
    ) -> tuple[Optional[str], Optional[dict]]:
        """Locate the single position for a coin, accepting legacy SYMBOL:tf keys."""
        positions = st.get("positions") or {}
        sym = str(symbol or "").upper()
        key = _pos_key(sym)
        if key in positions and isinstance(positions[key], dict):
            return key, positions[key]
        if tf:
            for cand in (
                f"{symbol}:{tf}",
                f"{sym}:{tf}",
                f"{symbol}:{str(tf).lower()}",
                f"{sym}:{str(tf).lower()}",
            ):
                if cand in positions and isinstance(positions[cand], dict):
                    return cand, positions[cand]
        matches: list[tuple[str, dict]] = []
        for k, pos in positions.items():
            if not isinstance(pos, dict):
                continue
            if str(pos.get("symbol") or "").upper() == sym or str(k).split(":")[0].upper() == sym:
                matches.append((k, pos))
        if tf:
            tf_l = str(tf).lower()
            for k, pos in matches:
                if str(pos.get("timeframe") or "").lower() == tf_l:
                    return k, pos
        if len(matches) == 1:
            return matches[0]
        if matches:
            return matches[0]
        return None, None

    def _drop_position(self, st: dict, pos: dict) -> Optional[str]:
        positions = st.setdefault("positions", {})
        for k, p in list(positions.items()):
            if p is pos:
                positions.pop(k, None)
                return k
        key, found = self._find_position(st, pos.get("symbol") or "", pos.get("timeframe"))
        if key:
            positions.pop(key, None)
            return key
        return None

    def _mark_price(self, prices: dict, symbol: str) -> Optional[float]:
        for k in (symbol, str(symbol).upper(), str(symbol).lower()):
            if k in prices:
                try:
                    px = float(prices[k])
                    if px > 0:
                        return px
                except (TypeError, ValueError):
                    pass
        return None

    def migrate_to_active_timeframe(self, prices: dict[str, float]) -> list[dict]:
        """Close non-active-TF positions; keep one position per coin; rekey to SYMBOL.

        Non-active positions are closed at the provided mark with reason
        "timeframe_off". Extra positions on the same coin are closed with
        reason "duplicate_symbol" (active-TF and then newest opening kept).
        Idempotent. Returns the CLOSE records written.
        """
        lock = self._lock_file()
        closed_recs: list[dict] = []
        try:
            st = self.load()
            st.setdefault("positions", {})
            now = _now_utc()
            active = self.active_timeframe

            if active:
                for _key, pos in list((st.get("positions") or {}).items()):
                    if not isinstance(pos, dict):
                        continue
                    tf = str(pos.get("timeframe") or "").lower()
                    if tf == active:
                        continue
                    symbol = pos.get("symbol") or str(_key).split(":")[0]
                    px = self._mark_price(prices, symbol)
                    if px is None:
                        log.warning(
                            "paper migrate skip %s %s: no mark price for timeframe_off close",
                            symbol, tf,
                        )
                        continue
                    closed_recs.append(self._close(st, pos, px, now, reason="timeframe_off"))

            by_sym: dict[str, list[tuple[str, dict]]] = {}
            for key, pos in list((st.get("positions") or {}).items()):
                if not isinstance(pos, dict):
                    continue
                sym = str(pos.get("symbol") or str(key).split(":")[0]).upper()
                by_sym.setdefault(sym, []).append((key, pos))

            for sym, items in by_sym.items():
                if len(items) <= 1:
                    continue

                def rank(item: tuple[str, dict]) -> tuple[int, str]:
                    _k, p = item
                    tf = str(p.get("timeframe") or "").lower()
                    on_active = 1 if active and tf == active else 0
                    return (on_active, str(p.get("opened_at_utc") or ""))

                ordered = sorted(items, key=rank)
                for _key, pos in ordered[:-1]:
                    px = self._mark_price(prices, pos.get("symbol") or sym)
                    if px is None:
                        log.warning("paper migrate skip duplicate %s: no mark price", sym)
                        continue
                    closed_recs.append(self._close(st, pos, px, now, reason="duplicate_symbol"))

            new_positions: dict[str, dict] = {}
            for key, pos in list((st.get("positions") or {}).items()):
                if not isinstance(pos, dict):
                    continue
                nk = _pos_key(pos.get("symbol") or str(key).split(":")[0])
                if nk in new_positions and new_positions[nk] is not pos:
                    log.warning("paper migrate unexpected leftover key %s vs %s", key, nk)
                    continue
                new_positions[nk] = pos
            st["positions"] = new_positions
            st["active_timeframe"] = active or None
            self.save(st)
            return closed_recs
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def _normalize_position(self, pos: dict) -> dict:
        """Backfill legs / first_notional on pre-scale-in unconfirmed positions."""
        notional = float(pos.get("notional") or NOTIONAL)
        if not pos.get("legs"):
            pos["legs"] = [{
                "price": float(pos.get("entry") or 0),
                "qty": float(pos.get("qty") or 0),
                "notional": notional,
                "margin": float(pos.get("margin") or notional),
                "entry_fee": float(pos.get("entry_fee") or 0),
                "opened_at_utc": pos.get("opened_at_utc"),
                "opened_at_wib": pos.get("opened_at_wib"),
            }]
        if pos.get("first_notional") is None:
            first = pos["legs"][0]
            pos["first_notional"] = float(first.get("notional") or notional)
        pos["leg_count"] = len(pos["legs"])
        pos.setdefault("faded", False)
        pos.setdefault("fade_count", 0)
        return pos

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
        if self.scale_in:
            pos["first_notional"] = notional
            pos["leg_count"] = 1
            pos["faded"] = False
            pos["fade_count"] = 0
            pos["legs"] = [{
                "price": price,
                "qty": qty,
                "notional": notional,
                "margin": margin,
                "entry_fee": fee,
                "opened_at_utc": pos["opened_at_utc"],
                "opened_at_wib": pos["opened_at_wib"],
            }]
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
        if self.scale_in:
            rec["leg_count"] = 1
        self._append_trade(rec)
        log.info("paper OPEN %s %s %s @ %s qty=%s fee=%.5f", symbol, tf, side, price, qty, fee)
        return pos

    def _requires_fade_to_add(self, tf: str) -> bool:
        return str(tf or "").lower() not in self.dca_every_signal_timeframes

    def _try_add_leg(
        self,
        st: dict,
        pos: dict,
        price: float,
        now: datetime,
        require_fade: Optional[bool] = None,
    ) -> Optional[dict]:
        """Add one same-direction leg sized to the first leg's notional."""
        self._normalize_position(pos)
        symbol, tf, side = pos["symbol"], pos["timeframe"], pos["side"]
        if require_fade is None:
            require_fade = self._requires_fade_to_add(tf)
        if require_fade and not pos.get("faded"):
            log.info(
                "paper already %s %s %s — ignoring same-side signal (no fade since last entry)",
                symbol, tf, side,
            )
            return None
        max_legs = 1 + self.max_add_positions
        if len(pos["legs"]) >= max_legs:
            log.info(
                "paper skip add %s %s %s: already %s legs (max %s additional)",
                symbol, tf, side, len(pos["legs"]), self.max_add_positions,
            )
            return None
        first_notional = float(pos.get("first_notional") or NOTIONAL)
        lev = float(st.get("leverage") or LEVERAGE) or 1.0
        fee_rate = float(st.get("taker_fee") or TAKER_FEE)
        margin = first_notional / lev
        fee = first_notional * fee_rate
        needed = margin + fee
        avail = self._available(st)
        if avail + 1e-12 < needed:
            log.warning(
                "paper skip add %s %s %s: need %.4f USDT, available %.4f",
                symbol, tf, side, needed, avail,
            )
            return None
        qty_add = first_notional / price
        old_qty = float(pos["qty"])
        old_entry = float(pos["entry"])
        new_qty = old_qty + qty_add
        pos["entry"] = (old_entry * old_qty + price * qty_add) / new_qty
        pos["qty"] = new_qty
        pos["notional"] = float(pos.get("notional") or 0) + first_notional
        pos["margin"] = float(pos.get("margin") or 0) + margin
        pos["entry_fee"] = float(pos.get("entry_fee") or 0) + fee
        leg = {
            "price": price,
            "qty": qty_add,
            "notional": first_notional,
            "margin": margin,
            "entry_fee": fee,
            "opened_at_utc": _fmt_utc(now),
            "opened_at_wib": _fmt_wib(now),
        }
        pos["legs"].append(leg)
        pos["leg_count"] = len(pos["legs"])
        pos["faded"] = False
        pos["last_add_utc"] = leg["opened_at_utc"]
        pos["last_add_wib"] = leg["opened_at_wib"]
        st["balance"] = float(st["balance"]) - fee
        st["fees_paid"] = float(st.get("fees_paid") or 0) + fee
        rec = {
            "action": "ADD",
            "symbol": symbol,
            "timeframe": tf,
            "side": side,
            "price": price,
            "qty": qty_add,
            "fee": fee,
            "notional": first_notional,
            "margin": margin,
            "avg_entry": pos["entry"],
            "total_qty": pos["qty"],
            "total_notional": pos["notional"],
            "leg_count": pos["leg_count"],
            "reason": "dca" if not require_fade else "scale_in",
            "at_utc": leg["opened_at_utc"],
            "at_wib": leg["opened_at_wib"],
            "balance": st["balance"],
        }
        self._append_trade(rec)
        log.info(
            "paper ADD %s %s %s @ %s leg %s/%s qty=%s avg_entry=%s fee=%.5f",
            symbol, tf, side, price, pos["leg_count"], max_legs, qty_add, pos["entry"], fee,
        )
        return leg

    def _close(self, st: dict, pos: dict, price: float, now: datetime, reason: str) -> dict:
        if self.scale_in:
            self._normalize_position(pos)
        self._drop_position(st, pos)
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
        if self.scale_in:
            rec["leg_count"] = int(pos.get("leg_count") or len(pos.get("legs") or []) or 1)
            rec["total_notional"] = float(pos.get("notional") or 0)
        self._append_trade(rec)
        log.info(
            "paper CLOSE %s %s %s entry=%s exit=%s pnl=%.5f",
            pos["symbol"], pos["timeframe"], side, entry, price, pnl,
        )
        return rec

    def close_repainted(self, symbol: str, tf: str, fill_price: float) -> None:
        """Mark an unconfirmed fade. Never close: only an opposite signal exits."""
        try:
            price = float(fill_price)
        except (TypeError, ValueError):
            log.warning("paper skip fade %s %s: bad fill price %r", symbol, tf, fill_price)
            return
        if price <= 0:
            return
        lock = self._lock_file()
        try:
            st = self.load()
            st.setdefault("positions", {})
            _key, pos = self._find_position(st, symbol, tf)
            if not pos:
                return
            pos_tf = str(pos.get("timeframe") or "").lower()
            if str(tf or "").lower() != pos_tf:
                log.info(
                    "paper ignore fade %s %s — open position is on %s",
                    symbol, tf, pos_tf or "?",
                )
                return
            now = _now_utc()
            if self.scale_in:
                self._mark_faded(st, pos, price, now)
                self.save(st)
            else:
                log.info(
                    "paper ignore fade %s %s %s — positions only exit on an opposite signal",
                    symbol, tf, pos.get("side"),
                )
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def _mark_faded(self, st: dict, pos: dict, price: float, now: datetime) -> dict:
        self._normalize_position(pos)
        pos["faded"] = True
        pos["fade_count"] = int(pos.get("fade_count") or 0) + 1
        pos["last_fade_utc"] = _fmt_utc(now)
        pos["last_fade_wib"] = _fmt_wib(now)
        pos["last_fade_price"] = price
        rec = {
            "action": "FADED",
            "symbol": pos["symbol"],
            "timeframe": pos["timeframe"],
            "side": pos["side"],
            "price": price,
            "entry": float(pos["entry"]),
            "qty": float(pos["qty"]),
            "leg_count": int(pos["leg_count"]),
            "fade_count": int(pos["fade_count"]),
            "reason": "repainted",
            "opened_at_utc": pos.get("opened_at_utc"),
            "opened_at_wib": pos.get("opened_at_wib"),
            "at_utc": pos["last_fade_utc"],
            "at_wib": pos["last_fade_wib"],
            "balance": st["balance"],
        }
        self._append_trade(rec)
        log.info(
            "paper FADED %s %s %s @ %s — keeping position open (%s legs, fade #%s)",
            pos["symbol"], pos["timeframe"], pos["side"], price,
            pos["leg_count"], pos["fade_count"],
        )
        return rec

    def on_signal(self, symbol: str, tf: str, side_name: str, fill_price: float) -> None:
        """Apply a BUY/SELL (or BUY?/SELL?) at the given fill price.

        Only the active timeframe is traded. An opposite-direction signal
        closes the open coin position and opens the new side. Same-direction
        signals add a DCA/scale-in leg when the unconfirmed rules allow it.
        """
        n = (side_name or "").upper().rstrip("?")
        if n == "BUY":
            want = "LONG"
        elif n == "SELL":
            want = "SHORT"
        else:
            return
        if not self._is_tradable_tf(tf):
            log.info(
                "paper skip %s %s %s — not the active timeframe (%s)",
                symbol, tf, side_name, self.active_timeframe or "any",
            )
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
            _key, pos = self._find_position(st, symbol)
            if pos and pos.get("side") == want:
                if self.scale_in:
                    self._try_add_leg(st, pos, price, now)
                    self.save(st)
                else:
                    log.info("paper already %s %s — ignoring same-side signal", symbol, want)
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
        _ENGINE = PaperEngine(active_timeframe=load_active_timeframe())
    return _ENGINE


def get_unconfirmed_engine() -> PaperEngine:
    global _UNCONF_ENGINE
    if _UNCONF_ENGINE is None:
        _UNCONF_ENGINE = PaperEngine(
            state_path=UNCONF_STATE_PATH,
            trades_path=UNCONF_TRADES_PATH,
            lock_path=UNCONF_LOCK_PATH,
            scale_in=True,
            max_add_positions=load_unconfirmed_max_add(),
            dca_every_signal_timeframes=load_unconfirmed_dca_timeframes(),
            active_timeframe=load_active_timeframe(),
        )
    return _UNCONF_ENGINE


def on_signal(symbol: str, tf: str, side_name: str, fill_price: float) -> None:
    get_engine().on_signal(symbol, tf, side_name, fill_price)


def on_unconfirmed_signal(symbol: str, tf: str, side_name: str, fill_price: float) -> None:
    get_unconfirmed_engine().on_signal(symbol, tf, side_name, fill_price)


def close_unconfirmed_repainted(symbol: str, tf: str, fill_price: float) -> None:
    get_unconfirmed_engine().close_repainted(symbol, tf, fill_price)
