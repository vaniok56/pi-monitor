from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

import timez
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from plugins._ctx import PluginMeta

if TYPE_CHECKING:
    from plugins._ctx import PluginContext

logger = logging.getLogger(__name__)

META = PluginMeta(
    name="uptime_kuma_summary",
    description="Show Uptime Kuma monitor statuses via status-page API",
    default_config={
        "url": "http://localhost:3001",
        "slug": "default",
        "timeout": 10,
    },
)

_CB_MENU = "p.uptime_kuma_summary:menu"
_CB_PLUGINS = "plugins_menu"

# Monitor status codes from Uptime Kuma
_STATUS = {1: ("✅", "Up"), 0: ("🔴", "Down"), 2: ("🟡", "Pending"), 3: ("🔵", "Maintenance")}


def _fetch_json(url: str, timeout: int) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def _get_status_page(base_url: str, slug: str, timeout: int) -> dict:
    """Fetch monitor list and latest heartbeats. Returns combined dict."""
    base = base_url.rstrip("/")
    page_url = f"{base}/api/status-page/{slug}"
    hb_url = f"{base}/api/status-page/heartbeat/{slug}"

    page_data, hb_data = await asyncio.gather(
        asyncio.to_thread(_fetch_json, page_url, timeout),
        asyncio.to_thread(_fetch_json, hb_url, timeout),
        return_exceptions=True,
    )

    if isinstance(page_data, Exception):
        return {"error": f"Status page fetch failed: {page_data}"}
    if isinstance(hb_data, Exception):
        return {"error": f"Heartbeat fetch failed: {hb_data}"}

    # Build monitor list from publicGroupList
    monitors = []
    for group in page_data.get("publicGroupList", []):
        for mon in group.get("monitorList", []):
            mid = str(mon.get("id"))
            heartbeats = hb_data.get("heartbeatList", {}).get(mid, [])
            latest_hb = heartbeats[-1] if heartbeats else None
            status = latest_hb.get("status", 2) if latest_hb else 2
            ping = latest_hb.get("ping") if latest_hb else None
            uptime_24 = hb_data.get("uptimeList", {}).get(f"{mid}_24", None)
            monitors.append({
                "id": mid,
                "name": mon.get("name", f"monitor-{mid}"),
                "group": group.get("name", ""),
                "status": status,
                "ping": ping,
                "uptime_24": uptime_24,
            })

    return {"monitors": monitors, "title": page_data.get("config", {}).get("title", slug)}


def _format_monitors(monitors: list[dict]) -> str:
    if not monitors:
        return "No monitors on this status page."

    # Group by group name
    groups: dict[str, list] = {}
    for m in monitors:
        g = m["group"] or "Default"
        groups.setdefault(g, []).append(m)

    lines = []
    for g_name, mons in groups.items():
        if len(groups) > 1:
            lines.append(f"\n<b>{g_name}</b>")
        for m in mons:
            icon, label = _STATUS.get(m["status"], ("❓", "Unknown"))
            ping_s = f"  {m['ping']}ms" if m["ping"] is not None else ""
            uptime_s = f"  {m['uptime_24'] * 100:.1f}% (24h)" if m["uptime_24"] is not None else ""
            lines.append(f"{icon} <code>{m['name']}</code>{ping_s}{uptime_s}")

    up = sum(1 for m in monitors if m["status"] == 1)
    down = sum(1 for m in monitors if m["status"] == 0)
    return "\n".join(lines) + f"\n\n<i>{up}/{len(monitors)} up</i>" + (f"  ·  <b>{down} down</b>" if down else "")


async def _handle_action(query, parts, ctx: "PluginContext") -> None:
    base_url = ctx.plugin_cfg.get("url", "http://localhost:3001")
    slug = ctx.plugin_cfg.get("slug", "default")
    timeout = int(ctx.plugin_cfg.get("timeout", 10))
    sub = parts[1] if len(parts) > 1 else "menu"
    if sub != "menu":
        return

    await query.edit_message_text("⏳ Fetching Uptime Kuma status…")

    try:
        result = await asyncio.wait_for(
            _get_status_page(base_url, slug, timeout),
            timeout=timeout + 5,
        )
    except asyncio.TimeoutError:
        result = {"error": f"Request timed out after {timeout}s"}
    except Exception as exc:
        result = {"error": str(exc)[:200]}

    if "error" in result:
        await query.edit_message_text(
            f"📡 <b>Uptime Kuma</b>\n\n❌ {result['error']}\n\n"
            f"<i>URL: <code>{base_url}</code>  slug: <code>{slug}</code></i>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Retry", callback_data=_CB_MENU)],
                [InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS)],
            ]),
        )
        return

    monitors = result.get("monitors", [])
    title = result.get("title", slug)
    body = _format_monitors(monitors)

    await query.edit_message_text(
        f"📡 <b>Uptime Kuma — {title}</b>\n\n{body}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data=_CB_MENU)],
            [InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS)],
        ]),
    )


def register(ctx: "PluginContext") -> None:
    base_url = ctx.plugin_cfg.get("url", "http://localhost:3001")
    slug = ctx.plugin_cfg.get("slug", "default")

    ctx.actions.register("p.uptime_kuma_summary", _handle_action)
    ctx.buttons.add("📡 Uptime Kuma", _CB_MENU, sort_key=52)
    logger.info("uptime_kuma_summary: %s/api/status-page/%s", base_url, slug)
