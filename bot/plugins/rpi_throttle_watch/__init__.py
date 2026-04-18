from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from plugins._ctx import PluginMeta

if TYPE_CHECKING:
    from plugins._ctx import PluginContext

logger = logging.getLogger(__name__)

META = PluginMeta(
    name="rpi_throttle_watch",
    description="Alert on Pi CPU throttling / under-voltage conditions",
    requires_platform=("rpi",),
    default_config={"interval_seconds": 300},
)

_CB_MENU = "p.rpi_throttle_watch:menu"
_CB_PLUGINS = "plugins_menu"

_ACTIVE_BITS = {
    0: "Under-voltage now",
    1: "ARM frequency capped now",
    2: "Currently throttled",
    3: "Soft temp limit active",
}


def _check_throttle() -> tuple[int, list[str]]:
    """Run vcgencmd and return (raw_int, list_of_active_flag_strings)."""
    result = subprocess.run(
        ["vcgencmd", "get_throttled"],
        capture_output=True, text=True, timeout=5,
    )
    raw = result.stdout.strip()  # e.g. "throttled=0x50000"
    if not raw.startswith("throttled="):
        raise ValueError(f"Unexpected vcgencmd output: {raw!r}")
    hex_val = raw.split("=", 1)[1].strip()
    val = int(hex_val, 16)
    active = [desc for bit, desc in _ACTIVE_BITS.items() if val & (1 << bit)]
    return val, active, hex_val


async def _run_check(context) -> None:
    import asyncio
    from alerts import AlertItem, AlertType
    from alerts.notifier import put_alert

    try:
        val, active_flags, hex_val = await asyncio.to_thread(_check_throttle)
    except Exception as exc:
        logger.warning("rpi_throttle_watch: vcgencmd failed: %s", exc)
        return

    if not active_flags:
        return

    body_lines = [f"<code>vcgencmd get_throttled → {hex_val}</code>", ""]
    body_lines.extend(f"• {f}" for f in active_flags)

    put_alert(AlertItem(
        type=AlertType.HOST_RESOURCE,
        title="⚡ Pi throttling detected",
        body="\n".join(body_lines),
        key=f"rpi_throttle:{hex_val}",
    ))


async def _handle_action(query, parts, ctx: "PluginContext") -> None:
    import asyncio

    sub = parts[1] if len(parts) > 1 else "menu"
    if sub != "menu":
        return

    try:
        _, active_flags, hex_val = await asyncio.to_thread(_check_throttle)
    except Exception as exc:
        await query.edit_message_text(
            f"⚠️ <b>Pi throttle check failed</b>\n\n<code>{exc}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Retry", callback_data=_CB_MENU),
                InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS),
            ]]),
        )
        return

    if active_flags:
        status = ["⚠️ <b>Active flags:</b>"]
        status.extend(f"• {flag}" for flag in active_flags)
    else:
        status = ["✅ <b>No active throttling flags</b>"]

    interval_val = ctx.plugin_cfg.get("interval_seconds")
    auto_line = (
        f"🤖 Auto check: <b>enabled</b> every <b>{int(interval_val)}s</b>"
        if interval_val is not None
        else "🤖 Auto check: <b>disabled</b> (manual-only mode)"
    )

    await query.edit_message_text(
        "⚡ <b>Pi Throttle Report</b>\n\n"
        f"<code>vcgencmd get_throttled → {hex_val}</code>\n"
        f"{auto_line}\n\n"
        + "\n".join(status),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Recheck", callback_data=_CB_MENU),
            InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS),
        ]]),
    )


def register(ctx: "PluginContext") -> None:
    if not ctx.host_capabilities.get("vcgencmd"):
        logger.warning("rpi_throttle_watch: vcgencmd not found — skipping")
        return

    ctx.actions.register("p.rpi_throttle_watch", _handle_action)
    ctx.buttons.add("⚡ Pi throttle", _CB_MENU, sort_key=40)

    interval = ctx.plugin_cfg.get("interval_seconds")
    if interval is not None:
        interval_i = int(interval)
        ctx.scheduler.every(interval_i, _run_check, "rpi_throttle_watch.check")
        logger.info("rpi_throttle_watch: checking every %ds", interval_i)
    else:
        logger.info("rpi_throttle_watch: no interval configured — manual-only mode")
