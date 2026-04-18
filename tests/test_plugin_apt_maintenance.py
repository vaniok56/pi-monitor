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

import plugins.apt_maintenance as apt_maintenance  # noqa: E402


class _FakeQuery:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def edit_message_text(self, text: str, **kwargs) -> None:
        self.calls.append((text, kwargs))


class _FakeCtx:
    def __init__(self, plugin_cfg: dict) -> None:
        self.plugin_cfg = plugin_cfg


class AptMaintenancePluginTests(unittest.TestCase):
    def _callbacks(self, call_kwargs: dict) -> list[str]:
        markup = call_kwargs.get("reply_markup")
        if markup is None:
            return []
        return [btn.callback_data for row in markup.inline_keyboard for btn in row]

    def test_menu_is_fast_and_shows_action_buttons(self) -> None:
        query = _FakeQuery()
        ctx = _FakeCtx({"max_listed_updates": 5})

        with patch.object(apt_maintenance, "_collect_preview", new=AsyncMock()) as preview_mock:
            asyncio.run(apt_maintenance._handle_action(query, ["p.apt_maintenance", "report"], ctx))
            preview_mock.assert_not_awaited()

        self.assertTrue(query.calls)
        text, kwargs = query.calls[0]
        self.assertIn("APT Maintenance", text)
        self.assertIn("Choose an action", text)
        callbacks = self._callbacks(kwargs)
        self.assertIn("p.apt_maintenance:update_preview", callbacks)
        self.assertIn("p.apt_maintenance:cleanup_confirm", callbacks)
        self.assertIn("plugins_menu", callbacks)

    def test_update_preview_shows_available_updates(self) -> None:
        query = _FakeQuery()
        ctx = _FakeCtx({})
        preview = {
            "unsupported": False,
            "error": None,
            "counts": (1, 0, 0, 0),
            "packages": ["docker-compose-plugin"],
            "listed_packages": ["docker-compose-plugin"],
            "remaining_count": 0,
            "docker_related": ["docker-compose-plugin"],
        }

        with patch.object(apt_maintenance, "_collect_preview", new=AsyncMock(return_value=preview)):
            asyncio.run(apt_maintenance._handle_action(query, ["p.apt_maintenance", "update_preview"], ctx))

        self.assertTrue(query.calls)
        text, kwargs = query.calls[0]
        self.assertIn("APT Update Preview", text)
        self.assertIn("Available updates", text)
        self.assertIn("Docker-related updates detected", text)
        callbacks = self._callbacks(kwargs)
        self.assertIn("p.apt_maintenance:update_run", callbacks)
        self.assertIn("p.apt_maintenance:menu", callbacks)

    def test_update_run_requires_extra_confirmation_for_docker_updates(self) -> None:
        query = _FakeQuery()
        ctx = _FakeCtx({})
        preview = {
            "unsupported": False,
            "error": None,
            "counts": (1, 0, 0, 0),
            "packages": ["docker-compose-plugin"],
            "listed_packages": ["docker-compose-plugin"],
            "remaining_count": 0,
            "docker_related": ["docker-compose-plugin"],
        }

        with patch.object(apt_maintenance, "_collect_preview", new=AsyncMock(return_value=preview)):
            asyncio.run(apt_maintenance._handle_action(query, ["p.apt_maintenance", "update_run"], ctx))

        self.assertTrue(query.calls)
        text, kwargs = query.calls[0]
        self.assertIn("Docker-related updates detected", text)
        callbacks = self._callbacks(kwargs)
        self.assertIn("p.apt_maintenance:update_run_force", callbacks)
        self.assertIn("p.apt_maintenance:menu", callbacks)

    def test_cleanup_confirm_shows_expected_actions(self) -> None:
        query = _FakeQuery()
        ctx = _FakeCtx({})

        asyncio.run(apt_maintenance._handle_action(query, ["p.apt_maintenance", "cleanup_confirm"], ctx))

        self.assertTrue(query.calls)
        text, kwargs = query.calls[0]
        self.assertIn("Run cleanup now", text)
        self.assertIn("apt-get autoremove -y", text)
        callbacks = self._callbacks(kwargs)
        self.assertIn("p.apt_maintenance:cleanup_run", callbacks)
        self.assertIn("p.apt_maintenance:menu", callbacks)


if __name__ == "__main__":
    unittest.main()
