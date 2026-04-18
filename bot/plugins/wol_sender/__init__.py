from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from plugins._ctx import PluginMeta

if TYPE_CHECKING:
    from plugins._ctx import PluginContext

logger = logging.getLogger(__name__)

META = PluginMeta(
    name="wol_sender",
    description="Send Wake-on-LAN magic packets to configured targets",
    default_config={"targets": []},
)


def _safe_name(name: str) -> str:
    """Sanitise target name for use in callback data."""
    return name.replace(":", "_").replace(" ", "_")[:30]


async def _handle_action(query, parts, ctx: "PluginContext") -> None:
    sub = parts[1] if len(parts) > 1 else ""

    if sub == "menu" or not sub:
        targets = ctx.plugin_cfg.get("targets", [])
        if not targets:
            await query.edit_message_text(
                "No WoL targets configured.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Plugins", callback_data="plugins_menu"),
                ]]),
            )
            return
        kb = []
        row = []
        for t in targets:
            n = _safe_name(t["name"])
            row.append(InlineKeyboardButton(f"💡 Wake {t['name']}", callback_data=f"p.wol_sender:wake:{n}"))
            if len(row) == 2:
                kb.append(row)
                row = []
        if row:
            kb.append(row)
        kb.append([InlineKeyboardButton("◀️ Plugins", callback_data="plugins_menu")])
        await query.edit_message_text(
            "💡 <b>Wake-on-LAN</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb),
        )

    elif sub == "wake":
        safe = parts[2] if len(parts) > 2 else ""
        targets = ctx.plugin_cfg.get("targets", [])
        target = next((t for t in targets if _safe_name(t["name"]) == safe), None)
        if target is None:
            await query.edit_message_text("Target not found.")
            return
        try:
            from wakeonlan import send_magic_packet
            send_magic_packet(target["mac"])
            await query.edit_message_text(
                f"✅ Magic packet sent to <b>{target['name']}</b> (<code>{target['mac']}</code>).",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Back", callback_data="p.wol_sender:menu"),
                ]]),
            )
        except Exception as exc:
            await query.edit_message_text(
                f"❌ WoL failed: <code>{exc}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Back", callback_data="p.wol_sender:menu"),
                ]]),
            )


def register(ctx: "PluginContext") -> None:
    targets = ctx.plugin_cfg.get("targets", [])
    if not targets:
        logger.warning("wol_sender: no targets configured — skipping")
        return

    ctx.actions.register("p.wol_sender", _handle_action)
    ctx.buttons.add("💡 Wake-on-LAN", "p.wol_sender:menu", sort_key=25)
