from __future__ import annotations

import asyncio
import logging

import timez
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from plugins._ctx import PluginMeta

if TYPE_CHECKING:
    from plugins._ctx import PluginContext

logger = logging.getLogger(__name__)

META = PluginMeta(
    name="midnight_restarter",
    description="Restart whitelisted containers on a daily schedule",
    default_config={"containers": [], "time": "04:00"},
)

_CB_MENU = "p.midnight_restarter:menu"
_CB_CONFIRM = "p.midnight_restarter:confirm"
_CB_RUN = "p.midnight_restarter:run"
_CB_PLUGINS = "plugins_menu"


async def _restart_containers(containers: list[str]) -> str:
    lines = []
    for name in containers:
        proc = await asyncio.create_subprocess_exec(
            "docker", "restart", name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode == 0:
            lines.append(f"✅ {name} restarted")
        else:
            err = stderr.decode("utf-8", errors="replace").strip()
            lines.append(f"❌ {name}: {err[:100]}")
    return "\n".join(lines) or "No containers configured."


def _next_daily_run(time_str: str) -> str:
    nxt = timez.next_daily(time_str)
    return nxt.strftime(f"%Y-%m-%d %H:%M {timez.tz_label()}")


async def _container_states(containers: list[str]) -> dict[str, str]:
    async def _inspect(name: str) -> tuple[str, str]:
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", "-f", "{{.State.Status}}", name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return name, "missing"
        status = stdout.decode("utf-8", errors="replace").strip() or "unknown"
        return name, status

    pairs = await asyncio.gather(*(_inspect(name) for name in containers), return_exceptions=True)
    states: dict[str, str] = {}
    for item in pairs:
        if isinstance(item, Exception):
            continue
        name, status = item
        states[name] = status
    return states


def _status_icon(status: str) -> str:
    if status == "running":
        return "✅"
    if status in {"exited", "dead", "paused", "restarting"}:
        return "⚠️"
    if status == "missing":
        return "❓"
    return "•"


async def _render_plan(containers: list[str], time_str: str | None) -> str:
    states = await _container_states(containers)
    if time_str:
        schedule_line = (
            f"🤖 Auto restart: <b>enabled</b> at <code>{time_str}</code> {timez.tz_label()}\n"
            f"⏭ Next run: <b>{_next_daily_run(time_str)}</b>"
        )
    else:
        schedule_line = "🤖 Auto restart: <b>disabled</b> (manual-only mode)"

    lines = [
        "🔁 <b>Midnight Restarter Plan</b>",
        "",
        schedule_line,
        "",
        "Containers to restart:",
    ]
    for name in containers:
        status = states.get(name, "unknown")
        lines.append(f"{_status_icon(status)} <code>{name}</code> — {status}")
    return "\n".join(lines)


async def _handle_action(query, parts, ctx: "PluginContext") -> None:
    containers = ctx.plugin_cfg.get("containers", [])
    time_str = ctx.plugin_cfg.get("time")
    sub = parts[1] if len(parts) > 1 else "menu"

    if sub == "menu":
        await query.edit_message_text(
            await _render_plan(containers, str(time_str) if time_str else None),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Restart now", callback_data=_CB_CONFIRM)],
                [
                    InlineKeyboardButton("🔄 Refresh", callback_data=_CB_MENU),
                    InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS),
                ],
            ]),
        )

    elif sub == "confirm":
        names = "\n".join(f"• <code>{c}</code>" for c in containers) or "none"
        await query.edit_message_text(
            "⚠️ <b>Restart these containers now?</b>\n\n"
            f"{names}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, restart", callback_data=_CB_RUN),
                InlineKeyboardButton("❌ Cancel", callback_data=_CB_PLUGINS),
            ]]),
        )

    elif sub == "run":
        names = ", ".join(f"<code>{c}</code>" for c in containers) or "none"
        await query.edit_message_text(f"⏳ Restarting: {names}…", parse_mode=ParseMode.HTML)
        result = await _restart_containers(containers)
        await query.edit_message_text(
            f"🔁 <b>Midnight restarter</b>\n\n{result}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS),
            ]]),
        )


def register(ctx: "PluginContext") -> None:
    containers = ctx.plugin_cfg.get("containers", [])
    time_str = ctx.plugin_cfg.get("time")

    if not containers:
        logger.warning("midnight_restarter: no containers configured, plugin not loaded")
        return

    ctx.actions.register("p.midnight_restarter", _handle_action)
    ctx.buttons.add("🔁 Night restarter", _CB_MENU, sort_key=20)

    async def _scheduled(context) -> None:
        logger.info("midnight_restarter: restarting %s", containers)
        result = await _restart_containers(containers)
        logger.info("midnight_restarter done: %s", result)

    if time_str:
        ctx.scheduler.daily_at(str(time_str), _scheduled, "midnight_restarter.scheduled")
    else:
        logger.info("midnight_restarter: no time configured — manual-only mode")
