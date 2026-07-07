from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from plugins._ctx import PluginMeta

if TYPE_CHECKING:
    from plugins._ctx import PluginContext

logger = logging.getLogger(__name__)

META = PluginMeta(
    name="minimax_usage",
    description="Show MiniMax Coding Plan token usage via the platform console API",
    default_config={
        "session_token_env": "MINIMAX_SESSION_TOKEN",
        "group_id_env": "MINIMAX_GROUP_ID",
        "timeout": 15,
    },
)

_CB_MENU = "p.minimax_usage:menu"
_CB_PLUGINS = "plugins_menu"

_REMAINS_URL = "https://platform.minimax.io/v1/api/openplatform/coding_plan/remains"
_USAGE_SUMMARY_URL = "https://platform.minimax.io/backend/account/token_plan/usage_summary"

# current_interval_status / current_weekly_status: 1 = active, 3 = inactive (bucket unused)
_STATUS_ACTIVE = 1

_ROLLING_WINDOW_DAYS = 30
_BAR_WIDTH = 12
_BUCKET_LABEL_WIDTH = 18

_REFRESH_PLUGINS_ROW = [
    InlineKeyboardButton("🔄 Refresh", callback_data=_CB_MENU),
    InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS),
]


def _fetch_json(url: str, session_token: str, group_id: str, timeout: int) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "X-Group-Id": group_id,
            "Cookie": f"_token={session_token}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _format_tokens(n: float) -> str:
    n = float(n)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{int(n)}"


def _format_reset(ms: int) -> str:
    total_seconds = max(ms, 0) // 1000
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def _status_emoji(used_percent: int) -> str:
    if used_percent >= 85:
        return "🔴"
    if used_percent >= 60:
        return "🟡"
    return "🟢"


def _bar(used_percent: int, width: int = _BAR_WIDTH) -> str:
    used = min(max(int(used_percent), 0), 100)
    filled = round((used / 100) * width)
    filled = min(max(filled, 0), width)
    return f"{_status_emoji(used)} {'█' * filled}{'░' * (width - filled)} {used}%"


def _format_bucket(bucket: dict) -> str:
    name = bucket.get("model_name", "unknown")[:_BUCKET_LABEL_WIDTH].ljust(_BUCKET_LABEL_WIDTH)
    remaining = bucket.get("current_interval_remaining_percent", 0)
    weekly_remaining = bucket.get("current_weekly_remaining_percent", 0)
    used = 100 - remaining
    weekly_used = 100 - weekly_remaining
    reset_in = _format_reset(bucket.get("remains_time", 0))
    weekly_reset_in = _format_reset(bucket.get("weekly_remains_time", 0))

    return (
        f"<code>{name}</code>\n"
        f"  🪟 Window  {_bar(used)}  ·  resets in {reset_in}\n"
        f"  📅 Week    {_bar(weekly_used)}  ·  resets in {weekly_reset_in}"
    )


def _format_header(remains_payload: dict) -> str:
    buckets = [
        b for b in remains_payload.get("model_remains", [])
        if b.get("current_interval_status") == _STATUS_ACTIVE
    ]
    now = datetime.datetime.now().strftime("%H:%M")

    if not buckets:
        return (
            f"🤖 <b>MiniMax Coding Plan Usage</b>\n"
            f"<i>Last updated {now}</i>\n\n"
            f"ℹ️ No active usage buckets — Coding Plan may not be enabled on this group."
        )

    return (
        f"🤖 <b>MiniMax Coding Plan Usage</b>\n"
        f"<i>Last updated {now}</i>"
    )


def _format_bucket_sections(remains_payload: dict) -> str:
    buckets = [
        b for b in remains_payload.get("model_remains", [])
        if b.get("current_interval_status") == _STATUS_ACTIVE
    ]
    if not buckets:
        return ""

    blocks = [_format_bucket(b) for b in buckets]
    return "\n\n".join(blocks)


def _format_today_section(payload: dict) -> str:
    daily = payload.get("date_model_usage", [])
    if not daily:
        return ""

    today = datetime.date.today().isoformat()
    entry = next((d for d in daily if d.get("date") == today), None)
    if entry is None:
        return "📅 <b>Today</b>\n<i>0 tokens so far</i>"

    total = entry.get("total_token", 0)
    model_totals = sorted(
        ((m.get("model", "unknown"), m.get("total_token", 0)) for m in entry.get("models", [])),
        key=lambda kv: kv[1],
        reverse=True,
    )
    model_lines = "\n".join(f"  · {name}: {_format_tokens(t)}" for name, t in model_totals if t > 0)

    lines = [f"📅 <b>Today</b>  ·  {_format_tokens(total)} tokens"]
    if model_lines:
        lines.append(model_lines)
    return "\n".join(lines)


