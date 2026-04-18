from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from plugins._ctx import PluginContext

logger = logging.getLogger(__name__)


def load_plugins(base_ctx: "PluginContext") -> None:
    plugins_yml_path = base_ctx.cfg.plugins_yml_path
    if not plugins_yml_path or not Path(plugins_yml_path).exists():
        logger.info("No plugins.yml at '%s'; no plugins loaded", plugins_yml_path)
        return

    try:
        import yaml
    except ImportError:
        logger.error("PyYAML not installed; cannot load plugins")
        return

    with open(plugins_yml_path) as f:
        config = yaml.safe_load(f) or {}

    enabled = config.get("enabled", {})
    if not enabled:
        logger.info("plugins.yml has no 'enabled' entries; no plugins loaded")
        return

    for plugin_name, plugin_cfg in enabled.items():
        _load_one(plugin_name, plugin_cfg or {}, base_ctx)


def _load_one(name: str, plugin_cfg: dict, base_ctx: "PluginContext") -> None:
    from plugins._ctx import PluginContext
    from plugins._registry import ScopedActionRegistry

    try:
        mod = importlib.import_module(f"plugins.{name}")
    except ImportError as exc:
        logger.warning("Plugin '%s' not found: %s", name, exc)
        return

    meta = getattr(mod, "META", None)
    if meta is not None and meta.requires_platform:
        if base_ctx.host_class not in meta.requires_platform:
            logger.info(
                "Plugin '%s' skipped (requires_platform=%s, host=%s)",
                name, meta.requires_platform, base_ctx.host_class,
            )
            return

    ctx_ref: dict[str, PluginContext] = {}
    scoped_actions = ScopedActionRegistry(base_ctx.actions, lambda: ctx_ref["ctx"])

    ctx = PluginContext(
        app=base_ctx.app,
        notifier=base_ctx.notifier,
        watchdog=base_ctx.watchdog,
        log_loop_manager=base_ctx.log_loop_manager,
        cfg=base_ctx.cfg,
        scheduler=base_ctx.scheduler,
        actions=scoped_actions,
        buttons=base_ctx.buttons,
        host_class=base_ctx.host_class,
        host_label=base_ctx.host_label,
        host_capabilities=base_ctx.host_capabilities,
        plugin_cfg=plugin_cfg,
        mute_store=base_ctx.mute_store,
    )
    ctx_ref["ctx"] = ctx

    try:
        mod.register(ctx)
        logger.info("Plugin '%s' loaded", name)
    except Exception as exc:
        logger.error("Plugin '%s' register() failed: %s", name, exc, exc_info=True)
