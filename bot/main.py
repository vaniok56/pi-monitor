"""
pi-control-bot entry point.

Starts:
  • PTB Application (long-polling, local Bot API)
  • Docker events monitor thread
  • Host watchdog thread
  • Log loop manager (one thread per container)
  • Alert queue consumer (async task)

All alert sources put AlertItems into the notifier queue; a single async
consumer sends them via Telegram.
"""
from __future__ import annotations

import asyncio
import logging

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
)
from config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def _post_init(
    application: Application,
    notifier: Notifier,
    watchdog: HostWatchdog,
    log_loop_manager: LogLoopManager,
) -> None:
    """Called after the bot is fully initialised but before polling starts."""
    loop = asyncio.get_running_loop()

    # Initialise the thread-safe notifier queue
    notifier_module.init(loop)

    # Start background threads
    DockerEventsMonitor().start()
    watchdog.start()
    log_loop_manager.start()

    # Start the alert consumer coroutine
    asyncio.create_task(consume_alerts(notifier))

    logger.info("All subsystems started.")


def main() -> None:
    cfg = Config.from_env()

    notifier = Notifier(
        bot=None,  # set below after Application is built
        allowed_users=set(cfg.allowed_users),
        cooldown_minutes=cfg.alert_cooldown_minutes,
    )
    watchdog = HostWatchdog(
        disk_pct=cfg.disk_threshold_pct,
        ram_pct=cfg.ram_threshold_pct,
        swap_pct=cfg.swap_threshold_pct,
        cpu_load=cfg.cpu_load_threshold,
        temp_c=cfg.temp_threshold_c,
    )
    log_loop_manager = LogLoopManager()

    _allowed = set(cfg.allowed_users)

    def _mk_cmd(fn, **kw):
        async def _handler(update, context):
            return await fn(update, context, **kw)
        return _handler

    async def _on_startup(app: Application) -> None:
        notifier.bot = app.bot
        await _post_init(app, notifier, watchdog, log_loop_manager)

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
            notifier=notifier,
            watchdog=watchdog,
            log_loop_manager=log_loop_manager,
        )
    ))

    logger.info(
        "Starting pi-control-bot (allowed users: %s, base_url: %s)",
        cfg.allowed_users,
        cfg.base_url,
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
