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


def _engine(tmp: Path, **kwargs) -> pe.PaperEngine:
    return pe.PaperEngine(
        state_path=tmp / "paper_state_unconfirmed.json",
        trades_path=tmp / "paper_trades_unconfirmed.jsonl",
        lock_path=tmp / "paper_unconfirmed.lock",
        scale_in=True,
        max_add_positions=kwargs.get("max_add_positions", 4),
        dca_every_signal_timeframes=kwargs.get("dca_every_signal_timeframes", ["15m"]),
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
        pos = st["positions"]["AKEUSDT:5m"]
        bal_after_open = st["balance"]
        self.assertEqual(pos["leg_count"], 1)
        self.assertFalse(pos["faded"])

        self.eng.close_repainted("AKEUSDT", "5m", 9.5)
        st = self.eng.load()
        self.assertIn("AKEUSDT:5m", st["positions"])
        pos = st["positions"]["AKEUSDT:5m"]
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
        pos = st["positions"]["FOOUSDT:5m"]
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
        pos = st["positions"]["BARUSDT:5m"]
        self.assertEqual(pos["leg_count"], 1)
        self.assertEqual(pos["notional"], 50.0)
        self.assertEqual([t["action"] for t in _trades(self.eng)], ["OPEN"])

    def test_cap_is_four_additional_legs(self):
        self.eng.on_signal("CAPUSDT", "15m", "SELL?", 20.0)
        for px in (19.0, 18.0, 17.0, 16.0, 15.0):
            self.eng.close_repainted("CAPUSDT", "15m", px + 0.1)
            self.eng.on_signal("CAPUSDT", "15m", "SELL?", px)
        st = self.eng.load()
        pos = st["positions"]["CAPUSDT:15m"]
        self.assertEqual(pos["leg_count"], 5)
        self.assertEqual(pos["notional"], 250.0)
        # Sixth same-direction after another fade is ignored
        self.eng.close_repainted("CAPUSDT", "15m", 14.5)
        self.eng.on_signal("CAPUSDT", "15m", "SELL?", 14.0)
        st = self.eng.load()
        pos = st["positions"]["CAPUSDT:15m"]
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
        pos = st["positions"]["BAZUSDT:5m"]
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
        pos = st["positions"]["BAZUSDT:5m"]
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
        pos = st["positions"]["CASHUSDT:5m"]
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
        pos = st["positions"]["OLDUSDT:5m"]
        self.assertEqual(pos["first_notional"], 50.0)
        self.assertEqual(pos["leg_count"], 2)
        self.assertEqual(len(pos["legs"]), 2)
        self.assertAlmostEqual(pos["qty"], 10.0 + 50.0 / 5.5)

    def test_confirmed_engine_ignores_same_side_and_repaint_still_closes(self):
        conf = pe.PaperEngine(
            state_path=self.tmp / "paper_state.json",
            trades_path=self.tmp / "paper_trades.jsonl",
            lock_path=self.tmp / "paper.lock",
            scale_in=False,
        )
        conf.on_signal("AKEUSDT", "5m", "SELL", 10.0)
        conf.on_signal("AKEUSDT", "5m", "SELL", 11.0)
        st = conf.load()
        pos = st["positions"]["AKEUSDT:5m"]
        self.assertEqual(pos["entry"], 10.0)
        self.assertNotIn("legs", pos)
        conf.close_repainted("AKEUSDT", "5m", 9.0)
        st = conf.load()
        self.assertEqual(st["positions"], {})
        self.assertEqual(st["closed_count"], 1)
        self.assertEqual(_trades(conf)[-1]["action"], "CLOSE")
        self.assertEqual(_trades(conf)[-1]["reason"], "repainted")

    def test_15m_dca_adds_without_fade(self):
        self.eng.on_signal("DCAUSDT", "15m", "SELL?", 10.0)
        self.eng.on_signal("DCAUSDT", "15m", "SELL?", 9.0)
        st = self.eng.load()
        pos = st["positions"]["DCAUSDT:15m"]
        self.assertEqual(pos["leg_count"], 2)
        self.assertEqual(pos["notional"], 100.0)
        self.assertEqual(pos["first_notional"], 50.0)
        self.assertFalse(pos["faded"])
        self.assertAlmostEqual(pos["qty"], 50.0 / 10.0 + 50.0 / 9.0)
        add = [t for t in _trades(self.eng) if t["action"] == "ADD"][0]
        self.assertEqual(add["reason"], "dca")
        self.assertEqual(add["notional"], 50.0)

    def test_15m_dca_uses_first_leg_notional(self):
        st = self.eng.load()
        st["notional"] = 30.0
        self.eng.save(st)
        self.eng.on_signal("DCA30USDT", "15m", "BUY?", 3.0)
        self.eng.on_signal("DCA30USDT", "15m", "BUY?", 4.0)
        pos = self.eng.load()["positions"]["DCA30USDT:15m"]
        self.assertEqual(pos["first_notional"], 30.0)
        self.assertEqual(pos["notional"], 60.0)
        self.assertEqual(pos["leg_count"], 2)

    def test_15m_dca_respects_add_cap(self):
        self.eng.on_signal("CAP15USDT", "15m", "SELL?", 20.0)
        for px in (19.0, 18.0, 17.0, 16.0, 15.0):
            self.eng.on_signal("CAP15USDT", "15m", "SELL?", px)
        pos = self.eng.load()["positions"]["CAP15USDT:15m"]
        self.assertEqual(pos["leg_count"], 5)
        self.assertEqual(pos["notional"], 250.0)
        self.eng.on_signal("CAP15USDT", "15m", "SELL?", 14.0)
        pos = self.eng.load()["positions"]["CAP15USDT:15m"]
        self.assertEqual(pos["leg_count"], 5)
        adds = [t for t in _trades(self.eng) if t["action"] == "ADD"]
        self.assertEqual(len(adds), 4)

    def test_5m_still_requires_fade_even_when_15m_dca_enabled(self):
        self.eng.on_signal("FIVEUSDT", "5m", "BUY?", 10.0)
        self.eng.on_signal("FIVEUSDT", "5m", "BUY?", 11.0)
        pos = self.eng.load()["positions"]["FIVEUSDT:5m"]
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


if __name__ == "__main__":
    unittest.main()
