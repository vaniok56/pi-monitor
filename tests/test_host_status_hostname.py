from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from alerts.host import HostWatchdog  # noqa: E402


def _fake_stats() -> dict:
    return {
        "mem": SimpleNamespace(used=1_000_000_000, total=2_000_000_000, percent=50.0),
        "swap": SimpleNamespace(used=0, total=0, percent=0.0),
        "disk": SimpleNamespace(used=3_000_000_000, total=6_000_000_000, percent=50.0, free=3_000_000_000),
        "load": (0.1, 0.2, 0.3),
        "temp": None,
        "uptime_secs": 3600,
    }


class HostStatusHostnameTests(unittest.TestCase):
    def test_host_status_prefers_watchdog_host_label(self) -> None:
        watchdog = HostWatchdog(disk_pct=90, ram_pct=90, swap_pct=80, cpu_load=3.0, temp_c=75)
        watchdog.host_label = "raspi4b"

        with patch("alerts.host.get_host_stats_sync", return_value=_fake_stats()), patch(
            "alerts.host._get_device_name", return_value="aarch64"
        ):
            text = watchdog.host_status_text()

        first_line = text.splitlines()[0]
        self.assertIn("<b>raspi4b</b>", first_line)


if __name__ == "__main__":
    unittest.main()
