from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from alerts.events import DockerEventsMonitor  # noqa: E402


class EventsInternalHelperTests(unittest.TestCase):
    def test_internal_helper_die_event_is_ignored(self) -> None:
        mon = DockerEventsMonitor()
        event = {
            "Type": "container",
            "Action": "die",
            "Actor": {
                "Attributes": {
                    "name": "compassionate_rhodes",
                    "exitCode": "1",
                    "com.pi-monitor.internal_helper": "true",
                }
            },
        }

        with patch("alerts.events.put_alert") as put_alert:
            mon._handle(event)
            put_alert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
