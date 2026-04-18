from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from plugins._loader import _load_one  # noqa: E402
from plugins._registry import ActionRegistry, ButtonRegistry  # noqa: E402


class PluginLoaderContextTests(unittest.TestCase):
    def test_plugin_handler_receives_plugin_specific_cfg(self) -> None:
        seen: dict[str, dict] = {}

        async def _plugin_handler(query, parts, ctx) -> None:
            seen["plugin_cfg"] = dict(ctx.plugin_cfg)

        def _register(ctx) -> None:
            ctx.actions.register("p.test_plugin", _plugin_handler)

        fake_module = types.SimpleNamespace(register=_register)

        base_ctx = SimpleNamespace(
            app=None,
            notifier=None,
            watchdog=None,
            log_loop_manager=None,
            cfg=SimpleNamespace(),
            scheduler=None,
            actions=ActionRegistry(),
            buttons=ButtonRegistry(),
            host_class="debian_amd64",
            host_label="test-host",
            host_capabilities={},
            mute_store=None,
        )

        plugin_cfg = {"containers": ["stremio-server"], "time": "04:00"}

        with patch("plugins._loader.importlib.import_module", return_value=fake_module):
            _load_one("test_plugin", plugin_cfg, base_ctx)

        handler = base_ctx.actions.get("p.test_plugin")
        self.assertIsNotNone(handler)

        asyncio.run(handler(None, ["p.test_plugin", "menu"], base_ctx))
        self.assertEqual(seen["plugin_cfg"], plugin_cfg)


if __name__ == "__main__":
    unittest.main()
