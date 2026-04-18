"""
pi-control-bot entry point.

Starts:
  • PTB Application (long-polling, local Bot API)
  • Docker events monitor thread
  • Host watchdog thread
  • Log loop manager (one thread per container)
  • Alert queue consumer (async task)
  • Plugin system (loaded in post_init)

All alert sources put AlertItems into the notifier queue; a single async
consumer sends them via Telegram.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time as _time

from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
)

from alerts.events import DockerEventsMonitor
from alerts.host import HostWatchdog
from alerts.logloop import LogLoopManager
from alerts import notifier as notifier_module
from alerts.notifier import Notifier, consume_alerts
from commands import (
    cmd_help,
    cmd_start,
    cmd_status,
    cmd_testalert,
    handle_callback,
    register_core_actions,
)
from config import Config
import timez
from host_info import detect as detect_host
from mute_store import MuteStore
from plugins import load_plugins
from plugins._ctx import PluginContext
from plugins._registry import ActionRegistry, ButtonRegistry
from plugins._scheduler import Scheduler

logger = logging.getLogger(__name__)


def main() -> None:
    cfg = Config.from_env()

    os.environ["TZ"] = cfg.tz_name
    try:
        _time.tzset()
    except AttributeError:
        pass
    timez.init(cfg.tz, cfg.tz_name)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    host_info = detect_host()
    logger.info(
        "Host detected: class=%s label=%s capabilities=%s",
        host_info.host_class, host_info.host_label, host_info.capabilities,
    )

    mute_store = MuteStore(cfg.mute_store_path)
    notifier = Notifier(
        bot=None,
        allowed_users=set(cfg.allowed_users),
        cooldown_minutes=cfg.alert_cooldown_minutes,
        host_label=host_info.host_label,
        mute_store=mute_store,
    )
    watchdog = HostWatchdog(
        disk_pct=cfg.disk_threshold_pct,
        ram_pct=cfg.ram_threshold_pct,
        swap_pct=cfg.swap_threshold_pct,
        cpu_load=cfg.cpu_load_threshold,
        temp_c=cfg.temp_threshold_c,
        host_label=host_info.host_label,
    )
    log_loop_manager = LogLoopManager()

    _allowed = set(cfg.allowed_users)

    def _mk_cmd(fn, **kw):
        async def _handler(update, context):
            return await fn(update, context, **kw)
        return _handler

    async def _on_startup(app: Application) -> None:
        notifier.bot = app.bot

        loop = asyncio.get_running_loop()
        notifier_module.init(loop)

        DockerEventsMonitor().start()
        watchdog.start()
        log_loop_manager.start()
        asyncio.create_task(consume_alerts(notifier))

        # Build plugin infrastructure
        action_registry = ActionRegistry()
        button_registry = ButtonRegistry()
        scheduler = Scheduler(app.job_queue)

        plugin_ctx = PluginContext(
            app=app,
            notifier=notifier,
            watchdog=watchdog,
            log_loop_manager=log_loop_manager,
            cfg=cfg,
            scheduler=scheduler,
            actions=action_registry,
            buttons=button_registry,
            host_class=host_info.host_class,
            host_label=host_info.host_label,
            host_capabilities=host_info.capabilities,
            plugin_cfg={},
            mute_store=mute_store,
        )

        # Register core actions first, then load plugins (plugins may register too)
        register_core_actions(plugin_ctx)
        load_plugins(plugin_ctx)

        # Register PTB handlers
        app.add_handler(CommandHandler(
            ["start", "menu"],
            _mk_cmd(cmd_start, allowed_users=_allowed),
        ))
        app.add_handler(CommandHandler(
            "status",
            _mk_cmd(cmd_status, allowed_users=_allowed, watchdog=watchdog),
        ))
        app.add_handler(CommandHandler(
            "testalert",
            _mk_cmd(
                cmd_testalert,
                allowed_users=_allowed,
                notifier=notifier,
                log_loop_manager=log_loop_manager,
            ),
        ))
        app.add_handler(CommandHandler(
            "help",
            _mk_cmd(cmd_help, allowed_users=_allowed),
        ))
        app.add_handler(CallbackQueryHandler(
            lambda u, c: handle_callback(
                u, c,
                allowed_users=_allowed,
                plugin_ctx=plugin_ctx,
            )
        ))

        logger.info("All subsystems started.")

    app = (
        ApplicationBuilder()
        .token(cfg.bot_token)
        .base_url(cfg.base_url)
        .base_file_url(cfg.base_file_url)
        .connect_timeout(30)
        .read_timeout(30)
        .post_init(_on_startup)
        .build()
    )

    logger.info(
        "Starting pi-control-bot (allowed users: %s, base_url: %s)",
        cfg.allowed_users,
        cfg.base_url,
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
