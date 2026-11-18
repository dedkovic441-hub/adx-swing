#!/usr/bin/env python3
"""Unit tests for unconfirmed paper scale-in (fade keeps position, add legs)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BOT_DIR))

import paper_engine as pe  # noqa: E402


_orig_default_state = pe.default_state


def _fixed_default_state() -> dict:
    st = _orig_default_state()
    st["sizing"] = "fixed"  # legacy tests exercise fixed-notional sizing
    return st


pe.default_state = _fixed_default_state


def _engine(tmp: Path, **kwargs) -> pe.PaperEngine:
    return pe.PaperEngine(
        state_path=tmp / "paper_state_unconfirmed.json",
        trades_path=tmp / "paper_trades_unconfirmed.jsonl",
        lock_path=tmp / "paper_unconfirmed.lock",
        scale_in=True,
        max_add_positions=kwargs.get("max_add_positions", 4),
        dca_every_signal_timeframes=kwargs.get("dca_every_signal_timeframes", ["15m"]),
        dca_signal_every=kwargs.get("dca_signal_every", 3),
        active_timeframe=kwargs.get("active_timeframe", None),
        take_profit_usd=kwargs.get("take_profit_usd", 2.0),
    )


def _trades(eng: pe.PaperEngine) -> list[dict]:
    if not eng.trades_path.exists():
        return []
    rows = []
    for line in eng.trades_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


class UnconfirmedScaleInTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.eng = _engine(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_fade_keeps_position_and_logs_repainted(self):
        self.eng.on_signal("AKEUSDT", "5m", "BUY?", 10.0)
        st = self.eng.load()
        pos = st["positions"]["AKEUSDT"]
        bal_after_open = st["balance"]
        self.assertEqual(pos["leg_count"], 1)
        self.assertFalse(pos["faded"])

        self.eng.close_repainted("AKEUSDT", "5m", 9.5)
        st = self.eng.load()
        self.assertIn("AKEUSDT", st["positions"])
        pos = st["positions"]["AKEUSDT"]
        self.assertTrue(pos["faded"])
        self.assertEqual(pos["fade_count"], 1)
        self.assertEqual(pos["qty"], 50.0 / 10.0)
        self.assertEqual(st["balance"], bal_after_open)
        self.assertEqual(st["closed_count"], 0)
        self.assertEqual(st["realized_pnl"], 0.0)
        actions = [t["action"] for t in _trades(self.eng)]
        self.assertEqual(actions, ["OPEN", "FADED"])
        faded = _trades(self.eng)[-1]
        self.assertEqual(faded["reason"], "repainted")
        self.assertEqual(faded["price"], 9.5)

    def test_add_uses_first_leg_notional_not_account_default(self):
        # First leg $30; account default stays $50. Each add must be $30.
        st = self.eng.load()
        st["notional"] = 30.0
        self.eng.save(st)

        self.eng.on_signal("FOOUSDT", "5m", "BUY?", 3.0)
        self.eng.close_repainted("FOOUSDT", "5m", 2.9)
        self.eng.on_signal("FOOUSDT", "5m", "BUY?", 4.0)

        st = self.eng.load()
        pos = st["positions"]["FOOUSDT"]
        self.assertEqual(pos["first_notional"], 30.0)
        self.assertEqual(pos["leg_count"], 2)
        self.assertEqual(pos["notional"], 60.0)
        self.assertAlmostEqual(pos["qty"], 30.0 / 3.0 + 30.0 / 4.0)
        expected_avg = (3.0 * (30.0 / 3.0) + 4.0 * (30.0 / 4.0)) / pos["qty"]
        self.assertAlmostEqual(pos["entry"], expected_avg)
        self.assertFalse(pos["faded"])
        # Two 0.05% fees on $30 = 0.03
        self.assertAlmostEqual(st["fees_paid"], 30.0 * 0.0005 * 2)
        add = [t for t in _trades(self.eng) if t["action"] == "ADD"][0]
        self.assertEqual(add["notional"], 30.0)
        self.assertEqual(add["leg_count"], 2)

    def test_no_add_without_fade(self):
        self.eng.on_signal("BARUSDT", "5m", "BUY?", 10.0)
        self.eng.on_signal("BARUSDT", "5m", "BUY?", 11.0)
        st = self.eng.load()
        pos = st["positions"]["BARUSDT"]
        self.assertEqual(pos["leg_count"], 1)
        self.assertEqual(pos["notional"], 50.0)
        self.assertEqual([t["action"] for t in _trades(self.eng)], ["OPEN"])

    def test_cap_is_four_additional_legs(self):
        # 5m is not in the default fade-free list, so each add still needs a fade.
        self.eng.on_signal("CAPUSDT", "5m", "SELL?", 20.0)
        for px in (19.0, 18.0, 17.0, 16.0, 15.0):
            self.eng.close_repainted("CAPUSDT", "5m", px + 0.1)
            self.eng.on_signal("CAPUSDT", "5m", "SELL?", px)
        st = self.eng.load()
        pos = st["positions"]["CAPUSDT"]
        self.assertEqual(pos["leg_count"], 5)
        self.assertEqual(pos["notional"], 250.0)
        # Sixth same-direction after another fade is ignored
        self.eng.close_repainted("CAPUSDT", "5m", 14.5)
        self.eng.on_signal("CAPUSDT", "5m", "SELL?", 14.0)
        st = self.eng.load()
        pos = st["positions"]["CAPUSDT"]
        self.assertEqual(pos["leg_count"], 5)
        self.assertEqual(pos["notional"], 250.0)
        self.assertTrue(pos["faded"])  # fade kept; add skipped
        adds = [t for t in _trades(self.eng) if t["action"] == "ADD"]
        self.assertEqual(len(adds), 4)

    def test_opposite_closes_whole_stack_and_flips(self):
        self.eng.on_signal("BAZUSDT", "5m", "BUY?", 10.0)
        self.eng.close_repainted("BAZUSDT", "5m", 10.0)
        self.eng.on_signal("BAZUSDT", "5m", "BUY?", 12.0)
        st = self.eng.load()
        pos = st["positions"]["BAZUSDT"]
        avg = float(pos["entry"])
        qty = float(pos["qty"])
        entry_fee = float(pos["entry_fee"])
        # Flip at 11
        self.eng.on_signal("BAZUSDT", "5m", "SELL?", 11.0)
        st = self.eng.load()
        close = [t for t in _trades(self.eng) if t["action"] == "CLOSE"][0]
        self.assertEqual(close["reason"], "opposite_signal")
        self.assertEqual(close["leg_count"], 2)
        exit_fee = qty * 11.0 * 0.0005
        expected_pnl = (11.0 - avg) * qty - entry_fee - exit_fee
        self.assertAlmostEqual(close["pnl"], expected_pnl)
        # New short is a fresh 1-leg position at account notional
        pos = st["positions"]["BAZUSDT"]
        self.assertEqual(pos["side"], "SHORT")
        self.assertEqual(pos["leg_count"], 1)
        self.assertEqual(pos["notional"], 50.0)
        self.assertEqual(pos["entry"], 11.0)
        self.assertEqual(st["closed_count"], 1)

    def test_skip_add_when_cash_insufficient(self):
        st = self.eng.load()
        st["balance"] = 50.03  # enough for first $50 + 0.025 fee, not a second leg
        self.eng.save(st)
        self.eng.on_signal("CASHUSDT", "5m", "BUY?", 5.0)
        self.eng.close_repainted("CASHUSDT", "5m", 5.0)
        self.eng.on_signal("CASHUSDT", "5m", "BUY?", 6.0)
        st = self.eng.load()
        pos = st["positions"]["CASHUSDT"]
        self.assertEqual(pos["leg_count"], 1)
        self.assertTrue(pos["faded"])
        self.assertEqual([t["action"] for t in _trades(self.eng)], ["OPEN", "FADED"])

    def test_legacy_state_loads_and_can_scale_in(self):
        legacy = pe.default_state()
        legacy["balance"] = 999.975
        legacy["fees_paid"] = 0.025
        legacy["positions"]["OLDUSDT:5m"] = {
            "symbol": "OLDUSDT",
            "timeframe": "5m",
            "side": "LONG",
            "qty": 10.0,
            "entry": 5.0,
            "notional": 50.0,
            "margin": 50.0,
            "entry_fee": 0.025,
            "opened_at_utc": "2026-11-17T00:00:00Z",
            "opened_at_wib": "2026-11-17 07:00:00 Asia/Jakarta",
        }
        (self.tmp / "paper_state_unconfirmed.json").write_text(
            json.dumps(legacy, indent=2), encoding="utf-8"
        )
        self.eng.close_repainted("OLDUSDT", "5m", 5.2)
        self.eng.on_signal("OLDUSDT", "5m", "BUY?", 5.5)
        st = self.eng.load()
        pos = st["positions"]["OLDUSDT"]
        self.assertEqual(pos["first_notional"], 50.0)
        self.assertEqual(pos["leg_count"], 2)
        self.assertEqual(len(pos["legs"]), 2)
        self.assertAlmostEqual(pos["qty"], 10.0 + 50.0 / 5.5)

    def test_confirmed_engine_ignores_same_side_and_fade_does_not_close(self):
        conf = pe.PaperEngine(
            state_path=self.tmp / "paper_state.json",
            trades_path=self.tmp / "paper_trades.jsonl",
            lock_path=self.tmp / "paper.lock",
            scale_in=False,
            active_timeframe=None,
        )
        conf.on_signal("AKEUSDT", "5m", "SELL", 10.0)
        conf.on_signal("AKEUSDT", "5m", "SELL", 11.0)
        st = conf.load()
        pos = st["positions"]["AKEUSDT"]
        self.assertEqual(pos["entry"], 10.0)
        self.assertNotIn("legs", pos)
        conf.close_repainted("AKEUSDT", "5m", 9.0)
        st = conf.load()
        self.assertIn("AKEUSDT", st["positions"])
        self.assertEqual(st["positions"]["AKEUSDT"]["entry"], 10.0)
        self.assertEqual(st["closed_count"], 0)
        self.assertEqual([t["action"] for t in _trades(conf)], ["OPEN"])

    def test_15m_dca_adds_on_every_third_same_dir_signal(self):
        self.eng.on_signal("DCAUSDT", "15m", "SELL?", 10.0)
        self.eng.on_signal("DCAUSDT", "15m", "SELL?", 9.8)
        pos = self.eng.load()["positions"]["DCAUSDT"]
        self.assertEqual(pos["leg_count"], 1)
        self.assertEqual(pos["dca_signal_count"], 1)
        self.eng.on_signal("DCAUSDT", "15m", "SELL?", 9.6)
        pos = self.eng.load()["positions"]["DCAUSDT"]
        self.assertEqual(pos["leg_count"], 1)
        self.assertEqual(pos["dca_signal_count"], 2)
        self.eng.on_signal("DCAUSDT", "15m", "SELL?", 9.4)
        st = self.eng.load()
        pos = st["positions"]["DCAUSDT"]
        self.assertEqual(pos["leg_count"], 2)
        self.assertEqual(pos["dca_signal_count"], 3)
        self.assertEqual(pos["notional"], 100.0)
        self.assertEqual(pos["first_notional"], 50.0)
        self.assertFalse(pos["faded"])
        self.assertAlmostEqual(pos["qty"], 50.0 / 10.0 + 50.0 / 9.4)
        add = [t for t in _trades(self.eng) if t["action"] == "ADD"][0]
        self.assertEqual(add["reason"], "dca")
        self.assertEqual(add["notional"], 50.0)
        # 4th and 5th do not add; 6th does
        self.eng.on_signal("DCAUSDT", "15m", "SELL?", 9.3)
        self.eng.on_signal("DCAUSDT", "15m", "SELL?", 9.2)
        self.assertEqual(self.eng.load()["positions"]["DCAUSDT"]["leg_count"], 2)
        self.eng.on_signal("DCAUSDT", "15m", "SELL?", 9.1)
        pos = self.eng.load()["positions"]["DCAUSDT"]
        self.assertEqual(pos["leg_count"], 3)
        self.assertEqual(pos["dca_signal_count"], 6)

    def test_15m_dca_uses_first_leg_notional(self):
        st = self.eng.load()
        st["notional"] = 30.0
        self.eng.save(st)
        self.eng.on_signal("DCA30USDT", "15m", "BUY?", 3.0)
        self.eng.on_signal("DCA30USDT", "15m", "BUY?", 3.1)
        self.eng.on_signal("DCA30USDT", "15m", "BUY?", 3.2)
        self.eng.on_signal("DCA30USDT", "15m", "BUY?", 4.0)
        pos = self.eng.load()["positions"]["DCA30USDT"]
        self.assertEqual(pos["first_notional"], 30.0)
        self.assertEqual(pos["notional"], 60.0)
        self.assertEqual(pos["leg_count"], 2)
        self.assertEqual(pos["dca_signal_count"], 3)

    def test_15m_dca_respects_add_cap(self):
        self.eng.on_signal("CAP15USDT", "15m", "SELL?", 20.0)
        # 12 post-entry same-dir signals → adds on 3, 6, 9, 12 (max 4 adds)
        for i, px in enumerate((19.5, 19.0, 18.5, 18.0, 17.5, 17.0, 16.5, 16.0, 15.5, 15.0, 14.5, 14.0), start=1):
            self.eng.on_signal("CAP15USDT", "15m", "SELL?", px)
        pos = self.eng.load()["positions"]["CAP15USDT"]
        self.assertEqual(pos["leg_count"], 5)
        self.assertEqual(pos["notional"], 250.0)
        self.assertEqual(pos["dca_signal_count"], 12)
        self.eng.on_signal("CAP15USDT", "15m", "SELL?", 13.5)
        self.eng.on_signal("CAP15USDT", "15m", "SELL?", 13.0)
        self.eng.on_signal("CAP15USDT", "15m", "SELL?", 12.5)  # would be 15th, still capped
        pos = self.eng.load()["positions"]["CAP15USDT"]
        self.assertEqual(pos["leg_count"], 5)
        self.assertEqual(pos["dca_signal_count"], 15)
        adds = [t for t in _trades(self.eng) if t["action"] == "ADD"]
        self.assertEqual(len(adds), 4)

    def test_5m_still_requires_fade_even_when_15m_dca_enabled(self):
        self.eng.on_signal("FIVEUSDT", "5m", "BUY?", 10.0)
        self.eng.on_signal("FIVEUSDT", "5m", "BUY?", 11.0)
        pos = self.eng.load()["positions"]["FIVEUSDT"]
        self.assertEqual(pos["leg_count"], 1)
        self.assertEqual([t["action"] for t in _trades(self.eng)], ["OPEN"])

    def test_is_new_unconfirmed_pivot(self):
        a = {"side": "SELL", "time": 100, "id": "top"}
        b = {"side": "SELL", "time": 200, "id": "top"}
        c = {"side": "SELL", "time": 100, "id": "top"}
        d = {"side": "SELL", "time": 100, "id": "top2"}
        self.assertTrue(pe.is_new_unconfirmed_pivot(a, b))
        self.assertFalse(pe.is_new_unconfirmed_pivot(a, c))
        self.assertTrue(pe.is_new_unconfirmed_pivot(a, d))
        self.assertFalse(pe.is_new_unconfirmed_pivot(a, None))

    def test_load_unconfirmed_max_add_from_yaml(self):
        cfg = self.tmp / "config.yaml"
        cfg.write_text("unconfirmed:\n  max_add_positions: 2\n", encoding="utf-8")
        self.assertEqual(pe.load_unconfirmed_max_add(cfg), 2)
        cfg.write_text("symbols: [AKEUSDT]\n", encoding="utf-8")
        self.assertEqual(pe.load_unconfirmed_max_add(cfg), 4)
        self.assertEqual(pe.load_unconfirmed_max_add(self.tmp / "missing.yaml"), 4)

    def test_load_unconfirmed_dca_timeframes_from_yaml(self):
        cfg = self.tmp / "config.yaml"
        cfg.write_text("unconfirmed:\n  dca_every_signal_timeframes: [15m]\n", encoding="utf-8")
        self.assertEqual(pe.load_unconfirmed_dca_timeframes(cfg), ["15m"])
        cfg.write_text("unconfirmed:\n  dca_every_signal_timeframes:\n    - 15m\n    - 1h\n", encoding="utf-8")
        self.assertEqual(pe.load_unconfirmed_dca_timeframes(cfg), ["15m", "1h"])
        cfg.write_text("unconfirmed:\n  dca_every_signal_timeframes: []\n", encoding="utf-8")
        self.assertEqual(pe.load_unconfirmed_dca_timeframes(cfg), [])
        cfg.write_text("symbols: [AKEUSDT]\n", encoding="utf-8")
        self.assertEqual(pe.load_unconfirmed_dca_timeframes(cfg), ["15m"])
        self.assertEqual(pe.load_unconfirmed_dca_timeframes(self.tmp / "missing.yaml"), ["15m"])

    def test_load_unconfirmed_dca_signal_every_from_yaml(self):
        cfg = self.tmp / "config.yaml"
        cfg.write_text("unconfirmed:\n  dca_signal_every: 3\n", encoding="utf-8")
        self.assertEqual(pe.load_unconfirmed_dca_signal_every(cfg), 3)
        cfg.write_text("unconfirmed:\n  dca_signal_every: 1\n", encoding="utf-8")
        self.assertEqual(pe.load_unconfirmed_dca_signal_every(cfg), 1)
        cfg.write_text("unconfirmed:\n  dca_signal_every: 0\n", encoding="utf-8")
        self.assertEqual(pe.load_unconfirmed_dca_signal_every(cfg), 0)
        cfg.write_text("symbols: [AKEUSDT]\n", encoding="utf-8")
        self.assertEqual(pe.load_unconfirmed_dca_signal_every(cfg), 3)
        self.assertEqual(pe.load_unconfirmed_dca_signal_every(self.tmp / "missing.yaml"), 3)

    def test_5m_interval_dca_when_listed(self):
        eng = _engine(self.tmp, dca_every_signal_timeframes=["15m", "5m"], dca_signal_every=3, active_timeframe="5m")
        eng.on_signal("FIVDCAUSDT", "5m", "BUY?", 10.0)
        eng.on_signal("FIVDCAUSDT", "5m", "BUY?", 10.1)
        eng.on_signal("FIVDCAUSDT", "5m", "BUY?", 10.2)
        self.assertEqual(eng.load()["positions"]["FIVDCAUSDT"]["leg_count"], 1)
        eng.on_signal("FIVDCAUSDT", "5m", "BUY?", 10.3)
        pos = eng.load()["positions"]["FIVDCAUSDT"]
        self.assertEqual(pos["leg_count"], 2)
        self.assertEqual(pos["dca_signal_count"], 3)
        self.assertFalse(pos["faded"])

    def test_dca_signal_count_persists_and_existing_starts_at_zero(self):
        self.eng.on_signal("PERSUSDT", "15m", "BUY?", 10.0)
        self.eng.on_signal("PERSUSDT", "15m", "BUY?", 10.1)
        path = self.tmp / "paper_state_unconfirmed.json"
        st = self.eng.load()
        self.assertEqual(st["positions"]["PERSUSDT"]["dca_signal_count"], 1)
        self.assertEqual(st["dca_signal_every"], 3)
        # Reload a fresh engine against the same files (restart)
        eng2 = _engine(self.tmp)
        eng2.on_signal("PERSUSDT", "15m", "BUY?", 10.2)
        self.assertEqual(eng2.load()["positions"]["PERSUSDT"]["dca_signal_count"], 2)
        self.assertEqual(eng2.load()["positions"]["PERSUSDT"]["leg_count"], 1)
        eng2.on_signal("PERSUSDT", "15m", "BUY?", 10.3)
        self.assertEqual(eng2.load()["positions"]["PERSUSDT"]["leg_count"], 2)
        # A position saved without a counter starts at 0
        st = eng2.load()
        raw = {
            "symbol": "OLDCTAUSDT",
            "timeframe": "15m",
            "side": "LONG",
            "qty": 5.0,
            "entry": 10.0,
            "notional": 50.0,
            "margin": 50.0,
            "entry_fee": 0.025,
            "first_notional": 50.0,
            "leg_count": 1,
            "opened_at_utc": "2026-11-18T00:00:00Z",
            "opened_at_wib": "2026-11-18 07:00:00 Asia/Jakarta",
        }
        st["positions"]["OLDCTAUSDT"] = raw
        eng2.save(st)
        eng2.on_signal("OLDCTAUSDT", "15m", "BUY?", 10.05)
        pos = eng2.load()["positions"]["OLDCTAUSDT"]
        self.assertEqual(pos["dca_signal_count"], 1)
        self.assertEqual(pos["leg_count"], 1)

    def test_dca_every_one_adds_on_each_signal(self):
        eng = _engine(self.tmp, dca_signal_every=1, active_timeframe="15m")
        eng.on_signal("EACHUSDT", "15m", "BUY?", 10.0)
        eng.on_signal("EACHUSDT", "15m", "BUY?", 10.1)
        pos = eng.load()["positions"]["EACHUSDT"]
        self.assertEqual(pos["leg_count"], 2)
        self.assertEqual(pos["dca_signal_count"], 1)


class ActiveTimeframeAndOnePerCoinTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _eng(self, **kwargs):
        return _engine(self.tmp, **kwargs)

    def test_load_active_timeframe_from_yaml(self):
        cfg = self.tmp / "config.yaml"
        cfg.write_text("paper:\n  active_timeframe: 5m\n", encoding="utf-8")
        self.assertEqual(pe.load_active_timeframe(cfg), "5m")
        cfg.write_text("paper_active_timeframe: 1h\n", encoding="utf-8")
        self.assertEqual(pe.load_active_timeframe(cfg), "1h")
        cfg.write_text("symbols: [AKEUSDT]\n", encoding="utf-8")
        self.assertEqual(pe.load_active_timeframe(cfg), "15m")
        self.assertEqual(pe.load_active_timeframe(self.tmp / "missing.yaml"), "15m")

    def test_non_active_timeframe_signal_does_not_trade(self):
        eng = self._eng(active_timeframe="15m")
        eng.on_signal("AAAUSDT", "5m", "BUY?", 10.0)
        st = eng.load()
        self.assertEqual(st["positions"], {})
        self.assertEqual(_trades(eng), [])

    def test_one_position_per_coin_and_opposite_flips(self):
        eng = self._eng(active_timeframe="15m")
        eng.on_signal("BBBUSDT", "15m", "BUY?", 10.0)
        st = eng.load()
        self.assertEqual(list(st["positions"]), ["BBBUSDT"])
        self.assertEqual(st["positions"]["BBBUSDT"]["side"], "LONG")
        # 5m opposite is ignored (not the active TF)
        eng.on_signal("BBBUSDT", "5m", "SELL?", 9.0)
        st = eng.load()
        self.assertEqual(st["positions"]["BBBUSDT"]["side"], "LONG")
        self.assertEqual(st["closed_count"], 0)
        # 15m opposite closes and flips
        eng.on_signal("BBBUSDT", "15m", "SELL?", 11.0)
        st = eng.load()
        pos = st["positions"]["BBBUSDT"]
        self.assertEqual(pos["side"], "SHORT")
        self.assertEqual(pos["entry"], 11.0)
        self.assertEqual(pos["leg_count"], 1)
        self.assertEqual(st["closed_count"], 1)
        close = [t for t in _trades(eng) if t["action"] == "CLOSE"][0]
        self.assertEqual(close["reason"], "opposite_signal")

    def test_5m_fade_does_not_mark_15m_position(self):
        eng = self._eng(active_timeframe="15m")
        eng.on_signal("CCCUSDT", "15m", "BUY?", 10.0)
        eng.close_repainted("CCCUSDT", "5m", 9.5)
        pos = eng.load()["positions"]["CCCUSDT"]
        self.assertFalse(pos["faded"])
        self.assertEqual(pos["fade_count"], 0)

    def test_15m_fade_marks_but_does_not_close(self):
        eng = self._eng(active_timeframe="15m")
        eng.on_signal("DDDUSDT", "15m", "BUY?", 10.0)
        eng.close_repainted("DDDUSDT", "15m", 9.5)
        st = eng.load()
        pos = st["positions"]["DDDUSDT"]
        self.assertTrue(pos["faded"])
        self.assertEqual(st["closed_count"], 0)
        self.assertEqual([t["action"] for t in _trades(eng)], ["OPEN", "FADED"])

    def test_migrate_closes_non_active_and_rekeys(self):
        eng = self._eng(active_timeframe="15m")
        st = eng.load()
        st["balance"] = 999.95
        st["fees_paid"] = 0.05
        st["positions"] = {
            "FOOUSDT:5m": {
                "symbol": "FOOUSDT", "timeframe": "5m", "side": "LONG",
                "qty": 5.0, "entry": 10.0, "notional": 50.0, "margin": 50.0,
                "entry_fee": 0.025,
                "opened_at_utc": "2026-11-17T01:00:00Z",
                "opened_at_wib": "2026-11-17 08:00:00 Asia/Jakarta",
            },
            "FOOUSDT:15m": {
                "symbol": "FOOUSDT", "timeframe": "15m", "side": "SHORT",
                "qty": 4.0, "entry": 12.5, "notional": 50.0, "margin": 50.0,
                "entry_fee": 0.025,
                "opened_at_utc": "2026-11-17T02:00:00Z",
                "opened_at_wib": "2026-11-17 09:00:00 Asia/Jakarta",
            },
            "BARUSDT:5m": {
                "symbol": "BARUSDT", "timeframe": "5m", "side": "SHORT",
                "qty": 10.0, "entry": 5.0, "notional": 50.0, "margin": 50.0,
                "entry_fee": 0.025,
                "opened_at_utc": "2026-11-17T03:00:00Z",
                "opened_at_wib": "2026-11-17 10:00:00 Asia/Jakarta",
            },
        }
        eng.save(st)
        recs = eng.migrate_to_active_timeframe({"FOOUSDT": 11.0, "BARUSDT": 4.0})
        self.assertEqual(len(recs), 2)
        self.assertTrue(all(r["reason"] == "timeframe_off" for r in recs))
        reasons = {(r["symbol"], r["timeframe"], r["side"]): r for r in recs}
        self.assertIn(("FOOUSDT", "5m", "LONG"), reasons)
        self.assertIn(("BARUSDT", "5m", "SHORT"), reasons)
        # LONG FOO 10 -> 11: gross +5, minus fees
        foo = reasons[("FOOUSDT", "5m", "LONG")]
        self.assertAlmostEqual(foo["gross_pnl"], (11.0 - 10.0) * 5.0)
        st = eng.load()
        self.assertEqual(set(st["positions"]), {"FOOUSDT"})
        pos = st["positions"]["FOOUSDT"]
        self.assertEqual(pos["side"], "SHORT")
        self.assertEqual(pos["timeframe"], "15m")
        self.assertEqual(st["closed_count"], 2)
        # idempotent
        recs2 = eng.migrate_to_active_timeframe({"FOOUSDT": 11.0, "BARUSDT": 4.0})
        self.assertEqual(recs2, [])
        self.assertEqual(set(eng.load()["positions"]), {"FOOUSDT"})





class CompoundingSizingTests(unittest.TestCase):
    def test_compound_sizes_from_balance(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        eng = _engine(tmp)
        st = _orig_default_state()
        st["balance"] = 1200.0
        self.assertEqual(eng._position_notional(st), 60.0)
        st["sizing"] = "fixed"
        self.assertEqual(eng._position_notional(st), 50.0)




class TakeProfitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _eng(self, **kwargs):
        return _engine(self.tmp, take_profit_usd=kwargs.pop("take_profit_usd", 2.0), **kwargs)

    def test_load_take_profit_usd_from_yaml(self):
        cfg = self.tmp / "config.yaml"
        cfg.write_text("paper:\n  take_profit_usd: 3.5\n", encoding="utf-8")
        self.assertEqual(pe.load_take_profit_usd(cfg), 3.5)
        cfg.write_text("take_profit_usd: 1\n", encoding="utf-8")
        self.assertEqual(pe.load_take_profit_usd(cfg), 1.0)
        cfg.write_text("paper:\n  take_profit_usd: 0\n", encoding="utf-8")
        self.assertEqual(pe.load_take_profit_usd(cfg), 0.0)
        cfg.write_text("symbols: [AKEUSDT]\n", encoding="utf-8")
        self.assertEqual(pe.load_take_profit_usd(cfg), 2.0)
        self.assertEqual(pe.load_take_profit_usd(self.tmp / "missing.yaml"), 2.0)

    def test_tp_closes_long_at_exact_price_no_flip(self):
        eng = self._eng(active_timeframe="5m")
        eng.on_signal("TPLONGUSDT", "5m", "BUY?", 10.0)
        st = eng.load()
        pos = st["positions"]["TPLONGUSDT"]
        tp = eng._take_profit_price(st, pos)
        self.assertIsNotNone(tp)
        self.assertAlmostEqual(eng._unrealized_pnl(st, pos, tp), 2.0, places=9)
        # Live last still below TP: stays open
        recs = eng.on_price("TPLONGUSDT", last=10.2, high=10.2, low=9.9, bar_open_ts=2_000_000_000)
        self.assertEqual(recs, [])
        self.assertIn("TPLONGUSDT", eng.load()["positions"])
        # Last reaches TP: close at exact TP, no new position
        recs = eng.on_price("TPLONGUSDT", last=tp + 0.05, high=tp + 0.05, low=10.0, bar_open_ts=2_000_000_000)
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["reason"], "take_profit")
        self.assertAlmostEqual(rec["exit"], tp)
        self.assertAlmostEqual(rec["pnl"], 2.0, places=9)
        st = eng.load()
        self.assertNotIn("TPLONGUSDT", st["positions"])
        self.assertEqual(st["closed_count"], 1)
        self.assertEqual(st["win_count"], 1)
        self.assertEqual(st["take_profit_usd"], 2.0)
        closes = [t for t in _trades(eng) if t["action"] == "CLOSE"]
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]["reason"], "take_profit")

    def test_tp_closes_short_at_exact_price_no_flip(self):
        eng = self._eng(active_timeframe="5m")
        eng.on_signal("TPSHORTUSDT", "5m", "SELL?", 10.0)
        st = eng.load()
        pos = st["positions"]["TPSHORTUSDT"]
        tp = eng._take_profit_price(st, pos)
        self.assertLess(tp, 10.0)
        self.assertAlmostEqual(eng._unrealized_pnl(st, pos, tp), 2.0, places=9)
        recs = eng.on_price("TPSHORTUSDT", last=tp, low=tp, high=10.0, bar_open_ts=2_000_000_000)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["reason"], "take_profit")
        self.assertAlmostEqual(recs[0]["exit"], tp)
        self.assertAlmostEqual(recs[0]["pnl"], 2.0, places=9)
        self.assertNotIn("TPSHORTUSDT", eng.load()["positions"])

    def test_tp_with_dca_legs_uses_avg_entry_and_total_qty(self):
        eng = self._eng(active_timeframe="15m", dca_every_signal_timeframes=["15m"], dca_signal_every=1)
        # Small same-side adds so the stack is not already past +$2 at the add price
        eng.on_signal("TPDCAUSDT", "15m", "BUY?", 10.0)
        eng.on_signal("TPDCAUSDT", "15m", "BUY?", 10.02)
        st = eng.load()
        pos = st["positions"]["TPDCAUSDT"]
        self.assertEqual(pos["leg_count"], 2)
        qty = float(pos["qty"])
        entry = float(pos["entry"])
        entry_fee = float(pos["entry_fee"])
        self.assertAlmostEqual(qty, 50.0 / 10.0 + 50.0 / 10.02)
        tp = eng._take_profit_price(st, pos)
        self.assertAlmostEqual(eng._unrealized_pnl(st, pos, tp), 2.0, places=9)
        # Formula must use the blended entry and total qty
        fee_rate = 0.0005
        expected = (2.0 + entry_fee + entry * qty) / (qty * (1.0 - fee_rate))
        self.assertAlmostEqual(tp, expected)
        recs = eng.check_take_profits({"TPDCAUSDT": tp})
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["reason"], "take_profit")
        self.assertEqual(recs[0]["leg_count"], 2)
        self.assertAlmostEqual(recs[0]["pnl"], 2.0, places=9)
        self.assertAlmostEqual(recs[0]["exit"], tp)
        self.assertNotIn("TPDCAUSDT", eng.load()["positions"])

    def test_opposite_signal_still_flips_when_tp_not_hit(self):
        eng = self._eng(active_timeframe="5m")
        eng.on_signal("FLIPUSDT", "5m", "BUY?", 10.0)
        # 0.5% up is well under the ~4% needed for +$2 on a $50 long
        recs = eng.on_price("FLIPUSDT", last=10.05, high=10.05, low=9.99, bar_open_ts=2_000_000_000)
        self.assertEqual(recs, [])
        self.assertIn("FLIPUSDT", eng.load()["positions"])
        self.assertEqual(eng.load()["positions"]["FLIPUSDT"]["side"], "LONG")
        eng.on_signal("FLIPUSDT", "5m", "SELL?", 10.05)
        st = eng.load()
        close = [t for t in _trades(eng) if t["action"] == "CLOSE"][0]
        self.assertEqual(close["reason"], "opposite_signal")
        pos = st["positions"]["FLIPUSDT"]
        self.assertEqual(pos["side"], "SHORT")
        self.assertEqual(pos["entry"], 10.05)
        self.assertEqual(pos["leg_count"], 1)
        self.assertEqual(st["closed_count"], 1)

    def test_tp_uses_post_entry_wick_but_ignores_pre_entry_high(self):
        eng = self._eng(active_timeframe="5m")
        eng.on_signal("WICKUSDT", "5m", "BUY?", 10.0)
        st = eng.load()
        pos = st["positions"]["WICKUSDT"]
        opened = pe._parse_utc(pos["opened_at_utc"]).timestamp()
        tp = eng._take_profit_price(st, pos)
        # Same bar as the fill (open before the last fill): a pre-entry high must not TP
        recs = eng.on_price(
            "WICKUSDT", high=tp + 1, low=9.9, last=10.01, bar_open_ts=opened - 60,
        )
        self.assertEqual(recs, [])
        self.assertIn("WICKUSDT", eng.load()["positions"])
        # A later bar: the high reaching TP fills at the exact TP price, even if close is below
        recs = eng.on_price(
            "WICKUSDT", high=tp + 0.2, low=10.0, last=10.05, bar_open_ts=opened + 300,
        )
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["reason"], "take_profit")
        self.assertAlmostEqual(recs[0]["exit"], tp)
        self.assertAlmostEqual(recs[0]["pnl"], 2.0, places=9)
        self.assertNotIn("WICKUSDT", eng.load()["positions"])

    def test_tp_disabled_when_zero(self):
        eng = self._eng(active_timeframe="5m", take_profit_usd=0)
        eng.on_signal("OFFUSDT", "5m", "BUY?", 10.0)
        recs = eng.on_price("OFFUSDT", last=20.0, high=20.0, low=10.0, bar_open_ts=2_000_000_000)
        self.assertEqual(recs, [])
        self.assertIn("OFFUSDT", eng.load()["positions"])
        self.assertEqual(eng.check_take_profits({"OFFUSDT": 20.0}), [])

    def test_existing_open_position_is_closed_by_mark_sweep(self):
        # Positions already open when TP is enabled must also be eligible.
        eng = self._eng(active_timeframe="5m", take_profit_usd=2.0)
        st = eng.load()
        st["balance"] = 999.975
        st["fees_paid"] = 0.025
        st["positions"]["OLDUSDT"] = {
            "symbol": "OLDUSDT",
            "timeframe": "5m",
            "side": "SHORT",
            "qty": 5.0,
            "entry": 10.0,
            "notional": 50.0,
            "margin": 50.0,
            "entry_fee": 0.025,
            "opened_at_utc": "2026-11-18T00:00:00Z",
            "opened_at_wib": "2026-11-18 07:00:00 Asia/Jakarta",
        }
        eng.save(st)
        pos = eng.load()["positions"]["OLDUSDT"]
        tp = eng._take_profit_price(eng.load(), pos)
        recs = eng.check_take_profits({"OLDUSDT": tp + 0.001})  # short: above TP is not yet filled
        self.assertEqual(recs, [])
        recs = eng.check_take_profits({"OLDUSDT": tp})
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["reason"], "take_profit")
        self.assertAlmostEqual(recs[0]["pnl"], 2.0, places=9)
        self.assertNotIn("OLDUSDT", eng.load()["positions"])


if __name__ == "__main__":
    unittest.main()
