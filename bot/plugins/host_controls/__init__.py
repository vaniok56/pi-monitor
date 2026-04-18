from __future__ import annotations

import asyncio
import logging
import os
import shlex
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from plugins._ctx import PluginMeta

if TYPE_CHECKING:
    from plugins._ctx import PluginContext

logger = logging.getLogger(__name__)

META = PluginMeta(
    name="host_controls",
    description="Reboot, shutdown, restart bot, drop caches",
)

_HELPER_LABEL = "com.pi-monitor.internal_helper=true"
_HOST_HELPER_IMAGE = os.environ.get("HOST_CONTROLS_HELPER_IMAGE", "debian:12-slim")


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _controls_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Restart bot", callback_data="p.host_controls:restart_bot"),
            InlineKeyboardButton("💿 Drop caches", callback_data="p.host_controls:drop_caches"),
        ],
        [
            InlineKeyboardButton("🔁 Reboot host", callback_data="p.host_controls:reboot"),
            InlineKeyboardButton("⏹ Shutdown host", callback_data="p.host_controls:shutdown"),
        ],
        [InlineKeyboardButton("◀️ Plugins", callback_data="plugins_menu")],
    ])


async def _run_privileged(cmd: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "docker", "run", "--rm", "--pid=host", "--privileged", "--network=none",
        "--label", _HELPER_LABEL,
        "alpine:latest", *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    return proc.returncode, (stdout + stderr).decode("utf-8", errors="replace").strip()


async def _run_host_ns(script: str, timeout: int = 45) -> tuple[int, str]:
    """Run a shell script in host namespaces via a short-lived helper container."""
    wrapped = (
        "set -e; "
        "if ! command -v nsenter >/dev/null 2>&1; then "
        "echo '__NSENTER_MISSING__: helper image lacks nsenter binary' >&2; exit 127; "
        "fi; "
        f"nsenter -t 1 -m -u -i -n -p -- sh -lc {shlex.quote(script)}"
    )
    proc = await asyncio.create_subprocess_exec(
        "docker", "run", "--rm", "--pid=host", "--privileged", "--network=none",
        "--label", _HELPER_LABEL,
        _HOST_HELPER_IMAGE, "sh", "-lc", wrapped,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode, (stdout + stderr).decode("utf-8", errors="replace").strip()


async def _run_cmd(cmd: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    return proc.returncode, (stdout + stderr).decode("utf-8", errors="replace").strip()


async def _handle_action(query, parts, ctx: "PluginContext") -> None:
    sub = parts[1] if len(parts) > 1 else "menu"

    if sub == "menu":
        await query.edit_message_text(
            "🖥 <b>Host Controls</b>\n\nChoose an action:",
            parse_mode=ParseMode.HTML,
            reply_markup=_controls_keyboard(),
        )

    elif sub == "reboot":
        await query.edit_message_text(
            "⚠️ <b>Reboot host?</b>\n\nThis will reboot the entire machine.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, reboot", callback_data="p.host_controls:reboot_confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="p.host_controls:menu"),
            ]]),
        )

    elif sub == "reboot_confirm":
        await query.edit_message_text("⏳ Rebooting host…")
        rc, out = await _run_host_ns(
            "sync; "
            "if command -v systemctl >/dev/null 2>&1; then "
            "systemctl reboot -i || reboot -f || /sbin/reboot -f || shutdown -r now; "
            "else reboot -f || /sbin/reboot -f || shutdown -r now; fi",
            timeout=60,
        )
        if rc != 0:
            if not (out or "").strip():
                await query.edit_message_text(
                    "⚠️ <b>Reboot command returned no output.</b>\n\n"
                    "Host may already be rebooting. If it stays online, try again and send logs.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("◀️ Back", callback_data="p.host_controls:menu"),
                    ]]),
                )
                return
            await query.edit_message_text(
                f"❌ <b>Reboot failed</b>\n\n<code>{_escape_html((out or 'no output')[:700])}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Back", callback_data="p.host_controls:menu"),
                ]]),
            )

    elif sub == "shutdown":
        await query.edit_message_text(
            "⚠️ <b>Shutdown host?</b>\n\nThis will power off the machine.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, shutdown", callback_data="p.host_controls:shutdown_confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="p.host_controls:menu"),
            ]]),
        )

    elif sub == "shutdown_confirm":
        await query.edit_message_text("⏳ Shutting down host…")
        rc, out = await _run_host_ns(
            "sync; "
            "if command -v systemctl >/dev/null 2>&1; then "
            "systemctl poweroff -i || poweroff -f || /sbin/poweroff -f || shutdown -h now; "
            "else poweroff -f || /sbin/poweroff -f || shutdown -h now; fi",
            timeout=60,
        )
        if rc != 0:
            if not (out or "").strip():
                await query.edit_message_text(
                    "⚠️ <b>Shutdown command returned no output.</b>\n\n"
                    "Host may already be powering off. If it stays online, try again and send logs.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("◀️ Back", callback_data="p.host_controls:menu"),
                    ]]),
                )
                return
            await query.edit_message_text(
                f"❌ <b>Shutdown failed</b>\n\n<code>{_escape_html((out or 'no output')[:700])}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Back", callback_data="p.host_controls:menu"),
                ]]),
            )

    elif sub == "restart_bot":
        container_name = os.environ.get("HOSTNAME", "pi-control-bot")
        await query.edit_message_text(
            f"⚠️ <b>Restart bot?</b>\n\nWill restart container <code>{container_name}</code>.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, restart", callback_data="p.host_controls:restart_bot_confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="p.host_controls:menu"),
            ]]),
        )

    elif sub == "restart_bot_confirm":
        await query.edit_message_text("⏳ Restarting bot…")
        container_name = os.environ.get("HOSTNAME", "pi-control-bot")
        rc, out = await _run_cmd(["docker", "restart", container_name])
        if rc != 0:
            await query.edit_message_text(
                f"❌ Restart failed:\n<code>{out[:500]}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Plugins", callback_data="plugins_menu"),
                ]]),
            )

    elif sub == "drop_caches":
        await query.edit_message_text(
            "⚠️ <b>Drop Linux page caches?</b>\n\n"
            "This is a host-level operation intended for troubleshooting.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, drop caches", callback_data="p.host_controls:drop_caches_confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="p.host_controls:menu"),
            ]]),
        )

    elif sub == "drop_caches_confirm":
        await query.edit_message_text("⏳ Dropping page caches…")
        rc, out = await _run_host_ns(
            "sync; echo 3 > /proc/sys/vm/drop_caches && echo done"
        )
        status = "✅" if rc == 0 else "❌"
        await query.edit_message_text(
            f"{status} <b>Drop caches</b>\n\n<code>{_escape_html((out or 'done')[:500])}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Back", callback_data="p.host_controls:menu"),
            ]]),
        )

def register(ctx: "PluginContext") -> None:
    ctx.actions.register("p.host_controls", _handle_action)
    ctx.buttons.add("🖥 Host controls", "p.host_controls:menu", sort_key=30)
