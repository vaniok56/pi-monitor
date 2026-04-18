from __future__ import annotations

import asyncio
import logging
import shlex

import timez
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from plugins._ctx import PluginMeta

if TYPE_CHECKING:
    from plugins._ctx import PluginContext

logger = logging.getLogger(__name__)

META = PluginMeta(
    name="stremio_cache",
    description="Wipe Stremio server cache on schedule or on demand",
    default_config={
        "container": "stremio-server",
        "path": "/root/.stremio-server/stremio-cache",
        "schedule": "0 3 * * 0",
    },
)

_CB = "p.stremio_cache"
_CB_REPORT = "p.stremio_cache:report"
_CB_CONFIRM = "p.stremio_cache:confirm"
_CB_RUN = "p.stremio_cache:run"
_CB_PLUGINS = "plugins_menu"


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


async def _exec_wipe(container: str, path: str) -> str:
    """Run du + rm -rf inside the named container via docker exec."""
    size_bytes, _ = await _probe_cache_size(container, path)
    size_before = size_bytes if size_bytes is not None else 0

    # Wipe contents, keep directory
    qpath = shlex.quote(path)
    proc2 = await asyncio.create_subprocess_exec(
        "docker", "exec", container, "sh", "-c",
        (
            f"if [ -d {qpath} ]; then "
            f"rm -rf {qpath}/* 2>&1 && echo ok; "
            f"else echo __PATH_MISSING__; fi"
        ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out2, err2 = await asyncio.wait_for(proc2.communicate(), timeout=120)
    combined = (out2 + err2).decode("utf-8", errors="replace").strip()
    if proc2.returncode != 0:
        return f"❌ Wipe failed:\n<code>{combined[:500]}</code>"

    if "__PATH_MISSING__" in combined:
        return "ℹ️ Cache path not found in container — nothing to wipe."

    freed = _human_bytes(size_before)
    return f"✅ Stremio cache wiped — freed <b>{freed}</b>."


async def _probe_cache_size(container: str, path: str) -> tuple[int | None, str | None]:
    qpath = shlex.quote(path)
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", container, "sh", "-c",
        (
            f"if [ -d {qpath} ]; then "
            f"du -sb {qpath} | cut -f1; "
            f"else echo __PATH_MISSING__; fi"
        ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        detail = err or out or f"docker exec failed (exit {proc.returncode})"
        return None, detail[:300]

    if out == "__PATH_MISSING__":
        return 0, "Path not found in container"

    try:
        return int(out), None
    except (ValueError, UnicodeDecodeError):
        detail = out or err or "unexpected command output"
        return None, f"Unable to parse cache size: {detail[:200]}"


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _next_cron_run(expr: str) -> str | None:
    try:
        nxt = timez.next_cron(expr)
        return nxt.strftime(f"%Y-%m-%d %H:%M:%S {timez.tz_label()}")
    except Exception:
        return None


def _report_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Wipe cache", callback_data=_CB_CONFIRM)],
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=_CB_REPORT),
            InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS),
        ],
    ])


def _render_schedule_lines(schedule: str | None) -> list[str]:
    if not schedule:
        return ["🤖 Auto wipe: <b>disabled</b> (manual-only mode)"]

    lines = [f"🤖 Auto wipe: <b>enabled</b> (<code>{schedule}</code> {timez.tz_label()})"]
    next_run = _next_cron_run(schedule)
    if next_run:
        lines.append(f"⏭ Next run: <b>{next_run}</b>")
    else:
        lines.append("⏭ Next run: <i>unavailable</i>")
    return lines


async def _render_report_text(container: str, path: str, schedule: str | None, result_note: str | None = None) -> str:
    size_bytes, note = await _probe_cache_size(container, path)
    size_text = _human_bytes(size_bytes) if size_bytes is not None else "n/a"

    lines = [
        "🧹 <b>Stremio Cache Report</b>",
        "",
        *_render_schedule_lines(schedule),
        "",
        f"Container: <code>{container}</code>",
        f"Path: <code>{path}</code>",
        f"Current size: <b>{size_text}</b>",
        "",
        f"🕒 Updated: <code>{timez.fmt(timez.now(), '%Y-%m-%d %H:%M:%S.%f')} {timez.tz_label()}</code>",
    ]
    if result_note:
        lines.append(f"\n{result_note}")
    if note:
        lines.append(f"\n<i>Probe: {_escape_html(note)}</i>")
    return "\n".join(lines)


async def _handle_action(query, parts, ctx: "PluginContext") -> None:
    sub = parts[1] if len(parts) > 1 else "report"
    container = ctx.plugin_cfg.get("container", "stremio-server")
    path = ctx.plugin_cfg.get("path", "/root/.stremio-server/stremio-cache")
    schedule = ctx.plugin_cfg.get("schedule")

    if sub == "report":
        await query.edit_message_text(
            await _render_report_text(container, path, str(schedule) if schedule else None),
            parse_mode=ParseMode.HTML,
            reply_markup=_report_keyboard(),
        )

    elif sub == "confirm":
        await query.edit_message_text(
            "⚠️ <b>Wipe Stremio cache now?</b>\n\n"
            f"Container: <code>{container}</code>\n"
            f"Path: <code>{path}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, wipe", callback_data=_CB_RUN),
                InlineKeyboardButton("❌ Cancel", callback_data=_CB_REPORT),
            ]]),
        )

    elif sub == "run":
        await query.edit_message_text("⏳ Wiping Stremio cache…")
        result = await _exec_wipe(container, path)
        await query.edit_message_text(
            await _render_report_text(
                container,
                path,
                str(schedule) if schedule else None,
                result_note=result,
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=_report_keyboard(),
        )


async def _scheduled_wipe(context) -> None:
    logger.info("Scheduled Stremio cache wipe starting")
    # ctx stored at registration time via closure
    container = _scheduled_wipe._container
    path = _scheduled_wipe._path
    try:
        result = await _exec_wipe(container, path)
        logger.info("Stremio cache wipe result: %s", result)
    except Exception as exc:
        logger.error("Stremio cache scheduled wipe failed: %s", exc)


def register(ctx: "PluginContext") -> None:
    container = ctx.plugin_cfg.get("container", "stremio-server")
    path = ctx.plugin_cfg.get("path", "/root/.stremio-server/stremio-cache")
    schedule = ctx.plugin_cfg.get("schedule")

    if not container or not path:
        logger.warning("stremio_cache: container or path not set — skipping")
        return

    _scheduled_wipe._container = container
    _scheduled_wipe._path = path

    ctx.actions.register("p.stremio_cache", _handle_action)
    ctx.buttons.add("🧹 Stremio cache", _CB_REPORT, sort_key=20)

    if schedule:
        ctx.scheduler.cron(str(schedule), _scheduled_wipe, "stremio_cache.scheduled")
    else:
        logger.info("stremio_cache: no schedule configured — manual-only mode")
