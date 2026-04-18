from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import logging

ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import timez  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402


def _reset():
    timez._tz = ZoneInfo("UTC")
    timez._tz_name = "UTC"


class TimezInitTests(unittest.TestCase):
    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def test_default_is_utc(self):
        self.assertEqual(timez.tz_label(), "UTC")
        n = timez.now()
        self.assertEqual(str(n.tzinfo), "UTC")

    def test_valid_iana_zone(self):
        tz = ZoneInfo("Europe/Berlin")
        timez.init(tz, "Europe/Berlin")
        self.assertEqual(timez.tz_label(), "Europe/Berlin")
        n = timez.now()
        self.assertEqual(n.tzinfo, tz)

    def test_utcnow_always_utc(self):
        timez.init(ZoneInfo("America/New_York"), "America/New_York")
        u = timez.utcnow()
        self.assertEqual(u.tzinfo, timezone.utc)


class ConfigTzFallbackTests(unittest.TestCase):
    """Config.from_env falls back to UTC on unknown TZ."""

    def test_invalid_tz_falls_back_to_utc(self):
        import os
        env = {
            "BOT_TOKEN": "x",
            "ALLOWED_USER_IDS": "1",
            "DESKTOP_PATH": "/tmp",
            "TZ": "Foo/Bar",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertLogs("config", level=logging.WARNING) as cm:
                from config import Config
                cfg = Config.from_env()
        self.assertEqual(cfg.tz_name, "UTC")
        self.assertTrue(any("Foo/Bar" in line for line in cm.output))

    def test_valid_tz_stored(self):
        import os
        env = {
            "BOT_TOKEN": "x",
            "ALLOWED_USER_IDS": "1",
            "DESKTOP_PATH": "/tmp",
            "TZ": "Europe/Berlin",
        }
        with patch.dict(os.environ, env, clear=True):
            from config import Config
            cfg = Config.from_env()
        self.assertEqual(cfg.tz_name, "Europe/Berlin")
        self.assertEqual(cfg.tz, ZoneInfo("Europe/Berlin"))

    def test_missing_tz_defaults_utc(self):
        import os
        env = {
            "BOT_TOKEN": "x",
            "ALLOWED_USER_IDS": "1",
            "DESKTOP_PATH": "/tmp",
        }
        with patch.dict(os.environ, env, clear=True):
            from config import Config
            cfg = Config.from_env()
        self.assertEqual(cfg.tz_name, "UTC")


class TimezNextCronTests(unittest.TestCase):
    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def test_next_cron_returns_aware_datetime_in_cfg_tz(self):
        tz = ZoneInfo("Europe/Berlin")
        timez.init(tz, "Europe/Berlin")
        nxt = timez.next_cron("0 */4 * * *")
        self.assertIsInstance(nxt, datetime)
        self.assertIsNotNone(nxt.tzinfo)
        # Result should be in cfg tz (Berlin), not UTC
        self.assertEqual(nxt.tzinfo, tz)

    def test_next_cron_utc_returns_utc(self):
        nxt = timez.next_cron("0 */4 * * *")
        self.assertEqual(nxt.tzinfo, ZoneInfo("UTC"))

    def test_next_daily_returns_future_time(self):
        n = timez.now()
        hh_mm = "23:59"
        nxt = timez.next_daily(hh_mm)
        self.assertGreater(nxt, n)

    def test_fmt_converts_to_cfg_tz(self):
        tz = ZoneInfo("Europe/Berlin")
        timez.init(tz, "Europe/Berlin")
        utc_dt = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = timez.fmt(utc_dt, "%H:%M")
        # Berlin is UTC+2 in summer, so 12:00 UTC = 14:00 Berlin
        self.assertEqual(result, "14:00")


if __name__ == "__main__":
    unittest.main()
