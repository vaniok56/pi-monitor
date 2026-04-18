from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import plugins.docker_prune as docker_prune  # noqa: E402
import plugins.host_controls as host_controls  # noqa: E402
import plugins.midnight_restarter as midnight_restarter  # noqa: E402
import plugins.stremio_cache as stremio_cache  # noqa: E402

try:
    import plugins.cobalt_temp_cleanup as cobalt_temp_cleanup  # noqa: E402
except ModuleNotFoundError:  # pragma: no cover - optional plugin in this workspace snapshot
    cobalt_temp_cleanup = None


class _FakeQuery:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def edit_message_text(self, text: str, **kwargs) -> None:
        self.calls.append((text, kwargs))


class _FakeCtx:
    def __init__(self, plugin_cfg: dict) -> None:
        self.plugin_cfg = plugin_cfg


class PluginBehaviorTests(unittest.TestCase):
    def _callbacks(self, call_kwargs: dict) -> list[str]:
        markup = call_kwargs.get("reply_markup")
        if markup is None:
            return []
        return [btn.callback_data for row in markup.inline_keyboard for btn in row]

    def test_docker_report_is_first_screen(self) -> None:
        query = _FakeQuery()
        ctx = _FakeCtx({"aggressive": False, "schedule": "0 3 * * 0"})
        report_data = ({
            "images": "12.0 GB",
            "containers": "9.0 GB",
            "volumes": "381.0 MB",
            "build_cache": "2.0 GB",
            "reclaimable": "5.0 GB",
        }, None)

        with patch.object(docker_prune, "_docker_df_report", new=AsyncMock(return_value=report_data)):
            asyncio.run(docker_prune._handle_action(query, ["p.docker_prune", "report"], ctx))

        self.assertTrue(query.calls)
        text, kwargs = query.calls[0]
        self.assertIn("Docker Report", text)
        self.assertIn("Auto prune", text)
        self.assertIn("Next run", text)
        callbacks = self._callbacks(kwargs)
        self.assertIn("p.docker_prune:run", callbacks)
        self.assertIn("plugins_menu", callbacks)

    def test_manual_prune_requires_confirmation(self) -> None:
        query = _FakeQuery()
        ctx = _FakeCtx({"aggressive": False})
        with patch.object(docker_prune, "_do_prune", new=AsyncMock(return_value="ok")) as prune:
            asyncio.run(docker_prune._handle_action(query, ["p.docker_prune", "run"], ctx))
            prune.assert_not_awaited()
        self.assertTrue(query.calls)
        first_text, first_kwargs = query.calls[0]
        self.assertIn("Are you sure", first_text)
        callbacks = self._callbacks(first_kwargs)
        self.assertIn("plugins_menu", callbacks)

    def test_midnight_restarter_shows_plan_before_restart(self) -> None:
        query = _FakeQuery()
        ctx = _FakeCtx({"containers": ["stremio-server"], "time": "04:00"})
        with patch.object(midnight_restarter, "_container_states", new=AsyncMock(return_value={"stremio-server": "running"})):
            asyncio.run(midnight_restarter._handle_action(query, ["p.midnight_restarter", "menu"], ctx))

        self.assertTrue(query.calls)
        text, kwargs = query.calls[0]
        self.assertIn("Midnight Restarter Plan", text)
        self.assertIn("Next run", text)
        callbacks = self._callbacks(kwargs)
        self.assertIn("p.midnight_restarter:confirm", callbacks)
        self.assertIn("plugins_menu", callbacks)

    def test_stremio_report_shows_probe_note(self) -> None:
        query = _FakeQuery()
        ctx = _FakeCtx({"container": "stremio-server", "path": "/cache"})

        with patch.object(
            stremio_cache,
            "_probe_cache_size",
            new=AsyncMock(return_value=(0, "Path not found in container")),
        ):
            asyncio.run(stremio_cache._handle_action(query, ["p.stremio_cache", "report"], ctx))

        self.assertTrue(query.calls)
        text, kwargs = query.calls[0]
        self.assertIn("Current size", text)
        self.assertIn("Auto wipe", text)
        self.assertIn("Probe:", text)
        self.assertIn("Path not found", text)
        callbacks = self._callbacks(kwargs)
        self.assertIn("plugins_menu", callbacks)

    def test_cobalt_report_shows_schedule_status(self) -> None:
        if cobalt_temp_cleanup is None:
            self.skipTest("cobalt_temp_cleanup plugin not present in this workspace")

        query = _FakeQuery()
        ctx = _FakeCtx({"path": "/tmp/cobalt", "age_days": 1, "schedule": "0 */6 * * *"})

        with patch.object(cobalt_temp_cleanup, "_estimate_cleanup", return_value=(3, 2048)):
            asyncio.run(cobalt_temp_cleanup._handle_action(query, ["p.cobalt_temp_cleanup", "report"], ctx))

        self.assertTrue(query.calls)
        text, kwargs = query.calls[0]
        self.assertIn("Cobalt Cleanup Report", text)
        self.assertIn("Auto cleanup", text)
        self.assertIn("Next run", text)
        callbacks = self._callbacks(kwargs)
        self.assertIn("p.cobalt_temp_cleanup:confirm", callbacks)
        self.assertIn("plugins_menu", callbacks)

    def test_host_controls_menu_excludes_vacuum_and_fstrim(self) -> None:
        keyboard = host_controls._controls_keyboard()
        callback_data = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]

        self.assertNotIn("p.host_controls:vacuum_journal", callback_data)
        self.assertNotIn("p.host_controls:fstrim", callback_data)
        self.assertIn("p.host_controls:reboot", callback_data)
        self.assertIn("p.host_controls:shutdown", callback_data)
        self.assertIn("plugins_menu", callback_data)

    def test_host_controls_reboot_failure_is_reported(self) -> None:
        query = _FakeQuery()
        ctx = _FakeCtx({})

        with patch.object(host_controls, "_run_host_ns", new=AsyncMock(return_value=(1, "permission denied"))):
            asyncio.run(host_controls._handle_action(query, ["p.host_controls", "reboot_confirm"], ctx))

        self.assertGreaterEqual(len(query.calls), 2)
        text, kwargs = query.calls[-1]
        self.assertIn("Reboot failed", text)
        self.assertIn("permission denied", text)
        callbacks = self._callbacks(kwargs)
        self.assertIn("p.host_controls:menu", callbacks)

    def test_host_controls_reboot_no_output_shows_maybe_rebooting(self) -> None:
        query = _FakeQuery()
        ctx = _FakeCtx({})

        with patch.object(host_controls, "_run_host_ns", new=AsyncMock(return_value=(1, ""))):
            asyncio.run(host_controls._handle_action(query, ["p.host_controls", "reboot_confirm"], ctx))

        self.assertGreaterEqual(len(query.calls), 2)
        text, kwargs = query.calls[-1]
        self.assertIn("returned no output", text)
        self.assertIn("already be rebooting", text)
        callbacks = self._callbacks(kwargs)
        self.assertIn("p.host_controls:menu", callbacks)


if __name__ == "__main__":
    unittest.main()
