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
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    if hours > 0:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def _bar(remaining_percent: int, width: int = 10) -> str:
    filled = round((remaining_percent / 100) * width)
    filled = min(max(filled, 0), width)
    return "█" * filled + "░" * (width - filled)


def _format_bucket(bucket: dict, label: str) -> str:
    remaining = bucket.get("current_interval_remaining_percent", 0)
    used = 100 - remaining
    weekly_remaining = bucket.get("current_weekly_remaining_percent", 0)
    weekly_used = 100 - weekly_remaining
    reset_in = _format_reset(bucket.get("remains_time", 0))
    weekly_reset_in = _format_reset(bucket.get("weekly_remains_time", 0))

    return (
        f"<b>{label}</b>\n"
        f"Window: {_bar(remaining)} {used}% used  ·  resets in {reset_in}\n"
        f"Week:   {_bar(weekly_remaining)} {weekly_used}% used  ·  resets in {weekly_reset_in}"
    )


def _format_remains_section(payload: dict) -> str:
    buckets = payload.get("model_remains", [])
    if not buckets:
        return "No usage buckets returned."

    active = [b for b in buckets if b.get("current_interval_status") == _STATUS_ACTIVE]

    lines = []
    for bucket in active:
        lines.append(_format_bucket(bucket, bucket.get("model_name", "unknown")))
        lines.append("")

    return "\n".join(lines).strip()


def _format_today_section(payload: dict) -> str:
    daily = payload.get("date_model_usage", [])
    if not daily:
        return ""

    today = datetime.date.today().isoformat()
    entry = next((d for d in daily if d.get("date") == today), None)
    if entry is None:
        return f"<b>Today</b>\n0 tokens so far"

    total = entry.get("total_token", 0)
    model_totals = sorted(
        ((m.get("model", "unknown"), m.get("total_token", 0)) for m in entry.get("models", [])),
        key=lambda kv: kv[1],
        reverse=True,
    )
    model_lines = "\n".join(f"  · {name}: {_format_tokens(t)}" for name, t in model_totals if t > 0)

    lines = [f"<b>Today</b>", f"{_format_tokens(total)} tokens"]
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
        f"<b>Last {_ROLLING_WINDOW_DAYS} days</b>",
        f"{_format_tokens(total)} tokens across {active_days} active day{'s' if active_days != 1 else ''}",
    ]
    if model_lines:
        lines.append(model_lines)
    return "\n".join(lines)


def _format_usage_message(remains_payload: dict, summary_payload: dict) -> str:
    parts = ["🤖 <b>MiniMax Coding Plan Usage</b>", "", _format_remains_section(remains_payload)]

    today = _format_today_section(summary_payload)
    if today:
        parts.append("")
        parts.append(today)

    rolling = _format_rolling_window_section(summary_payload)
    if rolling:
        parts.append("")
        parts.append(rolling)

    return "\n".join(parts).strip()


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
        await query.edit_message_text(
            f"🤖 <b>MiniMax Usage</b>\n\n"
            f"❌ Missing <code>{session_token_env}</code> or <code>{group_id_env}</code> in env.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS)],
            ]),
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
        await query.edit_message_text(
            f"🤖 <b>MiniMax Usage</b>\n\n❌ Request timed out after {timeout}s",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Retry", callback_data=_CB_MENU)],
                [InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS)],
            ]),
        )
        return
    except urllib.error.HTTPError as exc:
        await query.edit_message_text(
            f"🤖 <b>MiniMax Usage</b>\n\n❌ HTTP {exc.code} from platform.minimax.io",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Retry", callback_data=_CB_MENU)],
                [InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS)],
            ]),
        )
        return
    except Exception as exc:
        await query.edit_message_text(
            f"🤖 <b>MiniMax Usage</b>\n\n❌ {str(exc)[:200]}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Retry", callback_data=_CB_MENU)],
                [InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS)],
            ]),
        )
        return

    remains_status = remains_payload.get("base_resp", {}).get("status_code")
    summary_status = summary_payload.get("base_resp", {}).get("status_code")
    if remains_status != 0 or summary_status != 0:
        status_msg = (
            remains_payload.get("base_resp", {}).get("status_msg")
            or summary_payload.get("base_resp", {}).get("status_msg")
            or "unknown error"
        )
        await query.edit_message_text(
            f"🤖 <b>MiniMax Usage</b>\n\n"
            f"⚠️ Session expired ({status_msg}).\n\n"
            f"Re-extract <code>_token</code> from platform.minimax.io DevTools "
            f"(Network tab → any request → Cookie header → <code>_token=</code> value), then:\n"
            f"<code>secrets-cli put {session_token_env}</code>\n"
            f"then redeploy: <code>docker compose up -d --build pi-control-bot</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS)],
            ]),
        )
        return

    body = _format_usage_message(remains_payload, summary_payload)

    await query.edit_message_text(
        body,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data=_CB_MENU)],
            [InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS)],
        ]),
    )


def register(ctx: "PluginContext") -> None:
    ctx.actions.register("p.minimax_usage", _handle_action)
    ctx.buttons.add("🤖 MiniMax Usage", _CB_MENU, sort_key=53)
    logger.info("minimax_usage: registered (endpoint=%s)", _REMAINS_URL)
