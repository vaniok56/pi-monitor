from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import plugins.docker_prune as docker_prune  # noqa: E402
import plugins.midnight_restarter as midnight_restarter  # noqa: E402
import plugins.rpi_throttle_watch as rpi_throttle_watch  # noqa: E402


class _FakeActions:
    def __init__(self) -> None:
        self._handlers: dict[str, object] = {}

    def register(self, action: str, handler) -> None:
        self._handlers[action] = handler


class _FakeButtons:
    def __init__(self) -> None:
        self._buttons: list[tuple[str, str, int]] = []

    def add(self, label: str, callback_data: str, sort_key: int = 100) -> None:
        self._buttons.append((label, callback_data, sort_key))


class _FakeScheduler:
    def __init__(self) -> None:
        self.cron_calls: list[tuple[str, str]] = []
        self.daily_calls: list[tuple[str, str]] = []
        self.every_calls: list[tuple[int, str]] = []

    def cron(self, expr: str, callback, name: str) -> None:
        self.cron_calls.append((expr, name))

    def daily_at(self, hh_mm: str, callback, name: str) -> None:
        self.daily_calls.append((hh_mm, name))

    def every(self, interval: int, callback, name: str) -> None:
        self.every_calls.append((interval, name))


class _FakeCtx:
    def __init__(self, plugin_cfg: dict, host_capabilities: dict | None = None) -> None:
        self.plugin_cfg = plugin_cfg
        self.actions = _FakeActions()
        self.buttons = _FakeButtons()
        self.scheduler = _FakeScheduler()
        self.host_capabilities = host_capabilities or {}


class PluginScheduleModeTests(unittest.TestCase):
    def test_docker_prune_without_schedule_is_manual_only(self) -> None:
        ctx = _FakeCtx({"aggressive": False})
        docker_prune.register(ctx)

        self.assertIn("p.docker_prune", ctx.actions._handlers)
        self.assertEqual(len(ctx.scheduler.cron_calls), 0)

    def test_docker_prune_with_schedule_registers_cron(self) -> None:
        ctx = _FakeCtx({"schedule": "0 3 * * 0", "aggressive": False})
        docker_prune.register(ctx)

        self.assertEqual(ctx.scheduler.cron_calls, [("0 3 * * 0", "docker_prune.scheduled")])

    def test_midnight_restarter_without_time_is_manual_only(self) -> None:
        ctx = _FakeCtx({"containers": ["stremio-server"]})
        midnight_restarter.register(ctx)

        self.assertIn("p.midnight_restarter", ctx.actions._handlers)
        self.assertEqual(len(ctx.scheduler.daily_calls), 0)

    def test_rpi_throttle_without_interval_is_manual_only(self) -> None:
        ctx = _FakeCtx({}, host_capabilities={"vcgencmd": True})
        rpi_throttle_watch.register(ctx)

        self.assertIn("p.rpi_throttle_watch", ctx.actions._handlers)
        self.assertEqual(len(ctx.scheduler.every_calls), 0)


if __name__ == "__main__":
    unittest.main()
