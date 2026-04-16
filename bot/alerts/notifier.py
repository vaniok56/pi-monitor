"""
De-duplicating, cooldown-aware Telegram notifier.

All alert sources call put_alert() from any thread. A single async consumer
task (started by main.py) drains the queue and does the actual send.

last_alert is stored in-memory as (container_name, timestamp_str) so the
"Last alert" jump button can navigate directly to the affected container.
"""
from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import Optional, Set, Tuple

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

from alerts import AlertItem

logger = logging.getLogger(__name__)

# Module-level queue; main.py sets it after the event loop starts.
_queue: asyncio.Queue[AlertItem] = None  # type: ignore[assignment]
_loop: asyncio.AbstractEventLoop = None  # type: ignore[assignment]


def init(loop: asyncio.AbstractEventLoop) -> None:
    global _queue, _loop
    _loop = loop
    _queue = asyncio.Queue()


def put_alert(alert: AlertItem) -> None:
    """Thread-safe: put an alert onto the async queue."""
    if _loop is None or _queue is None:
        logger.warning("Notifier not initialised; dropping alert: %s", alert.title)
        return
    _loop.call_soon_threadsafe(_queue.put_nowait, alert)


class Notifier:
    def __init__(
        self,
        bot: Bot,
        allowed_users: Set[int],
        cooldown_minutes: int,
    ) -> None:
        self.bot = bot
        self.allowed_users = allowed_users
        self.cooldown = cooldown_minutes * 60
        self._last_fire: dict[str, float] = {}
        self._ignored_sigs: Set[str] = set()
        # (container_name, human-readable timestamp) — in-memory only
        self.last_alert: Optional[Tuple[str, str]] = None

    # ── Ignore ───────────────────────────────────────────────────────────────

    def ignore_signature(self, sig_hash: str) -> None:
        self._ignored_sigs.add(sig_hash)

    # ── Cooldown check ───────────────────────────────────────────────────────

    def _can_fire(self, key: str) -> bool:
        last = self._last_fire.get(key, 0.0)
        if time.monotonic() - last < self.cooldown:
            return False
        self._last_fire[key] = time.monotonic()
        return True

    # ── Send ─────────────────────────────────────────────────────────────────

    async def dispatch(self, alert: AlertItem) -> None:
        if alert.sig_hash and alert.sig_hash in self._ignored_sigs:
            return
        if not self._can_fire(alert.key):
            return

        # Track last alert for the "Last alert" jump button
        if alert.container:
            from datetime import datetime
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            self.last_alert = (alert.container, ts)

        text = f"<b>{alert.title}</b>\n\n{alert.body}"

        buttons: list[list[InlineKeyboardButton]] = []
        if alert.container and alert.show_container_buttons:
            buttons.append([
                InlineKeyboardButton("🔄 Restart", callback_data=f"restart:{alert.container}"),
                InlineKeyboardButton("🔨 Rebuild", callback_data=f"rebuild:{alert.container}"),
                InlineKeyboardButton("📋 Logs 100", callback_data=f"logs:{alert.container}:100"),
            ])
        if alert.sig_hash:
            buttons.append([
                InlineKeyboardButton("🚫 Ignore signature", callback_data=f"ignore_sig:{alert.sig_hash}"),
            ])

        markup = InlineKeyboardMarkup(buttons) if buttons else None

        for uid in self.allowed_users:
            try:
                if len(text) > 4000:
                    doc = io.BytesIO(text.encode())
                    doc.name = "alert.txt"
                    await self.bot.send_document(
                        uid,
                        document=doc,
                        caption=f"<b>{alert.title}</b> (full detail in file)",
                        parse_mode=ParseMode.HTML,
                        reply_markup=markup,
                    )
                else:
                    await self.bot.send_message(
                        uid,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=markup,
                    )
            except TelegramError as e:
                logger.error("Failed to send alert to %d: %s", uid, e)


async def consume_alerts(notifier: Notifier) -> None:
    """Async task: drain the alert queue indefinitely."""
    global _queue
    while True:
        alert = await _queue.get()
        try:
            await notifier.dispatch(alert)
        except Exception:
            logger.exception("Error dispatching alert: %s", alert)
        finally:
            _queue.task_done()