def _format_rolling_window_section(payload: dict) -> str:
    daily = payload.get("date_model_usage", [])
    if not daily:
        return ""

    cutoff = (datetime.date.today() - datetime.timedelta(days=_ROLLING_WINDOW_DAYS)).isoformat()
    window = [d for d in daily if d.get("date", "") >= cutoff]
    if not window:
        return ""

    total = sum(d.get("total_token", 0) for d in window)
    active_days = sum(1 for d in window if d.get("total_token", 0) > 0)

    model_totals: dict[str, int] = {}
    for day in window:
        for m in day.get("models", []):
            name = m.get("model", "unknown")
            model_totals[name] = model_totals.get(name, 0) + m.get("total_token", 0)

    top_models = sorted(model_totals.items(), key=lambda kv: kv[1], reverse=True)[:5]
    model_lines = "\n".join(f"  · {name}: {_format_tokens(t)}" for name, t in top_models if t > 0)

    lines = [
        f"📊 <b>Last {_ROLLING_WINDOW_DAYS} days</b>  ·  {_format_tokens(total)} tokens",
        f"<i>across {active_days} active day{'s' if active_days != 1 else ''}</i>",
    ]
    if model_lines:
        lines.append(model_lines)
    return "\n".join(lines)


def _format_usage_message(remains_payload: dict, summary_payload: dict) -> str:
    parts = [_format_header(remains_payload)]

    bucket_blocks = _format_bucket_sections(remains_payload)
    if bucket_blocks:
        parts.append("")
        parts.append(bucket_blocks)

    today = _format_today_section(summary_payload)
    rolling = _format_rolling_window_section(summary_payload)
    tail_sections = [s for s in (today, rolling) if s]
    if tail_sections:
        parts.append("")
        parts.append("\n—————————\n".join(tail_sections))

    return "\n".join(parts).strip()


async def _edit_error(query, title_icon: str, body: str, *, show_retry: bool = True) -> None:
    rows = []
    if show_retry:
        rows.append([InlineKeyboardButton("🔄 Retry", callback_data=_CB_MENU)])
    rows.append([InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS)])
    await query.edit_message_text(
        f"🤖 <b>MiniMax Usage</b>\n\n{title_icon} {body}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _handle_action(query, parts, ctx: "PluginContext") -> None:
    session_token_env = ctx.plugin_cfg.get("session_token_env", "MINIMAX_SESSION_TOKEN")
    group_id_env = ctx.plugin_cfg.get("group_id_env", "MINIMAX_GROUP_ID")
    timeout = int(ctx.plugin_cfg.get("timeout", 15))
    sub = parts[1] if len(parts) > 1 else "menu"
    if sub != "menu":
        return

    session_token = os.environ.get(session_token_env, "")
    group_id = os.environ.get(group_id_env, "")

    if not session_token or not group_id:
        await _edit_error(
            query,
            "⛔",
            f"Missing <code>{session_token_env}</code> or <code>{group_id_env}</code> in env.",
            show_retry=False,
        )
        return

    await query.edit_message_text("⏳ Fetching MiniMax usage…")

    try:
        remains_payload, summary_payload = await asyncio.wait_for(
            asyncio.gather(
                asyncio.to_thread(_fetch_json, _REMAINS_URL, session_token, group_id, timeout),
                asyncio.to_thread(_fetch_json, _USAGE_SUMMARY_URL, session_token, group_id, timeout),
            ),
            timeout=timeout + 5,
        )
    except asyncio.TimeoutError:
        await _edit_error(query, "⏱", f"Request timed out after {timeout}s")
        return
    except urllib.error.HTTPError as exc:
        await _edit_error(query, "🌐", f"HTTP {exc.code} from platform.minimax.io")
        return
    except Exception as exc:
        await _edit_error(query, "❌", str(exc)[:200])
        return

    remains_status = remains_payload.get("base_resp", {}).get("status_code")
    summary_status = summary_payload.get("base_resp", {}).get("status_code")
    if remains_status != 0 or summary_status != 0:
        status_msg = (
            remains_payload.get("base_resp", {}).get("status_msg")
            or summary_payload.get("base_resp", {}).get("status_msg")
            or "unknown error"
        )
        await _edit_error(
            query,
            "🔑",
            (
                f"Session expired ({status_msg}).\n\n"
                f"Re-extract <code>_token</code> from platform.minimax.io DevTools "
                f"(Network tab → any request → Cookie header → <code>_token=</code> value), then:\n"
                f"<code>secrets-cli put {session_token_env}</code>\n"
                f"then redeploy: <code>docker compose up -d --build pi-control-bot</code>"
            ),
            show_retry=False,
        )
        return

    body = _format_usage_message(remains_payload, summary_payload)

    await query.edit_message_text(
        body,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([_REFRESH_PLUGINS_ROW]),
    )


def register(ctx: "PluginContext") -> None:
    ctx.actions.register("p.minimax_usage", _handle_action)
    ctx.buttons.add("🤖 MiniMax Usage", _CB_MENU, sort_key=53)
    logger.info("minimax_usage: registered (endpoint=%s)", _REMAINS_URL)