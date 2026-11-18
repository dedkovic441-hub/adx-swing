#!/usr/bin/env python3
"""Tests for dashboard trade-settings validation and config apply."""
from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import settings_poller as sp  # noqa: E402
import paper_engine as pe  # noqa: E402


SAMPLE = textwrap.dedent(
    """\
    # comment stays
    symbols:
      - FOOUSDT
    timeframes:
      - 5m
      - 15m
    sensitivity: medium
    paper:
      active_timeframe: 5m
      take_profit_usd: 2
      leverage: 3
      cooldown_minutes: 3
    unconfirmed:
      max_add_positions: 4
      dca_signal_every: 3
      dca_every_signal_timeframes:
        - 15m
        - 5m
    """
)

LIVE = {
    "active_timeframe": "5m",
    "take_profit_usd": 2,
    "cooldown_minutes": 3,
    "leverage": 3,
    "size_percent": 5,
    "dca_signal_every": 3,
    "max_add_positions": 4,
}


class ValidateSettingsTests(unittest.TestCase):
    def test_accepts_current_live_values(self):
        got = sp.validate_settings(LIVE)
        self.assertEqual(got["active_timeframe"], "5m")
        self.assertEqual(got["take_profit_usd"], 2.0)
        self.assertEqual(got["cooldown_minutes"], 3.0)
        self.assertEqual(got["leverage"], 3.0)
        self.assertEqual(got["size_percent"], 5.0)
        self.assertEqual(got["dca_signal_every"], 3)
        self.assertEqual(got["max_add_positions"], 4)

    def test_accepts_aliases_and_fraction_size(self):
        got = sp.validate_settings({
            "timeframe": "1m",
            "tp": 10,
            "cooldown": 0,
            "leverage": 20,
            "size_pct": 0.05,
            "dca_every": 1,
            "max_adds": 0,
        })
        self.assertEqual(got["active_timeframe"], "1m")
        self.assertEqual(got["take_profit_usd"], 10.0)
        self.assertEqual(got["cooldown_minutes"], 0.0)
        self.assertEqual(got["leverage"], 20.0)
        self.assertEqual(got["size_percent"], 5.0)
        self.assertEqual(got["dca_signal_every"], 1)
        self.assertEqual(got["max_add_positions"], 0)

    def test_size_pct_as_percent(self):
        raw = {k: v for k, v in LIVE.items() if k != "size_percent"}
        raw["size_pct"] = 12.5
        got = sp.validate_settings(raw)
        self.assertEqual(got["size_percent"], 12.5)

    def test_rejects_bad_timeframe(self):
        with self.assertRaises(sp.SettingsError):
            sp.validate_settings({**LIVE, "active_timeframe": "1h"})

    def test_rejects_out_of_range(self):
        with self.assertRaises(sp.SettingsError):
            sp.validate_settings({**LIVE, "take_profit_usd": 101})
        with self.assertRaises(sp.SettingsError):
            sp.validate_settings({**LIVE, "cooldown_minutes": 241})
        with self.assertRaises(sp.SettingsError):
            sp.validate_settings({**LIVE, "leverage": 0.5})
        with self.assertRaises(sp.SettingsError):
            sp.validate_settings({**LIVE, "size_percent": 0.4})
        with self.assertRaises(sp.SettingsError):
            sp.validate_settings({**LIVE, "size_percent": 51})
        with self.assertRaises(sp.SettingsError):
            sp.validate_settings({**LIVE, "dca_signal_every": 0})
        with self.assertRaises(sp.SettingsError):
            sp.validate_settings({**LIVE, "max_add_positions": 11})

    def test_settings_equal_ignores_float_noise(self):
        self.assertTrue(sp.settings_equal(LIVE, {**LIVE, "take_profit_usd": 2.0, "size_pct": 0.05}))
        self.assertFalse(sp.settings_equal(LIVE, {**LIVE, "active_timeframe": "15m"}))


class ApplyConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "config.yaml"
        self.cfg.write_text(SAMPLE, encoding="utf-8")

    def test_apply_writes_keys_and_adds_1m_lists(self):
        changed = sp.apply_settings_to_config(self.cfg, {
            "active_timeframe": "1m",
            "take_profit_usd": 4,
            "cooldown_minutes": 10,
            "leverage": 5,
            "size_percent": 8,
            "dca_signal_every": 2,
            "max_add_positions": 3,
        })
        self.assertTrue(changed)
        text = self.cfg.read_text(encoding="utf-8")
        self.assertIn("# comment stays", text)
        self.assertIn("FOOUSDT", text)
        self.assertEqual(pe.load_active_timeframe(self.cfg), "1m")
        self.assertEqual(pe.load_take_profit_usd(self.cfg), 4.0)
        self.assertEqual(pe.load_cooldown_minutes(self.cfg), 10.0)
        self.assertEqual(pe.load_leverage(self.cfg), 5.0)
        self.assertAlmostEqual(pe.load_size_pct(self.cfg), 0.08)
        self.assertEqual(pe.load_unconfirmed_dca_signal_every(self.cfg), 2)
        self.assertEqual(pe.load_unconfirmed_max_add(self.cfg), 3)
        self.assertEqual(pe.load_unconfirmed_dca_timeframes(self.cfg), ["1m", "5m", "15m"])
        import yaml
        self.assertEqual(yaml.safe_load(text)["timeframes"], ["1m", "5m", "15m"])

    def test_apply_same_values_still_adds_1m_first_time_then_no_change(self):
        # First apply of the live values adds 1m + size_pct, so the file changes.
        first = sp.apply_settings_to_config(self.cfg, LIVE)
        self.assertTrue(first)
        # Second apply of the same values must not rewrite.
        second = sp.apply_settings_to_config(self.cfg, LIVE)
        self.assertFalse(second)
        before = self.cfg.read_text(encoding="utf-8")
        third = sp.apply_settings_to_config(self.cfg, {**LIVE, "size_pct": 0.05})
        self.assertFalse(third)
        self.assertEqual(self.cfg.read_text(encoding="utf-8"), before)

    def test_current_from_config_matches_live_after_size_pct_added(self):
        sp.apply_settings_to_config(self.cfg, LIVE)
        self.assertTrue(sp.settings_equal(sp.current_settings_from_config(self.cfg), LIVE))

    def test_parse_issue_body_json_fence(self):
        body = "Please apply:\n\n```json\n" + __import__("json").dumps(LIVE) + "\n```\n"
        got = sp.parse_issue_body(body)
        self.assertEqual(got["active_timeframe"], "5m")
        self.assertEqual(got["size_percent"], 5.0)

    def test_parse_issue_body_rejects_garbage(self):
        with self.assertRaises(sp.SettingsError):
            sp.parse_issue_body("please set leverage to 3")


class SizePctLoaderTests(unittest.TestCase):
    def test_fraction_and_percent_in_yaml(self):
        tmp = Path(tempfile.mkdtemp()) / "c.yaml"
        tmp.write_text("paper:\n  size_pct: 0.05\n", encoding="utf-8")
        self.assertAlmostEqual(pe.load_size_pct(tmp), 0.05)
        tmp.write_text("paper:\n  size_pct: 8\n", encoding="utf-8")
        self.assertAlmostEqual(pe.load_size_pct(tmp), 0.08)
        self.assertEqual(pe.size_pct_to_percent(0.05), 5.0)
        self.assertEqual(pe.load_size_pct(tmp.parent / "missing.yaml"), 0.05)

    def test_engine_uses_configured_size(self):
        tmp = Path(tempfile.mkdtemp())
        eng = pe.PaperEngine(
            state_path=tmp / "s.json",
            trades_path=tmp / "t.jsonl",
            lock_path=tmp / "l.lock",
            leverage=1.0,
            cooldown_minutes=0,
            size_pct=0.10,
            active_timeframe="5m",
        )
        st = pe.default_state()
        st["sizing"] = "compound"
        st["balance"] = 1000.0
        self.assertAlmostEqual(eng._position_margin(st), 100.0)
        self.assertAlmostEqual(eng._position_notional(st), 100.0)


if __name__ == "__main__":
    unittest.main()
